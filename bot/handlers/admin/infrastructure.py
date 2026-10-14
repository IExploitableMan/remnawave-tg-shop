"""Private administrator UI for infrastructure. No secrets in callback payloads."""
import html
import asyncio
from contextvars import ContextVar
import ipaddress
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import asyncssh
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, text

from db.models import InfrastructureNode as Node, InfrastructureDeployment as Deployment, InfrastructureJob as Job, InfrastructureReport as Report
from db.dal import app_settings_dal
from bot.services.infrastructure.settings import read_config, save_config, validate_address, validate_hostname
from bot.services.infrastructure.provisioning import host_key
from bot.services.infrastructure.diagnostics import send_artifacts
from bot.services.infrastructure.localization import bind_language, tr, localize_error
from bot.services.infrastructure.job_messages import operation_label, operation_text, operation_buttons, publish_job, status_label
from bot.services.infrastructure.versions import latest_node_version, version_tuple

router = Router(name='admin_infrastructure')


class InfrastructureStates(StatesGroup):
    input = State()
    confirm = State()


def keyboard(buttons):
    builder = InlineKeyboardBuilder()
    for label, payload in buttons:
        builder.button(text=label, callback_data=payload)
    builder.adjust(1)
    return builder.as_markup()


BACK_TARGET = ContextVar('infra_back_target', default='infra:home')


def back(target=None):
    return [(tr('infra_back'), target or BACK_TARGET.get())]


async def show(callback, content, buttons):
    try:
        await callback.message.edit_text(content, reply_markup=keyboard(buttons))
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise


async def card(callback, service, session, node_id):
    node = await service.node(session, node_id, include_deleted=True)
    if not node.deleted:
        await service.sync_removed_nodes(session)
        await session.commit()
    if node.deleted:
        BACK_TARGET.set('infra:archive')
        await show(callback, tr('infra_archived_node', p0=html.escape(node.name)), [
            (tr('infra_credentials'), f'infra:credentials:{node.id}'),
            (tr('infra_test_history'), f'infra:reports:{node.id}'),
            (tr('infra_operation_history'), f'infra:history:{node.id}')] + back())
        return
    await service.refresh_node(session, node)
    deployment = await service.deployment(session, node.id)
    entry = await service.billing_entry(session, node)
    await session.commit()
    text_value = tr('infra_node_period', p0=html.escape(node.name), p1=node.period_days)
    provider = next((p for p in await service.api.providers() if p['uuid'] == node.provider_uuid), None)
    if provider:
        text_value += tr('infra_provider_label') + html.escape(provider['name'])
    if deployment:
        text_value += tr('infra_node_addresses', p0=html.escape(deployment.address), p1=html.escape(deployment.host_address), p2=html.escape(deployment.node_version))
    if entry:
        text_value += tr('infra_paid_until', p0=html.escape(entry['nextBillingAt']))
    else:
        text_value += tr('infra_unknown_deadline')
    if not node.host_automation:
        text_value += tr('infra_access_only_mode')
    text_value += tr('infra_tg_access') + (tr('infra_excluded') if node.excluded_tg else tr('infra_not_excluded'))
    gemini_row = await app_settings_dal.get_setting(session, f'infra.gemini.{service.scope}.{node.id}')
    gemini_state = json.loads(gemini_row.value) if gemini_row and gemini_row.value else {}
    gemini_enabled = bool(gemini_state.get('enabled') and deployment
        and gemini_state.get('gemini_profile_uuid') == deployment.profile_uuid
        and gemini_state.get('gemini_inbound_uuid') == deployment.inbound_uuid)
    gemini_revert_available = bool(gemini_state.get('base_profile_uuid') and gemini_state.get('base_inbound_uuid'))
    if gemini_enabled and not gemini_revert_available:
        text_value += tr('infra_gemini_panel_selected')
    buttons = [(tr('infra_extend'), f'infra:extend:{node.id}'), (tr('infra_replace'), f'infra:replace:{node.id}'),
               (tr('infra_credentials'), f'infra:credentials:{node.id}'),
               (tr('infra_save_access'), f'infra:access:{node.id}'), (tr('infra_diagnostics'), f'infra:tests:{node.id}'),
               (tr('infra_test_history'), f'infra:reports:{node.id}'), (tr('infra_operation_history'), f'infra:history:{node.id}'), (tr('infra_restart'), f'infra:restart:{node.id}'),
               (tr('infra_upgrade'), f'infra:upgrade:{node.id}'),
               (tr('infra_edit_access'), f'infra:edit:{node.id}'),
               (tr('infra_change_sni'), f'infra:sni:{node.id}'), (tr('infra_delete'), f'infra:delete:{node.id}')] + back()
    if not gemini_enabled or gemini_revert_available:
        buttons.insert(-6, (tr('infra_gemini_disable' if gemini_enabled else 'infra_gemini_enable'),
            f"infra:gemini_{'disable' if gemini_enabled else 'enable'}:{node.id}"))
    buttons.insert(-6, (tr('infra_bridge_create'), f'infra:bridge_create:{node.id}'))
    await show(callback, text_value, buttons)


LABELS = {
    'default_days': 'infra_default_days', 'warning_hours': 'infra_warning_hours',
    'expire_exclude_tg': 'infra_expire_exclude', 'panel_ip': 'infra_panel_ip',
    'large_repeats': 'infra_large_repeats', 'node_version_2': 'infra_version_2',
    'node_version_3': 'infra_version_3',
}


async def ask(message, state, service, session):
    data = await state.get_data()
    step = data['steps'][data['position']]
    defaults = {'ssh_port': '22', 'ssh_user': 'root', 'host_address': data.get('address', ''),
                'days': str(data.get('previous_days') or (await read_config(session))['default_days'])}
    prompts = {'address': tr('infra_ask_address'), 'ssh_port': tr('infra_ask_port'),
        'ssh_user': tr('infra_ask_user'), 'password': tr('infra_ask_password'),
        'host_address': tr('infra_ask_client_address'), 'country': tr('infra_ask_country'),
        'days': tr('infra_ask_days')}
    previous = dict(data.get('previous', {}))
    if step in data and step != 'password':
        previous[step] = data[step]
    prompt = prompts[step]
    if step in previous:
        prompt += tr('infra_previous_value', p0=html.escape(str(previous[step])))
    buttons = []
    if step in data:
        buttons.append((tr('infra_keep_value'), 'infra:keep'))
    if step in defaults:
        buttons.append((tr('infra_use_default', p0=defaults[step]), 'infra:default'))
    buttons += [(tr('infra_cancel'), 'infra:home')] + back('infra:wizard_back' if data['position'] else 'infra:home')
    await state.update_data(phase='fields')
    await message.answer(prompt, reply_markup=keyboard(buttons))


