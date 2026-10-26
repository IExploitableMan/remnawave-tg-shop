"""Recoverable form errors and credential-free operational diagnostics."""
import hashlib
import logging
import os
from pathlib import Path
import re
import traceback


class ProviderSelectionRequired(ValueError):
    def __init__(self):
        super().__init__('Провайдер ноды отсутствует в Infra Billing')


async def require_provider(api, provider_uuid):
    providers = await api.providers()
    provider = next((p for p in providers if p['uuid'] == provider_uuid), None)
    if not provider:
        raise ProviderSelectionRequired()
    return provider


def code_revision():
    revision = os.getenv('BOT_BUILD_REVISION', '')
    if re.fullmatch(r'[0-9a-fA-F]{7,40}', revision):
        return revision
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    files = [root / 'handlers/admin/infrastructure.py'] + sorted((root / 'services/infrastructure').glob('*.py'))
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return 'code-' + digest.hexdigest()[:16]


def log_failure(action, data, error):
    def label(value):
        return str(value) if re.fullmatch(r'[a-z_]{1,40}', str(value)) else '-'
    node_id = data.get('node_id') or data.get('replace_id')
    node_id = str(node_id) if str(node_id).isdigit() else '-'
    frames = traceback.extract_tb(error.__traceback__)
    trace = ' > '.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames)
    # Exception messages and locals can contain passwords (for example int(port)).
    logging.error('Infrastructure failure action=%s flow=%s phase=%s node=%s error=%s revision=%s trace=%s',
        label(action), label(data.get('flow')), label(data.get('phase')), node_id,
        type(error).__name__, code_revision(), trace)
