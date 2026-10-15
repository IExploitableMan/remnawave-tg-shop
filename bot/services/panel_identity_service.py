"""Durable, panel-scoped references across the Remnawave user UUID migration.

Legacy callers retain a stable reference (in their historically named uuid fields).
For newly created v3 users this is a LOCAL UUID, never sent to the panel.
The actual numeric API identifier is kept separately in panel_user_identities.
"""
import hashlib
from uuid import uuid5, NAMESPACE_URL

from sqlalchemy import select, update, or_
from sqlalchemy.dialects.postgresql import insert
from db.models import PanelUserIdentity, User, Subscription


class PanelIdentityService:
    def __init__(self, panel_url, session_factory=None):
        self.scope = hashlib.sha256(panel_url.rstrip('/').encode()).hexdigest()
        self.session_factory = session_factory
        self.by_reference = {}
        self.by_id = {}

    async def load(self):
        if self.session_factory:
            async with self.session_factory() as session:
                rows = (await session.execute(select(PanelUserIdentity).where(
                    PanelUserIdentity.panel_scope == self.scope))).scalars().all()
                for row in rows:
                    self.by_reference[row.reference] = row.panel_user_id
                    self.by_id[row.panel_user_id] = row.reference

    async def normalize(self, raw):
        if not isinstance(raw, dict) or not raw.get('id'):
            return raw
        data = dict(raw)
        identifier = int(data['id'])
        if isinstance(data['id'], bool) or identifier <= 0 or float(data['id']) != identifier:
            raise ValueError('Invalid numeric panel user ID')
        reference = self.by_id.get(identifier) or data.get('uuid')
        async with self._lock:
            reference = self.by_id.get(identifier) or reference
            if self.session_factory:
                async with self.session_factory() as session:
                    stored = await session.get(PanelUserIdentity, (self.scope, identifier))
                    if stored:
                        if data.get('uuid') and data['uuid'] != stored.reference:
                            raise ValueError('Panel UUID/ID mapping changed; refusing to relink an account')
                        reference = stored.reference
                    if not reference:
                        # Recover existing account links after an upgrade. Never guess
                        # among multiple matches or use telegramId for an addon user.
                        username = data.get('username', '')
                        addon = username.endswith('_addon')
                        account_id = None
                        import re
                        match = re.fullmatch(r'(tg|web)_(\d+)(_addon)?', username)
                        if match:
                            account_id = int(match[2]) * (-1 if match[1] == 'web' else 1)
                        elif not addon and data.get('telegramId'):
                            account_id = int(data['telegramId'])
                        user = await session.get(User, account_id) if account_id else None
                        if user:
                            reference = user.addon_panel_user_uuid if addon else user.panel_user_uuid
                    reference = reference or str(uuid5(NAMESPACE_URL, f'{self.scope}:user:{identifier}'))
                    occupied = (await session.execute(select(PanelUserIdentity).where(
                        PanelUserIdentity.panel_scope == self.scope,
                        PanelUserIdentity.reference == reference))).scalar_one_or_none()
                    if occupied and occupied.panel_user_id != identifier:
                        raise ValueError('Panel identity conflict; refusing to relink an account')
                    stmt = insert(PanelUserIdentity).values(panel_scope=self.scope,
                        panel_user_id=identifier, reference=reference)
                    await session.execute(stmt.on_conflict_do_nothing())
                    canonical = await session.get(PanelUserIdentity, (self.scope, identifier))
                    if canonical:
                        reference = canonical.reference
                    await session.commit()
            reference = reference or str(uuid5(NAMESPACE_URL, f'{self.scope}:user:{identifier}'))
            self.by_reference[reference] = identifier
            self.by_id[identifier] = reference
        data['uuid'] = reference
        return data

    @property
    def _lock(self):
        if not hasattr(self, '_identity_lock'):
            import asyncio
            self._identity_lock = asyncio.Lock()
        return self._identity_lock

    async def resolve(self, reference):
        if not self.by_reference:
            await self.load()
        identifier = self.by_reference.get(str(reference))
        if identifier is None:
            raise ValueError('Unknown panel user reference; run panel identity synchronization')
        return identifier