async def advance(message, state, service, session, value):
    data = await state.get_data()
    if data.get('phase', 'fields') != 'fields' or data['position'] >= len(data['steps']):
        raise ValueError(tr('infra_choose_provider_buttons'))
    field = data['steps'][data['position']]
    if field in {'address', 'host_address'}:
        value = validate_address(value)
    elif field == 'ssh_port':
        value = int(value)
        if not 1 <= value <= 65535:
            raise ValueError(tr('infra_invalid_port'))
    elif field == 'ssh_user':
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value):
            raise ValueError(tr('infra_invalid_user'))
        if value != 'root':
            raise ValueError(tr('infra_root_required'))
    elif field == 'password':
        if not value or len(value) > 512 or '\n' in value:
            raise ValueError(tr('infra_invalid_password'))
        value = service.cipher().encrypt(value.encode()).decode()
    elif field == 'country':
        value = value.upper()
        if not re.fullmatch(r'[A-Z]{2}', value):
            raise ValueError(tr('infra_invalid_country'))
    elif field == 'days':
        value = int(value)
        if not 1 <= value <= 3650:
            raise ValueError(tr('infra_invalid_days'))
    await state.update_data(**{field: value, 'position': data['position'] + 1})
    if data['position'] + 1 < len(data['steps']):
        await ask(message, state, service, session)
    else:
        data = await state.get_data()
        if data.get('profile_step'):
            await choose_wizard_profile(message, state, service, session)
        elif data.get('replace_id'):
            await confirmation(message, state, service)
        else:
            await choose_provider(message, state, service)


async def choose_wizard_profile(message, state, service, session):
    data = await state.get_data()
    config = await read_config(session)
    selected = data.get('profile_uuid') or config['profile_uuid']
    profiles = await service.api.profiles()
    profile = next((p for p in profiles if p['uuid'] == selected), None)
    name = profile['name'] if profile else tr('infra_profile_missing')
    await state.update_data(phase='profile', profile_uuid=selected, profile_name=name,
        inbound_uuid=data.get('inbound_uuid') or config['inbound_uuid'])
    await state.set_state(InfrastructureStates.input)
    buttons = [(tr('infra_use_profile'), 'infra:wizard_profile_default')] if profile else []
    buttons.append((tr('infra_choose_another_profile'), 'infra:wizard_profiles'))
    await message.answer(tr('infra_wizard_profile', p0=html.escape(name)), reply_markup=keyboard(
        buttons + back('infra:wizard_back')))


async def finish_wizard_profile(message, state, service):
    data = await state.get_data()
    if data.get('replace_id'):
        await confirmation(message, state, service)
    else:
        await choose_provider(message, state, service)


async def choose_provider(message, state, service):
    await state.set_state(InfrastructureStates.input)
    await state.update_data(phase='providers')
    providers = await service.api.providers()
    await message.answer(tr('infra_choose_provider'), reply_markup=keyboard(
        [(p['name'], f"infra:pickprovider:{p['uuid']}") for p in providers] +
        [(tr('infra_add_provider'), 'infra:addprovider'), (tr('infra_cancel'), 'infra:home')] + back('infra:wizard_back')))


async def confirmation(message, state, service):
    data = await state.get_data()
    await message.answer(tr('infra_checking_ssh'),
        reply_markup=keyboard(back('infra:wizard_back')))
    # A timeout is a recoverable form error, not a lost update. The callback
    # was already acknowledged and the wizard data stay available for correction.
    public, fingerprint = await host_key(data['address'], int(data['ssh_port']))
    current = await state.get_data()
    if current.get('flow') != 'wizard' or any(current.get(k) != data.get(k)
            for k in ('position', 'address', 'ssh_port', 'provider_uuid')):
        return  # A Back/edit or newer selection must win over this SSH response.
    await state.update_data(host_public_key=public, phase='confirm')
    await state.set_state(InfrastructureStates.confirm)
    await message.answer(tr('infra_confirm_install', p0=html.escape(data['address']), p1=html.escape(data['host_address']), p2=data['country'], p3=data['days'], p4=html.escape(fingerprint)) + (tr('infra_selected_profile', p0=html.escape(data['profile_name'])) if data.get('profile_name') else ''),
        reply_markup=keyboard([(tr('infra_install'), 'infra:install'), (tr('infra_cancel'), 'infra:home')] + back('infra:wizard_back')))


async def wizard_back(message, state, service, session, provider_form=False):
    data = await state.get_data()
    if provider_form:
        data = data.get('resume_wizard') or {}
        if data.get('flow') != 'wizard':
            raise ValueError(tr('infra_form_closed'))
        await state.set_data(data)
        await choose_provider(message, state, service)
        return
    if data.get('flow') != 'wizard':
        raise ValueError(tr('infra_form_closed'))
    if data.get('phase') == 'confirm' and data.get('replace_id') and data.get('profile_step'):
        await choose_wizard_profile(message, state, service, session)
        return
    if data.get('phase') == 'confirm' and not data.get('replace_id'):
        await choose_provider(message, state, service)
        return
    if data.get('profile_step') and data.get('phase') == 'providers':
        await choose_wizard_profile(message, state, service, session)
        return
    if data.get('phase') in {'profile_list', 'profile_inbounds'}:
        await choose_wizard_profile(message, state, service, session)
        return
    position = max(0, min(data['position'], len(data['steps'])) - 1)
    await state.update_data(position=position, phase='fields')
    await state.set_state(InfrastructureStates.input)
    await ask(message, state, service, session)


