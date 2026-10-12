"""Infrastructure API operations shared by panel 2.x/3.x."""
import copy


class PanelInfrastructureApi:
    def __init__(self, panel):
        self.panel = panel

    async def call(self, method, path, payload=None):
        kwargs = {'json': payload} if payload is not None else {}
        result = await self.panel._request(method, path, **kwargs)
        if not result or result.get('error') or result.get('status') == 'success_parse_error':
            code = (result or {}).get('status_code', 0)
            raise RuntimeError(f'Panel operation failed: {method} {path} ({code})')
        return result.get('response')

    async def nodes(self):
        return await self.call('GET', '/nodes') or []

    async def providers(self):
        result = await self.call('GET', '/infra-billing/providers') or {}
        return result.get('providers', []) if isinstance(result, dict) else result

    async def profiles(self):
        result = await self.call('GET', '/config-profiles') or {}
        return result.get('configProfiles', []) if isinstance(result, dict) else result

    async def billing(self):
        return await self.call('GET', '/infra-billing/nodes') or {}

    async def node_secret(self):
        result = await self.call('GET', '/keygen')
        secret = result.get('secretKey') or result.get('pubKey')
        if not isinstance(secret, str) or not secret:
            raise RuntimeError('Panel did not return a node secret key')
        return secret

    async def patch_node(self, uuid, values):
        values = dict(values, uuid=uuid)
        if await self.panel.api_major() == 2:
            values.pop('note', None)
        return await self.call('PATCH', '/nodes', values)

    @staticmethod
    def access_for_version(values, major):
        """Translate saved host restrictions when the panel was upgraded."""
        values = copy.deepcopy(values)
        if major == 3 and 'excludedInternalSquads' in values:
            values['internalSquads'] = {'mode': 'EXCLUDE', 'squads': values.pop('excludedInternalSquads')}
        elif major == 2 and 'internalSquads' in values:
            access = values.pop('internalSquads') or {'mode': 'EXCLUDE', 'squads': []}
            if access['mode'] != 'EXCLUDE':
                raise ValueError('Host access cannot be restored on this panel version')
            values['excludedInternalSquads'] = access['squads']
        return values

    @staticmethod
    def exclusion_values(host, tg_uuid, major):
        if major == 2:
            original = {'excludedInternalSquads': list(host.get('excludedInternalSquads') or [])}
            values = {'excludedInternalSquads': list(dict.fromkeys(original['excludedInternalSquads'] + [tg_uuid]))}
        else:
            original = {'internalSquads': copy.deepcopy(host.get('internalSquads'))}
            access = copy.deepcopy(host.get('internalSquads') or {'mode': 'EXCLUDE', 'squads': []})
            if access['mode'] == 'ALLOW_ONLY':
                access['squads'] = [s for s in access['squads'] if s != tg_uuid]
                if not access['squads']:
                    original['isDisabled'] = host.get('isDisabled', False)
                    return original, {'isDisabled': True}
            elif access['mode'] == 'EXCLUDE':
                access['squads'] = list(dict.fromkeys(access['squads'] + [tg_uuid]))
            else:
                raise ValueError('Unsupported host access mode')
            values = {'internalSquads': access}
        return original, values

    async def host_access(self, host, tg_uuid, exclude, previous=None):
        if not exclude:
            if previous is None:
                raise ValueError('Cannot restore unknown previous host access')
            return previous, await self.call('PATCH', '/hosts', dict(previous, uuid=host['uuid']))
        original, values = self.exclusion_values(host, tg_uuid, await self.panel.api_major())
        return original, await self.call('PATCH', '/hosts', dict(values, uuid=host['uuid']))
