"""Reconcile the active provider without rewriting provider expense history."""
import json

from db.models import InfrastructureJob as Job, InfrastructureNode as Node


def provider_uuid(entry):
    return entry.get('providerUuid') or (entry.get('provider') or {}).get('uuid')


async def node_entries(api, node_uuid):
    billing = await api.billing()
    if not isinstance(billing, dict) or not isinstance(billing.get('billingNodes'), list):
        raise RuntimeError('Не удалось получить полный список биллинга панели')
    entries = billing['billingNodes']
    if any(not isinstance(entry, dict) or not entry.get('uuid') for entry in entries):
        raise RuntimeError('Не удалось получить полный список биллинга панели')
    return [entry for entry in entries
        if (entry.get('nodeUuid') or (entry.get('node') or {}).get('uuid')) == node_uuid]


async def reconcile_billing(service, node, deployment, job_id):
    api = service.api
    entries = await node_entries(api, node.panel_uuid)
    selected = next((entry for entry in entries if provider_uuid(entry) == node.provider_uuid), None)
    old = next((entry for entry in entries if entry['uuid'] == node.billing_uuid), None)
    if old is None and len(entries) == 1:
        old = entries[0]
    async with service.factory() as session:
        job = await session.get(Job, job_id)
        payload = json.loads(job.payload) if job.payload else {}
        previous = payload.get('billing_previous_uuid')
        if not previous and old and provider_uuid(old) != node.provider_uuid:
            previous = old['uuid']
            payload['billing_previous_uuid'] = previous
            job.payload = json.dumps(payload)
            await session.commit()
    deadline = deployment.paid_until.isoformat()
    if selected:
        await api.call('PATCH', '/infra-billing/nodes', {'uuids': [selected['uuid']], 'nextBillingAt': deadline})
    else:
        try:
            await api.call('POST', '/infra-billing/nodes', {'providerUuid': node.provider_uuid,
                'nodeUuid': node.panel_uuid, 'name': node.name, 'nextBillingAt': deadline})
        except Exception:
            # A timed-out create may already have committed. Never drop the old
            # assignment unless the replacement can actually be read back.
            entries = await node_entries(api, node.panel_uuid)
            if not any(provider_uuid(entry) == node.provider_uuid for entry in entries):
                raise
        entries = await node_entries(api, node.panel_uuid)
        selected = next((entry for entry in entries if provider_uuid(entry) == node.provider_uuid), None)
        if not selected:
            raise RuntimeError('Не найдена созданная запись Infra Billing')
    node.billing_uuid = selected['uuid']
    async with service.factory() as session:
        (await session.get(Node, node.id)).billing_uuid = selected['uuid']
        await session.commit()
    if previous and previous != selected['uuid']:
        entries = await node_entries(api, node.panel_uuid)
        previous_entry = next((entry for entry in entries if entry['uuid'] == previous), None)
        if previous_entry and provider_uuid(previous_entry) != node.provider_uuid:
            try:
                await api.call('DELETE', f'/infra-billing/nodes/{previous}')
            except Exception:
                if any(entry['uuid'] == previous for entry in await node_entries(api, node.panel_uuid)):
                    raise RuntimeError('Новый провайдер сохранён, но старую привязку биллинга удалить не удалось. Повторите задание.') from None
    return selected