@router.callback_query(F.data.startswith('infra:'))
async def infrastructure_callback(callback: types.CallbackQuery, state: FSMContext, session,
                                  infrastructure_service, settings, i18n_data=None):
    bind_language(i18n_data, settings, callback)
    service = infrastructure_service
    if callback.from_user.id not in settings.ADMIN_IDS or callback.message.chat.type != 'private':
        await callback.answer(tr('infra_access_denied'), show_alert=True); return
    if not settings.INFRA_ENABLED:
        await callback.answer(tr('infra_disabled_config'), show_alert=True); return
    await callback.answer()
    parts = callback.data.split(':')
    action = parts[1]
    argument = parts[2] if len(parts) > 2 else None
    data = await state.get_data()
    target = 'infra:home'
    if action == 'home': target = 'admin_action:runtime_settings'
    elif action in {'node', 'adopt'}: target = 'infra:nodes'
    elif action in {'provider', 'addprovider'}: target = 'infra:providers'
    elif action in {'provideredit', 'payment'}: target = f'infra:provider:{argument}'
    elif action in {'setting', 'profiles', 'squads', 'squad', 'pool', 'inbound', 'bridge'}: target = 'infra:settings'
    elif action in {'bridge_setting', 'bridge_key', 'bridge_test'}: target = 'infra:bridge'
    elif action == 'profile': target = 'infra:profiles'
    elif action in {'addkey', 'sync_keys'}: target = 'infra:keys'
    elif action in {'tests', 'test', 'restart', 'upgrade', 'extend', 'access', 'edit', 'sni', 'credentials', 'delete', 'reports', 'history', 'upgrade_confirm', 'gemini_enable', 'gemini_disable', 'bridge_create'}:
        target = f'infra:node:{argument}'
    elif action in {'job', 'cancel', 'retry'}: target = 'infra:jobs'
    if action == 'job' and len(parts) > 4 and parts[3] == 'queue':
        target = f'infra:jobs:{int(parts[4])}'
    elif action == 'job' and len(parts) > 5 and parts[3] == 'history':
        target = f'infra:history:{int(parts[4])}:{int(parts[5])}'
    elif action in {'notify', 'toggle_notify'}: target = 'infra:home'
    if action == 'addprovider' and data.get('flow') == 'wizard':
        target = 'infra:wizard_provider_back'
    if data.get('flow') == 'wizard' and action in {'default', 'keep', 'pickprovider', 'install'}:
        target = 'infra:wizard_back'
    BACK_TARGET.set(target)
    if action not in {'new', 'replace', 'default', 'keep', 'pickprovider', 'install', 'wizard_back', 'wizard_provider_back'}:
        await state.update_data(return_to=target)
    try:
        if action in {'nodes', 'node', 'providers', 'provider', 'settings', 'keys', 'jobs', 'tests', 'reports', 'history', 'archive', 'notify'}:
            await state.clear()
        if action == 'home':
            await state.clear()
            await show(callback, tr('infra_title'), [(tr('infra_nodes'), 'infra:nodes'),
                (tr('infra_providers'), 'infra:providers'), (tr('infra_settings'), 'infra:settings'), (tr('infra_ssh_keys'), 'infra:keys'),
                (tr('infra_all_credentials'), 'infra:snapshot'), (tr('infra_notifications'), 'infra:notify'), (tr('infra_jobs'), 'infra:jobs'),
                (tr('infra_archive'), 'infra:archive'),
                (tr('infra_back'), 'admin_action:runtime_settings')])
        elif action in {'wizard_back', 'wizard_provider_back'}:
            await wizard_back(callback.message, state, service, session, action == 'wizard_provider_back')
        elif action == 'archive':
            await service.sync_removed_nodes(session)
            await session.commit()
            nodes = (await session.execute(select(Node).where(Node.panel_scope == service.scope,
                Node.deleted.is_(True)).order_by(Node.id.desc()))).scalars().all()
            await show(callback, tr('infra_archive_title'),
                [(n.name, f'infra:node:{n.id}') for n in nodes] + back())
        elif action == 'nodes':
            panel_nodes, _removed = await service.sync_removed_nodes(session)
            await session.commit()
            nodes = await service.nodes(session)
            buttons = [(n.name, f'infra:node:{n.id}') for n in nodes]
            known = {n.panel_uuid for n in nodes}
            for node in panel_nodes:
                if node['uuid'] not in known:
                    buttons.append((tr('infra_adopt', p0=node['name']), f"infra:adopt:{node['uuid']}"))
            await show(callback, tr('infra_nodes_title'), buttons + [(tr('infra_add_node'), 'infra:new')] + back())
        elif action == 'adopt':
            node = await service.adopt(session, argument)
            await session.commit()
            await card(callback, service, session, node.id)
        elif action == 'node':
            await card(callback, service, session, argument)
        elif action in {'new', 'replace'}:
            config = await read_config(session)
            if not config['profile_uuid'] or not config['inbound_uuid'] or not config['panel_ip']:
                raise ValueError(tr('infra_configure_first'))
            await state.clear()
            values = {'flow': 'wizard', 'phase': 'fields', 'profile_step': True, 'steps': ['address', 'ssh_port', 'ssh_user', 'password', 'host_address', 'country', 'days'], 'position': 0}
            if action == 'replace':
                node = await service.node(session, argument)
                deployment = await service.deployment(session, node.id)
                values.update(replace_id=node.id, country=node.country, provider_uuid=node.provider_uuid,
                              steps=['address', 'ssh_port', 'ssh_user', 'password', 'host_address', 'days'])
                if deployment:
                    values['previous'] = {'address': deployment.address, 'host_address': deployment.host_address,
                                          'ssh_port': deployment.ssh_port, 'ssh_user': deployment.ssh_user}
                values['previous_days'] = node.period_days
            await state.update_data(**values)
            await state.set_state(InfrastructureStates.input)
            await ask(callback.message, state, service, session)
        elif action in {'wizard_profiles', 'wizard_profile', 'wizard_inbound', 'wizard_profile_default'}:
            data = await state.get_data()
            if data.get('flow') != 'wizard' or data.get('phase') not in {'profile', 'profile_list', 'profile_inbounds'}:
                raise ValueError(tr('infra_form_closed'))
            BACK_TARGET.set('infra:wizard_back')
            if action == 'wizard_profiles':
                await state.update_data(phase='profile_list')
                profiles = await service.api.profiles()
                await show(callback, tr('infra_choose_profile'), [(p['name'], f"infra:wizard_profile:{p['uuid']}") for p in profiles] + back())
            else:
                profile_uuid = argument if action == 'wizard_profile' else data.get('profile_uuid')
                profile = next((p for p in await service.api.profiles() if p['uuid'] == profile_uuid), None)
                if not profile:
                    raise ValueError(tr('infra_profile_missing'))
                inbounds = [i for i in await service.panel.get_inbounds_by_profile_uuid(profile_uuid, refresh=True) or []
                    if str(i.get('port')) == '443' and i.get('type', '').lower() == 'vless']
                selected = argument if action == 'wizard_inbound' else data.get('inbound_uuid') if action == 'wizard_profile_default' else None
                inbound = next((i for i in inbounds if i['uuid'] == selected), None)
                if not inbound and len(inbounds) == 1 and action != 'wizard_inbound':
                    inbound = inbounds[0]
                if not inbounds or (action == 'wizard_inbound' and not inbound):
                    raise ValueError(tr('infra_no_vless_inbound'))
                await state.update_data(profile_uuid=profile_uuid, profile_name=profile['name'], phase='profile_inbounds')
                if inbound:
                    await state.update_data(inbound_uuid=inbound['uuid'])
                    await finish_wizard_profile(callback.message, state, service)
                else:
                    await show(callback, tr('infra_choose_inbound'), [(i['tag'], f"infra:wizard_inbound:{i['uuid']}") for i in inbounds] + back())
        elif action == 'keep':
            data = await state.get_data()
            if data.get('flow') != 'wizard' or data['position'] >= len(data['steps']):
                raise ValueError(tr('infra_form_closed'))
            field = data['steps'][data['position']]
            value = data.get(field)
            if value is None:
                raise ValueError(tr('infra_no_default'))
            if field == 'password':
                value = service.cipher().decrypt(value.encode()).decode()
            await advance(callback.message, state, service, session, str(value))
        elif action == 'default':
            data = await state.get_data()
            if data.get('flow') != 'wizard':
                raise ValueError(tr('infra_form_closed'))
            field = data['steps'][data['position']]
            defaults = {'ssh_port': '22', 'ssh_user': 'root', 'host_address': data.get('address', ''),
                        'days': str(data.get('previous_days') or (await read_config(session))['default_days'])}
            if field not in defaults:
                raise ValueError(tr('infra_no_default'))
            await advance(callback.message, state, service, session, defaults[field])
        elif action == 'pickprovider':
            data = await state.get_data()
            if data.get('flow') != 'wizard' or data['position'] != len(data['steps']):
                raise ValueError(tr('infra_form_closed'))
            if not any(p['uuid'] == argument for p in await service.api.providers()):
                raise ValueError(tr('infra_provider_not_found'))
            await state.update_data(provider_uuid=argument, phase='providers')
            await confirmation(callback.message, state, service)
        elif action == 'install':
            if await state.get_state() != InfrastructureStates.confirm.state:
                raise ValueError(tr('infra_install_form_closed'))
            data = await state.get_data()
            if data.get('flow') != 'wizard':
                raise ValueError(tr('infra_form_closed'))
            values = dict(data, period_days=data['days'], password=service.cipher().decrypt(data['password'].encode()).decode())
            node, job = await service.prepare(session, values, callback.from_user.id, data.get('replace_id'))
            await session.commit()
            await state.clear()
            BACK_TARGET.set('infra:nodes')
            await publish_job(service, job.id)
            await show(callback, tr('infra_operation_added', p0=html.escape(node.name)), back())
        elif action == 'providers':
            providers = await service.api.providers()
            await show(callback, tr('infra_providers'), [(p['name'], f"infra:provider:{p['uuid']}") for p in providers] +
                       [(tr('infra_add'), 'infra:addprovider')] + back())
        elif action == 'provider':
            provider = next((p for p in await service.api.providers() if p['uuid'] == argument), None)
            if not provider:
                raise ValueError(tr('infra_provider_not_found'))
            await show(callback, f"<b>{html.escape(provider['name'])}</b>\n{html.escape(provider.get('loginUrl') or '')}",
                [(tr('infra_edit_provider'), f'infra:provideredit:{argument}'),
                 (tr('infra_record_expense'), f'infra:payment:{argument}')] + back())
        elif action in {'addprovider', 'provideredit', 'payment'}:
            data = await state.get_data()
            await state.update_data(flow=action, provider=argument, resume_wizard=data if data.get('flow') == 'wizard' else None)
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer(tr('infra_ask_provider') if action != 'payment'
                else tr('infra_ask_amount'), reply_markup=keyboard(back()))
        elif action == 'settings':
            config = await read_config(session)
            await show(callback, tr('infra_settings_title'), [(f'{tr(label)}: {config[key]}', f'infra:setting:{key}') for key, label in LABELS.items()] +
                [(tr('infra_default_profile'), 'infra:profiles'), (tr('infra_tg_squad'), 'infra:squads'),
                 (tr('infra_sni_ru'), 'infra:pool:sni_ru'), (tr('infra_sni_other'), 'infra:pool:sni_other'),
                 (tr('infra_bridge_settings'), 'infra:bridge')] + back())
        elif action == 'setting':
            if argument not in LABELS:
                raise ValueError(tr('infra_setting_not_found'))
            if argument == 'expire_exclude_tg':
                config = await read_config(session)
                await save_config(session, argument, not config[argument], callback.from_user.id)
                await session.commit()
                await callback.message.answer(tr('infra_setting_saved'), reply_markup=keyboard(back()))
            else:
                await state.update_data(flow='setting', key=argument)
                await state.set_state(InfrastructureStates.input)
                await callback.message.answer(tr('infra_ask_setting', p0=tr(LABELS[argument])), reply_markup=keyboard(back()))
        elif action == 'profiles':
            profiles = await service.api.profiles()
            await show(callback, tr('infra_choose_profile'), [(p['name'], f"infra:profile:{p['uuid']}") for p in profiles] + back())
        elif action == 'profile':
            inbounds = await service.panel.get_inbounds_by_profile_uuid(argument, refresh=True)
            if not inbounds:
                raise ValueError(tr('infra_no_inbounds'))
            await state.update_data(profile=argument)
            await show(callback, tr('infra_choose_inbound'), [(i['tag'], f"infra:inbound:{i['uuid']}") for i in inbounds
                if str(i.get('port')) == '443' and i.get('type', '').lower() == 'vless'] + back())
        elif action == 'inbound':
            data = await state.get_data()
            profile = data.get('profile')
            entries = await service.panel.get_inbounds_by_profile_uuid(profile)
            if not any(i['uuid'] == argument for i in entries or []):
                raise ValueError(tr('infra_inbound_not_found'))
            await save_config(session, 'profile_uuid', profile, callback.from_user.id)
            await save_config(session, 'inbound_uuid', argument, callback.from_user.id)
            await session.commit()
            await state.clear()
            await callback.message.answer(tr('infra_profile_saved'), reply_markup=keyboard(back()))
        elif action == 'squads':
            result = await service.api.call('GET', '/internal-squads')
            await show(callback, tr('infra_choose_squad'), [(s['name'], f"infra:squad:{s['uuid']}") for s in result['internalSquads']] + back())
        elif action == 'squad':
            result = await service.api.call('GET', '/internal-squads')
            if not any(s['uuid'] == argument for s in result['internalSquads']):
                raise ValueError(tr('infra_squad_not_found'))
            await save_config(session, 'tg_squad_uuid', argument, callback.from_user.id)
            await session.commit()
            await callback.message.answer(tr('infra_squad_saved'), reply_markup=keyboard(back()))
        elif action == 'pool':
            if argument not in {'sni_ru', 'sni_other'}:
                raise ValueError(tr('infra_list_not_found'))
            config = await read_config(session)
            await state.update_data(flow='pool', key=argument)
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer(tr('infra_current_pool') + '\n'.join(html.escape(v) for v in config[argument]) +
                tr('infra_ask_pool'),
                reply_markup=keyboard(back()))
        elif action == 'bridge':
            config = await read_config(session)
            status = tr('infra_bridge_pinned' if config['bridge_host_public_key'] else 'infra_bridge_not_pinned')
            await show(callback, tr('infra_bridge_title') + status, [
                (f"{tr('infra_bridge_address')}: {html.escape(config['bridge_address'] or '—')}", 'infra:bridge_setting:bridge_address'),
                (f"{tr('infra_bridge_port')}: {config['bridge_ssh_port']}", 'infra:bridge_setting:bridge_ssh_port'),
                (f"{tr('infra_bridge_user')}: {html.escape(config['bridge_ssh_user'])}", 'infra:bridge_setting:bridge_ssh_user'),
                (tr('infra_bridge_key'), 'infra:bridge_key'),
                (tr('infra_bridge_test'), 'infra:bridge_test')] + back())
        elif action == 'bridge_setting':
            if argument not in {'bridge_address', 'bridge_ssh_port', 'bridge_ssh_user'}:
                raise ValueError(tr('infra_setting_not_found'))
            labels = {'bridge_address': tr('infra_bridge_address'),
                      'bridge_ssh_port': tr('infra_bridge_port'),
                      'bridge_ssh_user': tr('infra_bridge_user')}
            await state.update_data(flow='bridge_setting', key=argument)
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer(tr('infra_ask_setting', p0=labels[argument]), reply_markup=keyboard(back()))
        elif action == 'bridge_key':
            if not service.settings.INFRA_SSH_PRIVATE_KEY:
                raise ValueError(tr('infra_bridge_unconfigured'))
            key = asyncssh.read_private_key(service.settings.INFRA_SSH_PRIVATE_KEY)
            await callback.message.answer(tr('infra_bridge_key_text')
                + f'\n<code>{html.escape(key.export_public_key().decode().strip())}</code>',
                reply_markup=keyboard(back()))
        elif action == 'bridge_test':
            config = await read_config(session)
            if not config['bridge_address'] or not service.settings.INFRA_SSH_PRIVATE_KEY:
                raise ValueError(tr('infra_bridge_unconfigured'))
            public, fingerprint = await host_key(config['bridge_address'], int(config['bridge_ssh_port']))
            async with asyncssh.connect(host=config['bridge_address'], port=int(config['bridge_ssh_port']),
                    username=config['bridge_ssh_user'], client_keys=[service.settings.INFRA_SSH_PRIVATE_KEY],
                    known_hosts=([asyncssh.import_public_key(public)], [], []),
                    connect_timeout=20, login_timeout=20):
                pass
            await save_config(session, 'bridge_host_public_key', public, callback.from_user.id)
            await session.commit()
            await callback.message.answer(tr('infra_bridge_verified', p0=html.escape(fingerprint)), reply_markup=keyboard(back()))
        elif action == 'keys':
            config = await read_config(session)
            await show(callback, tr('infra_keys_title') + '\n'.join(html.escape(k['name']) for k in config['ssh_keys']),
                [(tr('infra_add_key'), 'infra:addkey'), (tr('infra_sync_keys'), 'infra:sync_keys')] + back())
        elif action == 'addkey':
            await state.update_data(flow='addkey')
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer(tr('infra_ask_key'), reply_markup=keyboard(back()))
        elif action == 'sync_keys':
            count, waiting = await service.schedule_key_updates(session, callback.from_user.id)
            await session.commit()
            await callback.message.answer(tr('infra_keys_scheduled', p0=count, p1=html.escape(', '.join(waiting)) or '—'), reply_markup=keyboard(back()))
        elif action == 'notify':
            key = f'infra.notify.{callback.from_user.id}'
            row = await app_settings_dal.get_setting(session, key)
            enabled = not row or row.value != 'false'
            await show(callback, tr('infra_notification_status', p0=tr('infra_enabled') if enabled else tr('infra_disabled_value')), [(tr('infra_toggle'), 'infra:toggle_notify')] + back())
        elif action == 'toggle_notify':
            key = f'infra.notify.{callback.from_user.id}'
            row = await app_settings_dal.get_setting(session, key)
            await app_settings_dal.upsert_setting(session, key, 'true' if row and row.value == 'false' else 'false')
            await session.commit()
            await callback.message.answer(tr('infra_notification_saved'), reply_markup=keyboard(back()))
        elif action == 'snapshot':
            await service.publish_snapshots(all_admins=False, admin_id=callback.from_user.id, force_new=True)
        elif action == 'credentials':
            node = await service.node(session, argument, include_deleted=True)
            BACK_TARGET.set(f'infra:node:{node.id}')
            for content in await service.snapshot(session, int(argument), admin_id=callback.from_user.id):
                await callback.message.answer(content, reply_markup=keyboard(back()))
        elif action in {'extend', 'access', 'edit', 'sni', 'bridge_create'}:
            node = await service.node(session, argument)
            if action == 'bridge_create' and not await service.deployment(session, node.id):
                raise ValueError(tr('infra_attach_access_first'))
            await state.update_data(flow=action, node_id=node.id)
            await state.set_state(InfrastructureStates.input)
            prompt = {'extend': tr('infra_ask_extension', p0=node.period_days),
                'access': tr('infra_ask_ssh_only'),
                'edit': tr('infra_ask_host_only'),
                'sni': tr('infra_ask_sni'),
                'bridge_create': tr('infra_ask_bridge_sni')}[action]
            await callback.message.answer(prompt, reply_markup=keyboard(back()))
        elif action == 'tests':
            await show(callback, tr('infra_test_levels'),
                [(tr('infra_small_test'), f'infra:test:{argument}:small'), (tr('infra_medium_test'), f'infra:test:{argument}:medium'),
                 (tr('infra_large_test'), f'infra:test:{argument}:large')] + back())
        elif action in {'upgrade', 'upgrade_confirm'}:
            node = await service.node(session, argument)
            await service.refresh_node(session, node)
            deployment = await service.deployment(session, node.id)
            current = next((n for n in await service.api.nodes() if n['uuid'] == node.panel_uuid), {})
            installed = (current.get('versions') or {}).get('node')
            config = await read_config(session)
            approved = config[f'node_version_{await service.panel.api_major()}']
            latest = await latest_node_version(approved)
            await session.commit()
            can_upgrade = bool(version_tuple(installed) and version_tuple(latest)
                               and version_tuple(installed) < version_tuple(latest))
            if action == 'upgrade_confirm':
                if not can_upgrade or len(parts) < 4 or parts[3] != latest:
                    raise ValueError(tr('infra_upgrade_changed'))
                if not deployment:
                    raise ValueError(tr('infra_save_access_first'))
                await service.validate_deployment(session, node, deployment)
                job = await service.enqueue(session, node, deployment, 'upgrade', callback.from_user.id)
                job.target_version = latest
                await session.commit()
                await publish_job(service, job.id)
                await show(callback, tr('infra_operation_added', p0=html.escape(node.name)), back())
            else:
                text_value = tr('infra_upgrade_details', p0=html.escape(node.name),
                    p1=html.escape(installed or tr('infra_version_unknown')),
                    p2=html.escape(latest or tr('infra_version_unavailable')), p3='.'.join(approved.split('.')[:2]))
                buttons = [(tr('infra_upgrade_confirm'), f'infra:upgrade_confirm:{node.id}:{latest}')] if can_upgrade and deployment else []
                if version_tuple(installed) and version_tuple(latest) and version_tuple(installed) >= version_tuple(latest):
                    text_value += '\n' + tr('infra_up_to_date')
                elif not installed:
                    text_value += '\n' + tr('infra_version_refresh_hint')
                buttons.append((tr('infra_refresh'), f'infra:upgrade:{node.id}'))
                await show(callback, text_value, buttons + back())
        elif action in {'test', 'restart', 'gemini_enable', 'gemini_disable'}:
            node = await service.node(session, argument)
            deployment = await service.deployment(session, node.id)
            if not deployment:
                if action == 'gemini_enable':
                    BACK_TARGET.set(f'infra:node:{node.id}')
                    await state.update_data(flow='access', node_id=node.id, resume_gemini=True)
                    await state.set_state(InfrastructureStates.input)
                    await callback.message.answer(tr('infra_ask_ssh_only'), reply_markup=keyboard(back()))
                    return
                raise ValueError(tr('infra_save_access_first'))
            await service.validate_deployment(session, node, deployment)
            kind = parts[3] if action == 'test' else action
            if kind not in {'small', 'medium', 'large', 'restart', 'upgrade', 'gemini_enable', 'gemini_disable'}:
                raise ValueError(tr('infra_unknown_action'))
            job = await service.enqueue(session, node, deployment, kind, callback.from_user.id)
            await session.commit()
            await publish_job(service, job.id)
            await show(callback, tr('infra_operation_added', p0=html.escape(node.name)), back())
        elif action in {'jobs', 'history'}:
            page = max(0, int(argument or 0)) if action == 'jobs' else max(0, int(parts[3]) if len(parts) > 3 else 0)
            query = select(Job, Node).join(Node).where(Node.panel_scope == service.scope)
            if action == 'jobs':
                query = query.where(Job.status.in_(['pending', 'running'])).order_by(Job.id)
            else:
                node = await service.node(session, argument, include_deleted=True)
                query = query.where(Job.node_id == node.id).order_by(Job.id.desc())
                BACK_TARGET.set(f'infra:node:{node.id}')
            rows = (await session.execute(query.offset(page * 6).limit(7))).all()
            buttons = []
            waiting_order = {}
            if action == 'jobs':
                pending = (await session.execute(select(Job.id).join(Node).where(Node.panel_scope == service.scope,
                    Job.status == 'pending').order_by(Job.id))).scalars().all()
                waiting_order = {job_id: index + 1 for index, job_id in enumerate(pending)}
            for job, node in rows[:6]:
                prefix = '▶️' if job.status == 'running' else f'{waiting_order.get(job.id, 1)}.' if action == 'jobs' else ''
                label = f'{prefix} {operation_label(job, node, tr)}'.strip()
                if action == 'history':
                    label += ' · ' + status_label(job, tr)
                elif job.status == 'running' and job.stage.startswith('test:'):
                    progress = job.stage.split(':')
                    label += f' · {progress[1]}/{progress[2]}'
                context = f'queue:{page}' if action == 'jobs' else f'history:{argument}:{page}'
                buttons.append((label, f'infra:job:{job.id}:{context}'))
            base = 'infra:jobs' if action == 'jobs' else f'infra:history:{argument}'
            if page:
                buttons.append((tr('infra_page_previous'), f'{base}:{page - 1}'))
            if len(rows) > 6:
                buttons.append((tr('infra_page_next'), f'{base}:{page + 1}'))
            if action == 'jobs':
                buttons.append((tr('infra_refresh'), f'{base}:{page}'))
            title = tr('infra_queue_title') if action == 'jobs' else tr('infra_operation_history')
            await show(callback, title + ('\n' + tr('infra_queue_empty') if not rows else ''), buttons + back())
        elif action in {'job', 'cancel', 'retry'}:
            job = await session.get(Job, int(argument))
            if not job:
                raise ValueError(tr('infra_job_not_found'))
            node = await service.node(session, job.node_id, include_deleted=True)
            if action == 'cancel':
                if job.status not in {'pending', 'running'}:
                    raise ValueError(tr('infra_operation_finished'))
                if job.status == 'pending':
                    job.status = 'cancelled'
                    job.finished_at = datetime.now(timezone.utc)
                else:
                    job.cancel_requested = True
                await session.commit()
            elif action == 'retry':
                if job.kind == 'upgrade':
                    raise ValueError(tr('infra_upgrade_changed'))
                if job.status not in {'failed', 'interrupted', 'cancelled'} and not (job.status == 'done' and job.kind in {'small', 'medium', 'large', 'restart', 'keys'}):
                    raise ValueError(tr('infra_cannot_retry'))
                if node.deleted:
                    raise ValueError(tr('infra_operation_finished'))
                deployment = await session.get(Deployment, job.deployment_id)
                if job.kind != 'provision':
                    if deployment.state != 'active':
                        raise ValueError(tr('infra_deployment_replaced'))
                    await service.validate_deployment(session, node, deployment)
                new_job = await service.enqueue(session, node, deployment, job.kind, callback.from_user.id)
                new_job.payload = job.payload
                if job.kind in {'medium', 'large'} and job.status != 'done':
                    source = Path(settings.INFRA_ARTIFACT_DIR) / 'reports' / str(job.id) / 'results.json'
                    if source.exists():
                        import shutil
                        target = source.parent.parent / str(new_job.id)
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source, target / 'results.json')
                await session.commit()
                await publish_job(service, new_job.id)
                await show(callback, tr('infra_operation_added', p0=html.escape(node.name)), back())
            else:
                report = (await session.execute(select(Report).where(Report.job_id == job.id))).scalar_one_or_none()
                markup = operation_buttons(job, tr, report.id if report else None)
                buttons = [(row[0].text, row[0].callback_data) for row in markup.inline_keyboard]
                await show(callback, operation_text(job, node, tr), buttons + back())
            if action == 'cancel':
                await publish_job(service, job.id, complete=job.status == 'cancelled')
                await show(callback, tr('infra_cancel_requested'), back())
        elif action == 'reports':
            node = await service.node(session, argument, include_deleted=True)
            reports = (await session.execute(select(Report, Deployment.address).join(Deployment).where(
                Deployment.node_id == node.id).order_by(Report.id.desc()).limit(30))).all()
            BACK_TARGET.set('infra:archive' if node.deleted else f'infra:node:{node.id}')
            await show(callback, tr('infra_test_history'), [(f'{r.created_at:%d.%m %H:%M} {address} {r.level}', f'infra:report:{r.id}')
                for r, address in reports] + back())
        elif action in {'report', 'report_raw'}:
            report = await session.get(Report, int(argument))
            if not report:
                raise ValueError(tr('infra_report_not_found'))
            deployment = await session.get(Deployment, report.deployment_id)
            node = await service.node(session, deployment.node_id, include_deleted=True)
            BACK_TARGET.set(f'infra:reports:{deployment.node_id}')
            manifest = json.loads(report.manifest)
            directory = Path(settings.INFRA_ARTIFACT_DIR) / 'reports' / manifest['directory']
            if action == 'report_raw':
                await service.bot.send_document(callback.from_user.id, types.FSInputFile(directory / 'results.json'))
            else:
                if manifest.get('renderer_version') != 3:
                    source = directory / 'results.json'
                    if not source.exists():
                        raise ValueError(tr('infra_report_source_missing'))
                    saved = json.loads(source.read_text())
                    if isinstance(saved, list):
                        saved = {'node': node.name, 'address': deployment.address,
                            'created_at': report.created_at.isoformat(), 'level': report.level, 'results': saved}
                    await callback.message.answer(tr('infra_rendering_saved'))
                    from bot.services.infrastructure.reports import render_reports
                    manifest['files'] = await asyncio.to_thread(render_reports, directory, saved)
                    manifest['renderer_version'] = 3
                    report.manifest = json.dumps(manifest)
                    await session.commit()
                await send_artifacts(service, callback.from_user.id, directory, manifest['files'], report.level)
                await callback.message.answer(tr('infra_raw_saved'), reply_markup=keyboard([
                    (tr('infra_download_json'), f'infra:report_raw:{report.id}')] + back()))
        elif action == 'delete':
            node = await service.node(session, argument)
            await show(callback, tr('infra_confirm_delete', p0=html.escape(node.name)),
                [(tr('infra_delete_confirm'), f'infra:delete_confirm:{node.id}')] + back())
        elif action == 'delete_confirm':
            node = await service.node(session, argument)
            await service.delete(session, node)
            await session.commit()
            await service.publish_snapshots()
            await show(callback, tr('infra_deleted'), back())
    except (ValueError, RuntimeError, OSError, TimeoutError, asyncssh.Error) as exc:
        await session.rollback()
        safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else tr('infra_ssh_timeout')
        await callback.message.answer(html.escape(localize_error(safe)), reply_markup=keyboard(back()))


@router.message(InfrastructureStates.input)
async def infrastructure_input(message: types.Message, state: FSMContext, session, infrastructure_service, settings, i18n_data=None):
    bind_language(i18n_data, settings, message)
    if message.from_user.id not in settings.ADMIN_IDS or message.chat.type != 'private':
        return
    if not settings.INFRA_ENABLED:
        await state.clear()
        await message.answer(tr('infra_disabled'))
        return
    service = infrastructure_service
    data = await state.get_data()
    value = message.text or ''
    BACK_TARGET.set(data.get('return_to') or ('infra:wizard_back' if data.get('flow') == 'wizard' else 'infra:home'))
    try:
        flow = data.get('flow')
        if flow == 'wizard':
            await advance(message, state, service, session, value.strip())
            return
        if flow in {'addprovider', 'provideredit'}:
            name, separator, url = value.strip().partition('\n')
            name, url = name.strip(), url.strip()
            from urllib.parse import urlsplit
            if len(name) < 2 or len(name) > 30 or (url and urlsplit(url).scheme not in {'http', 'https'}):
                raise ValueError(tr('infra_invalid_provider'))
            payload = {'name': name}
            if url:
                payload['loginUrl'] = url
            if flow == 'provideredit':
                payload['uuid'] = data['provider']
            result = await service.api.call('PATCH' if flow == 'provideredit' else 'POST', '/infra-billing/providers', payload)
            if data.get('resume_wizard'):
                wizard = data['resume_wizard']
                result = (result.get('provider') or result) if isinstance(result, dict) else {}
                if not result.get('uuid'):
                    raise RuntimeError(tr('infra_provider_response_invalid'))
                wizard = dict(wizard, provider_uuid=result['uuid'], phase='providers')
                await state.clear(); await state.set_data(wizard)
                await state.set_state(InfrastructureStates.input)
                await confirmation(message, state, service)
                return
        elif flow == 'payment':
            amount = float(value)
            if not 0 <= amount <= 100000000 or amount != amount:
                raise ValueError(tr('infra_invalid_amount'))
            await service.api.call('POST', '/infra-billing/history', {'providerUuid': data['provider'],
                'amount': amount, 'billedAt': datetime.now(timezone.utc).isoformat()})
        elif flow == 'setting':
            key = data['key']
            if key == 'warning_hours':
                parsed = sorted(set(int(v) for v in re.split(r'[\s,]+', value.strip())), reverse=True)
                if not parsed or any(v < 0 or v > 8760 for v in parsed):
                    raise ValueError(tr('infra_invalid_hours'))
            elif key in {'default_days', 'large_repeats'}:
                parsed = int(value)
                if not 1 <= parsed <= (10 if key == 'large_repeats' else 3650):
                    raise ValueError(tr('infra_out_of_range'))
            elif key == 'panel_ip':
                parsed = str(ipaddress.ip_address(value.strip()))
            else:
                parsed = value.strip()
                if not re.fullmatch(r'\d+\.\d+\.\d+', parsed):
                    raise ValueError(tr('infra_invalid_version'))
            await save_config(session, key, parsed, message.from_user.id)
        elif flow == 'bridge_setting':
            key = data['key']
            if key == 'bridge_address':
                parsed = validate_address(value)
                config = await read_config(session)
                if parsed != config['bridge_address']:
                    # A pinned host key belongs to the previous server only.
                    await save_config(session, 'bridge_host_public_key', '', message.from_user.id)
            elif key == 'bridge_ssh_port':
                parsed = int(value)
                if not 1 <= parsed <= 65535:
                    raise ValueError(tr('infra_invalid_port'))
            else:
                parsed = value.strip()
                if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', parsed):
                    raise ValueError(tr('infra_invalid_user'))
                if key == 'bridge_ssh_user' and parsed != 'root':
                    # The bridge playbook runs become tasks without a become password.
                    raise ValueError(tr('infra_root_required'))
            await save_config(session, key, parsed, message.from_user.id)
        elif flow == 'pool':
            entries = [] if value.strip() == '-' else list(dict.fromkeys(validate_hostname(v) for v in value.splitlines() if v.strip()))
            if len(entries) > 200:
                raise ValueError(tr('infra_sni_limit'))
            await save_config(session, data['key'], entries, message.from_user.id)
        elif flow == 'addkey':
            name, separator, public = value.strip().partition('\n')
            if not separator or not name or len(name) > 64:
                raise ValueError(tr('infra_invalid_key_input'))
            key = asyncssh.import_public_key(public)
            public = key.export_public_key().decode().strip()
            await session.execute(text('SELECT pg_advisory_xact_lock(74120303)'))
            config = await read_config(session)
            if any(k['key'] == public for k in config['ssh_keys']):
                raise ValueError(tr('infra_duplicate_key'))
            config['ssh_keys'].append({'name': name, 'key': public})
            await save_config(session, 'ssh_keys', config['ssh_keys'], message.from_user.id)
            await service.schedule_key_updates(session, message.from_user.id)
        elif flow == 'extend':
            parts = value.split()
            if len(parts) not in {1, 2}:
                raise ValueError(tr('infra_invalid_extension'))
            amount = float(parts[1]) if len(parts) == 2 else None
            if amount is not None and not 0 <= amount <= 100000000:
                raise ValueError(tr('infra_invalid_amount'))
            node = await service.node(session, data['node_id'])
            deadline = await service.extend(session, node, int(parts[0]), amount)
            await message.answer(tr('infra_extended_until', p0=f'{deadline:%d.%m.%Y %H:%M UTC}'))
        elif flow == 'access':
            entries = value.splitlines()
            if len(entries) != 4:
                raise ValueError(tr('infra_four_access_lines'))
            address, port, user, password = entries
            node = await service.node(session, data['node_id'])
            deployment, fingerprint = await service.save_access(session, node, address, int(port), user, password)
            await session.commit()
            await service.publish_snapshots()
            await message.answer(tr('infra_credentials_saved', p0=html.escape(fingerprint)))
            if data.get('resume_gemini'):
                job = await service.enqueue(session, node, deployment, 'gemini_enable', message.from_user.id)
                await session.commit()
                await publish_job(service, job.id)
                await message.answer(tr('infra_operation_added', p0=html.escape(node.name)))
        elif flow == 'edit':
            node = await service.node(session, data['node_id'])
            await service.refresh_node(session, node)
            if not node.host_uuid:
                raise ValueError(tr('infra_ambiguous_host'))
            address = validate_address(value)
            await service.api.call('PATCH', '/hosts', {'uuid': node.host_uuid, 'address': address})
            deployment = await service.deployment(session, node.id)
            if deployment:
                deployment.host_address = address
        elif flow == 'sni':
            sni = validate_hostname(value)
            node = await service.node(session, data['node_id'])
            deployment = await service.deployment(session, node.id)
            if not deployment:
                raise ValueError(tr('infra_attach_access_first'))
            await service.validate_deployment(session, node, deployment)
            inbounds = await service.panel.get_inbounds_by_profile_uuid(deployment.profile_uuid, refresh=True)
            inbound = next((i for i in inbounds or [] if i['uuid'] == deployment.inbound_uuid), None)
            if not inbound:
                raise ValueError(tr('infra_inbound_unavailable'))
            raw = (inbound or {}).get('rawInbound') or {}
            if isinstance(raw, str):
                raw = json.loads(raw)
            reality = (raw.get('streamSettings') or {}).get('realitySettings')
            if reality and sni not in reality.get('serverNames', []):
                raise ValueError(tr('infra_sni_not_allowed'))
            await service.api.call('PATCH', '/hosts', {'uuid': node.host_uuid, 'sni': sni})
            deployment.sni = sni
        elif flow == 'bridge_create':
            sni = validate_hostname(value)
            node = await service.node(session, data['node_id'])
            deployment = await service.deployment(session, node.id)
            if not deployment:
                raise ValueError(tr('infra_attach_access_first'))
            config = await read_config(session)
            if not config['bridge_address'] or not config['bridge_host_public_key']:
                raise ValueError(tr('infra_bridge_unconfigured'))
            await service.validate_deployment(session, node, deployment)
            job = await service.enqueue(session, node, deployment, 'bridge_create', message.from_user.id)
            job.payload = json.dumps({'sni': sni})
            await session.commit()
            await publish_job(service, job.id)
            await message.answer(tr('infra_operation_added', p0=html.escape(node.name)))
        else:
            raise ValueError(tr('infra_form_closed'))
        await session.commit()
        await state.clear()
        await message.answer(tr('infra_saved'), reply_markup=keyboard(back()))
    except (ValueError, RuntimeError, OSError, TimeoutError, asyncssh.Error) as exc:
        await session.rollback()
        safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else tr('infra_ssh_error')
        await message.answer(html.escape(localize_error(safe)), reply_markup=keyboard(back()))
