"""Private administrator UI for infrastructure. No secrets in callback payloads."""
import html
import ipaddress
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import asyncssh
from aiogram import F, Router, types
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, text

from db.models import InfrastructureNode as Node, InfrastructureDeployment as Deployment, InfrastructureJob as Job, InfrastructureReport as Report
from db.dal import app_settings_dal
from bot.services.infrastructure.settings import read_config, save_config, validate_address, validate_hostname
from bot.services.infrastructure.provisioning import host_key
from bot.services.infrastructure.diagnostics import send_artifacts

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


def back():
    return [('Назад', 'infra:home')]


async def show(callback, content, buttons):
    await callback.message.edit_text(content, reply_markup=keyboard(buttons))


async def card(callback, service, session, node_id):
    node = await service.node(session, node_id)
    deployment = await service.deployment(session, node.id)
    text_value = f'<b>{html.escape(node.name)}</b>\nПериод продления: {node.period_days} дней'
    provider = next((p for p in await service.api.providers() if p['uuid'] == node.provider_uuid), None)
    if provider:
        text_value += '\nПровайдер: ' + html.escape(provider['name'])
    if deployment:
        text_value += f'\nАдрес: <code>{html.escape(deployment.address)}</code>\nHost: <code>{html.escape(deployment.host_address)}</code>\nВерсия ноды: {html.escape(deployment.node_version)}'
    billing = await service.api.billing()
    entry = next((b for b in billing.get('billingNodes', []) if b['uuid'] == node.billing_uuid), None)
    if entry:
        text_value += f"\nОплачено до: {html.escape(entry['nextBillingAt'])}"
    text_value += '\nДоступ tg: ' + ('исключён автоматикой' if node.excluded_tg else 'без автоматического исключения')
    buttons = [('Продлить', f'infra:extend:{node.id}'), ('Заменить VPS', f'infra:replace:{node.id}'),
               ('SSH-доступ', f'infra:credentials:{node.id}'), ('Диагностика', f'infra:tests:{node.id}'),
               ('История тестов', f'infra:reports:{node.id}'), ('Перезапустить ноду', f'infra:restart:{node.id}'),
               ('Обновить ПО ноды', f'infra:upgrade:{node.id}'),
               ('Редактировать доступ / host', f'infra:edit:{node.id}'),
               ('Изменить SNI host', f'infra:sni:{node.id}'), ('Удалить', f'infra:delete:{node.id}')] + back()
    await show(callback, text_value, buttons)


LABELS = {
    'default_days': 'Начальный срок, дней', 'warning_hours': 'Напоминания, часов до оплаты',
    'expire_exclude_tg': 'Исключать из tg после срока', 'panel_ip': 'IP сервера панели',
    'large_repeats': 'Повторы большого теста', 'node_version_2': 'Версия ноды для панели 2.x',
    'node_version_3': 'Версия ноды для панели 3.x',
}


async def ask(message, state, service, session):
    data = await state.get_data()
    step = data['steps'][data['position']]
    defaults = {'ssh_port': '22', 'ssh_user': 'root', 'host_address': data.get('address', ''),
                'days': str(data.get('previous_days') or (await read_config(session))['default_days'])}
    prompts = {'address': 'Введите IP или адрес сервера.', 'ssh_port': 'Введите SSH-порт.',
        'ssh_user': 'Введите SSH-логин.', 'password': 'Введите пароль SSH. Он будет сохранён и доступен администраторам.',
        'host_address': 'Введите адрес host для клиентов.', 'country': 'Введите код страны, например RU, DE, GE или NL.',
        'days': 'На сколько дней куплен сервер?'}
    previous = data.get('previous', {})
    prompt = prompts[step]
    if step in previous:
        prompt += f'\nПредыдущее значение: <code>{html.escape(str(previous[step]))}</code>'
    buttons = []
    if step in defaults:
        buttons.append((f"По умолчанию: {defaults[step]}", 'infra:default'))
    buttons += [('Отмена', 'infra:home')]
    await message.answer(prompt, reply_markup=keyboard(buttons))


async def advance(message, state, service, session, value):
    data = await state.get_data()
    field = data['steps'][data['position']]
    if field in {'address', 'host_address'}:
        value = validate_address(value)
    elif field == 'ssh_port':
        value = int(value)
        if not 1 <= value <= 65535:
            raise ValueError('Порт: 1–65535')
    elif field == 'ssh_user':
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value):
            raise ValueError('Некорректный SSH-логин')
        if value != 'root':
            raise ValueError('В первой версии установки используйте root')
    elif field == 'password':
        if not value or len(value) > 512 or '\n' in value:
            raise ValueError('Введите пароль одной строкой, до 512 символов')
        value = service.cipher().encrypt(value.encode()).decode()
    elif field == 'country':
        value = value.upper()
        if not re.fullmatch(r'[A-Z]{2}', value):
            raise ValueError('Нужен двухбуквенный код страны')
    elif field == 'days':
        value = int(value)
        if not 1 <= value <= 3650:
            raise ValueError('Срок: 1–3650 дней')
    await state.update_data(**{field: value, 'position': data['position'] + 1})
    if data['position'] + 1 < len(data['steps']):
        await ask(message, state, service, session)
    else:
        data = await state.get_data()
        if data.get('replace_id'):
            await confirmation(message, state, service)
        else:
            providers = await service.api.providers()
            await message.answer('Выберите провайдера.', reply_markup=keyboard(
                [(p['name'], f"infra:pickprovider:{p['uuid']}") for p in providers] +
                [('Добавить провайдера', 'infra:addprovider'), ('Отмена', 'infra:home')]))


async def confirmation(message, state, service):
    data = await state.get_data()
    public, fingerprint = await host_key(data['address'], int(data['ssh_port']))
    await state.update_data(host_public_key=public)
    await state.set_state(InfrastructureStates.confirm)
    await message.answer(f"<b>Установка ноды</b>\nАдрес: {html.escape(data['address'])}\nHost: {html.escape(data['host_address'])}\n"
        f"Страна: {data['country']}\nСрок: {data['days']} дней\nSSH-ключ сервера: <code>{html.escape(fingerprint)}</code>\n"
        'Проверьте адрес и отпечаток. Будут настроены Docker, SSH и firewall.',
        reply_markup=keyboard([('Установить', 'infra:install'), ('Отмена', 'infra:home')]))


@router.callback_query(F.data.startswith('infra:'))
async def infrastructure_callback(callback: types.CallbackQuery, state: FSMContext, session,
                                  infrastructure_service, settings):
    service = infrastructure_service
    if callback.from_user.id not in settings.ADMIN_IDS or callback.message.chat.type != 'private':
        await callback.answer('Нет доступа', show_alert=True); return
    if not settings.INFRA_ENABLED:
        await callback.answer('Инфраструктура выключена в конфигурации', show_alert=True); return
    await callback.answer()
    parts = callback.data.split(':')
    action = parts[1]
    argument = parts[2] if len(parts) > 2 else None
    try:
        if action == 'home':
            await state.clear()
            await show(callback, '<b>Инфраструктура</b>', [('Ноды', 'infra:nodes'), ('Добавить ноду', 'infra:new'),
                ('Провайдеры', 'infra:providers'), ('Настройки', 'infra:settings'), ('SSH-ключи', 'infra:keys'),
                ('Все доступы', 'infra:snapshot'), ('Уведомления', 'infra:notify'), ('Задания', 'infra:jobs'),
                ('Админ-панель', 'admin_action:main')])
        elif action == 'nodes':
            nodes = await service.nodes(session)
            buttons = [(n.name, f'infra:node:{n.id}') for n in nodes]
            known = {n.panel_uuid for n in nodes}
            for node in await service.api.nodes():
                if node['uuid'] not in known:
                    buttons.append((f"Подключить: {node['name']}", f"infra:adopt:{node['uuid']}"))
            await show(callback, 'Ноды панели и бота', buttons + back())
        elif action == 'adopt':
            node = await service.adopt(session, argument)
            await session.commit()
            await card(callback, service, session, node.id)
        elif action == 'node':
            await card(callback, service, session, argument)
        elif action in {'new', 'replace'}:
            config = await read_config(session)
            if not config['profile_uuid'] or not config['inbound_uuid'] or not config['panel_ip']:
                raise ValueError('Сначала выберите профиль, inbound и IP панели в настройках')
            await state.clear()
            values = {'flow': 'wizard', 'steps': ['address', 'ssh_port', 'ssh_user', 'password', 'host_address', 'country', 'days'], 'position': 0}
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
        elif action == 'default':
            data = await state.get_data()
            if data.get('flow') != 'wizard':
                raise ValueError('Форма закрыта')
            field = data['steps'][data['position']]
            defaults = {'ssh_port': '22', 'ssh_user': 'root', 'host_address': data.get('address', ''),
                        'days': str(data.get('previous_days') or (await read_config(session))['default_days'])}
            if field not in defaults:
                raise ValueError('Нет значения по умолчанию')
            await advance(callback.message, state, service, session, defaults[field])
        elif action == 'pickprovider':
            data = await state.get_data()
            if data.get('flow') != 'wizard' or data['position'] != len(data['steps']):
                raise ValueError('Форма закрыта')
            if not any(p['uuid'] == argument for p in await service.api.providers()):
                raise ValueError('Провайдер не найден')
            await state.update_data(provider_uuid=argument)
            await confirmation(callback.message, state, service)
        elif action == 'install':
            if await state.get_state() != InfrastructureStates.confirm.state:
                raise ValueError('Форма установки закрыта')
            data = await state.get_data()
            if data.get('flow') != 'wizard':
                raise ValueError('Форма закрыта')
            values = dict(data, period_days=data['days'], password=service.cipher().decrypt(data['password'].encode()).decode())
            node, job = await service.prepare(session, values, callback.from_user.id, data.get('replace_id'))
            await session.commit()
            await state.clear()
            await show(callback, f'{html.escape(node.name)}: задание #{job.id} в очереди.', back())
        elif action == 'providers':
            providers = await service.api.providers()
            await show(callback, 'Провайдеры', [(p['name'], f"infra:provider:{p['uuid']}") for p in providers] +
                       [('Добавить', 'infra:addprovider')] + back())
        elif action == 'provider':
            provider = next((p for p in await service.api.providers() if p['uuid'] == argument), None)
            if not provider:
                raise ValueError('Провайдер не найден')
            await show(callback, f"<b>{html.escape(provider['name'])}</b>\n{html.escape(provider.get('loginUrl') or '')}",
                [('Изменить название/ссылку', f'infra:provideredit:{argument}'),
                 ('Записать расход', f'infra:payment:{argument}')] + back())
        elif action in {'addprovider', 'provideredit', 'payment'}:
            data = await state.get_data()
            await state.update_data(flow=action, provider=argument, resume_wizard=data if data.get('flow') == 'wizard' else None)
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer('Введите название провайдера первой строкой, ссылку на кабинет второй (необязательно).' if action != 'payment'
                else 'Введите сумму расхода (число).', reply_markup=keyboard(back()))
        elif action == 'settings':
            config = await read_config(session)
            await show(callback, 'Настройки инфраструктуры', [(f'{label}: {config[key]}', f'infra:setting:{key}') for key, label in LABELS.items()] +
                [('Профиль по умолчанию', 'infra:profiles'), ('Squad tg', 'infra:squads'),
                 ('Список SNI РФ', 'infra:pool:sni_ru'), ('Список SNI не РФ', 'infra:pool:sni_other')] + back())
        elif action == 'setting':
            if argument not in LABELS:
                raise ValueError('Настройка не найдена')
            if argument == 'expire_exclude_tg':
                config = await read_config(session)
                await save_config(session, argument, not config[argument], callback.from_user.id)
                await session.commit()
                await callback.message.answer('Настройка сохранена.', reply_markup=keyboard(back()))
            else:
                await state.update_data(flow='setting', key=argument)
                await state.set_state(InfrastructureStates.input)
                await callback.message.answer(f'Введите значение: {LABELS[argument]}.', reply_markup=keyboard(back()))
        elif action == 'profiles':
            profiles = await service.api.profiles()
            await show(callback, 'Выберите профиль', [(p['name'], f"infra:profile:{p['uuid']}") for p in profiles] + back())
        elif action == 'profile':
            inbounds = await service.panel.get_inbounds_by_profile_uuid(argument, refresh=True)
            if not inbounds:
                raise ValueError('Нет inbound в профиле')
            await state.update_data(profile=argument)
            await show(callback, 'Выберите VLESS inbound 443', [(i['tag'], f"infra:inbound:{i['uuid']}") for i in inbounds
                if str(i.get('port')) == '443' and i.get('type', '').lower() == 'vless'] + back())
        elif action == 'inbound':
            data = await state.get_data()
            profile = data.get('profile')
            entries = await service.panel.get_inbounds_by_profile_uuid(profile)
            if not any(i['uuid'] == argument for i in entries or []):
                raise ValueError('Inbound не найден')
            await save_config(session, 'profile_uuid', profile, callback.from_user.id)
            await save_config(session, 'inbound_uuid', argument, callback.from_user.id)
            await session.commit()
            await state.clear()
            await callback.message.answer('Профиль по умолчанию сохранён.', reply_markup=keyboard(back()))
        elif action == 'squads':
            result = await service.api.call('GET', '/internal-squads')
            await show(callback, 'Выберите squad tg', [(s['name'], f"infra:squad:{s['uuid']}") for s in result['internalSquads']] + back())
        elif action == 'squad':
            result = await service.api.call('GET', '/internal-squads')
            if not any(s['uuid'] == argument for s in result['internalSquads']):
                raise ValueError('Squad не найден')
            await save_config(session, 'tg_squad_uuid', argument, callback.from_user.id)
            await session.commit()
            await callback.message.answer('Squad сохранён.', reply_markup=keyboard(back()))
        elif action == 'pool':
            if argument not in {'sni_ru', 'sni_other'}:
                raise ValueError('Список не найден')
            config = await read_config(session)
            await state.update_data(flow='pool', key=argument)
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer('Текущий список:\n' + '\n'.join(html.escape(v) for v in config[argument]) +
                '\n\nОтправьте новый список доменов, каждый с новой строки. Так можно добавить, изменить или удалить записи. «-» очищает список.',
                reply_markup=keyboard(back()))
        elif action == 'keys':
            config = await read_config(session)
            await show(callback, 'Публичные SSH-ключи\n' + '\n'.join(html.escape(k['name']) for k in config['ssh_keys']),
                [('Добавить ключ', 'infra:addkey'), ('Распространить на все ноды', 'infra:sync_keys')] + back())
        elif action == 'addkey':
            await state.update_data(flow='addkey')
            await state.set_state(InfrastructureStates.input)
            await callback.message.answer('Введите название ключа первой строкой, публичный ключ второй строкой.', reply_markup=keyboard(back()))
        elif action == 'sync_keys':
            count, waiting = await service.schedule_key_updates(session, callback.from_user.id)
            await session.commit()
            await callback.message.answer(f'Обновление ключей запланировано для {count} нод.\n'
                f'Ожидают добавления SSH-доступов: {html.escape(", ".join(waiting)) or "—"}', reply_markup=keyboard(back()))
        elif action == 'notify':
            key = f'infra.notify.{callback.from_user.id}'
            row = await app_settings_dal.get_setting(session, key)
            enabled = not row or row.value != 'false'
            await show(callback, f'Уведомления: {"включены" if enabled else "выключены"}', [('Переключить', 'infra:toggle_notify')] + back())
        elif action == 'toggle_notify':
            key = f'infra.notify.{callback.from_user.id}'
            row = await app_settings_dal.get_setting(session, key)
            await app_settings_dal.upsert_setting(session, key, 'true' if row and row.value == 'false' else 'false')
            await session.commit()
            await callback.message.answer('Настройка уведомлений изменена.', reply_markup=keyboard(back()))
        elif action == 'snapshot':
            await service.publish_snapshots(all_admins=False, admin_id=callback.from_user.id)
        elif action == 'credentials':
            for content in await service.snapshot(session, int(argument)):
                await callback.message.answer(content)
        elif action in {'extend', 'edit', 'sni'}:
            node = await service.node(session, argument)
            await state.update_data(flow=action, node_id=node.id)
            await state.set_state(InfrastructureStates.input)
            prompt = {'extend': f'Введите дни продления (обычно {node.period_days}); можно добавить стоимость через пробел.',
                'edit': 'Введите адрес, SSH-порт, логин, пароль и адрес host: каждый с новой строки.',
                'sni': 'Введите новый SNI для host. Значение должно быть допустимо в конфиге сервера.'}[action]
            await callback.message.answer(prompt, reply_markup=keyboard(back()))
        elif action == 'tests':
            await show(callback, 'Средний: весь набор, один проход. Большой: расширенный набор с повторами.',
                [('Маленький', f'infra:test:{argument}:small'), ('Средний', f'infra:test:{argument}:medium'),
                 ('Большой', f'infra:test:{argument}:large')] + back())
        elif action in {'test', 'restart', 'upgrade'}:
            node = await service.node(session, argument)
            deployment = await service.deployment(session, node.id)
            if not deployment:
                raise ValueError('Сначала сохраните SSH-доступ для существующей ноды')
            kind = parts[3] if action == 'test' else action
            if kind not in {'small', 'medium', 'large', 'restart', 'upgrade'}:
                raise ValueError('Неизвестное действие')
            job = await service.enqueue(session, node, deployment, kind, callback.from_user.id)
            await session.commit()
            await callback.message.answer(f'Задание #{job.id} в очереди.', reply_markup=keyboard(back()))
        elif action == 'jobs':
            jobs = (await session.execute(select(Job).join(Node).where(Node.panel_scope == service.scope).order_by(Job.id.desc()).limit(30))).scalars().all()
            await show(callback, '\n'.join(f'#{j.id} {j.kind}: {j.status} / {html.escape(j.stage)}' for j in jobs) or 'Нет заданий',
                [(f'#{j.id}', f'infra:job:{j.id}') for j in jobs] + back())
        elif action in {'job', 'cancel', 'retry'}:
            job = await session.get(Job, int(argument))
            if not job:
                raise ValueError('Задание не найдено')
            node = await service.node(session, job.node_id)
            if action == 'cancel':
                if job.status == 'pending':
                    job.status = 'cancelled'
                else:
                    job.cancel_requested = True
                await session.commit()
            elif action == 'retry':
                if job.status not in {'failed', 'interrupted', 'cancelled'}:
                    raise ValueError('Это задание нельзя повторить')
                deployment = await session.get(Deployment, job.deployment_id)
                new_job = await service.enqueue(session, node, deployment, job.kind, callback.from_user.id)
                if job.kind in {'medium', 'large'}:
                    source = Path(settings.INFRA_ARTIFACT_DIR) / 'reports' / str(job.id) / 'results.json'
                    if source.exists():
                        import shutil
                        target = source.parent.parent / str(new_job.id)
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source, target / 'results.json')
                await session.commit()
                await callback.message.answer(f'Повтор: задание #{new_job.id}', reply_markup=keyboard(back()))
            else:
                buttons = [('Остановить', f'infra:cancel:{job.id}')] if job.status in {'pending', 'running'} else []
                if job.status in {'failed', 'interrupted', 'cancelled'}:
                    buttons.append(('Повторить', f'infra:retry:{job.id}'))
                await show(callback, f'#{job.id}: {job.status}\n{html.escape(job.stage)}\n{html.escape(job.result or "")}', buttons + back())
        elif action == 'reports':
            node = await service.node(session, argument)
            reports = (await session.execute(select(Report, Deployment.address).join(Deployment).where(
                Deployment.node_id == node.id).order_by(Report.id.desc()).limit(30))).all()
            await show(callback, 'История тестов', [(f'{r.created_at:%d.%m %H:%M} {address} {r.level}', f'infra:report:{r.id}')
                for r, address in reports] + back())
        elif action in {'report', 'report_raw'}:
            report = await session.get(Report, int(argument))
            if not report:
                raise ValueError('Отчёт не найден')
            deployment = await session.get(Deployment, report.deployment_id)
            await service.node(session, deployment.node_id)
            manifest = json.loads(report.manifest)
            directory = Path(settings.INFRA_ARTIFACT_DIR) / 'reports' / manifest['directory']
            if action == 'report_raw':
                await service.bot.send_document(callback.from_user.id, types.FSInputFile(directory / 'results.json'))
            else:
                await send_artifacts(service, callback.from_user.id, directory, manifest['files'], report.level)
                await callback.message.answer('Исходные результаты сохранены.', reply_markup=keyboard([
                    ('Скачать JSON', f'infra:report_raw:{report.id}')] + back()))
        elif action == 'delete':
            node = await service.node(session, argument)
            await show(callback, f'Удалить {html.escape(node.name)} из панели вместе со связанным host и биллингом?\nVPS у провайдера останется.',
                [('Подтвердить удаление', f'infra:delete_confirm:{node.id}')] + back())
        elif action == 'delete_confirm':
            node = await service.node(session, argument)
            await service.delete(session, node)
            await session.commit()
            await service.publish_snapshots()
            await show(callback, 'Объекты удалены. История тестов сохранена.', back())
    except (ValueError, RuntimeError) as exc:
        await session.rollback()
        await callback.message.answer(html.escape(str(exc)), reply_markup=keyboard(back()))


@router.message(InfrastructureStates.input)
async def infrastructure_input(message: types.Message, state: FSMContext, session, infrastructure_service, settings):
    if message.from_user.id not in settings.ADMIN_IDS or message.chat.type != 'private':
        return
    if not settings.INFRA_ENABLED:
        await state.clear()
        await message.answer('Инфраструктура выключена.')
        return
    service = infrastructure_service
    data = await state.get_data()
    value = message.text or ''
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
                raise ValueError('Название до 30 символов; ссылка http/https')
            payload = {'name': name}
            if url:
                payload['loginUrl'] = url
            if flow == 'provideredit':
                payload['uuid'] = data['provider']
            result = await service.api.call('PATCH' if flow == 'provideredit' else 'POST', '/infra-billing/providers', payload)
            if data.get('resume_wizard'):
                wizard = data['resume_wizard']
                await state.clear(); await state.update_data(**wizard, provider_uuid=result['uuid'])
                await confirmation(message, state, service)
                return
        elif flow == 'payment':
            amount = float(value)
            if not 0 <= amount <= 100000000 or amount != amount:
                raise ValueError('Некорректная сумма')
            await service.api.call('POST', '/infra-billing/history', {'providerUuid': data['provider'],
                'amount': amount, 'billedAt': datetime.now(timezone.utc).isoformat()})
        elif flow == 'setting':
            key = data['key']
            if key == 'warning_hours':
                parsed = sorted(set(int(v) for v in re.split(r'[\s,]+', value.strip())), reverse=True)
                if not parsed or any(v < 0 or v > 8760 for v in parsed):
                    raise ValueError('Укажите часы от 0 до 8760 через пробел')
            elif key in {'default_days', 'large_repeats'}:
                parsed = int(value)
                if not 1 <= parsed <= (10 if key == 'large_repeats' else 3650):
                    raise ValueError('Значение вне диапазона')
            elif key == 'panel_ip':
                parsed = str(ipaddress.ip_address(value.strip()))
            else:
                parsed = value.strip()
                if not re.fullmatch(r'\d+\.\d+\.\d+', parsed):
                    raise ValueError('Версия вида 3.4.2')
            await save_config(session, key, parsed, message.from_user.id)
        elif flow == 'pool':
            entries = [] if value.strip() == '-' else list(dict.fromkeys(validate_hostname(v) for v in value.splitlines() if v.strip()))
            if len(entries) > 200:
                raise ValueError('Максимум 200 SNI')
            await save_config(session, data['key'], entries, message.from_user.id)
        elif flow == 'addkey':
            name, separator, public = value.strip().partition('\n')
            if not separator or not name or len(name) > 64:
                raise ValueError('Введите название и публичный ключ двумя строками')
            key = asyncssh.import_public_key(public)
            public = key.export_public_key().decode().strip()
            await session.execute(text('SELECT pg_advisory_xact_lock(74120303)'))
            config = await read_config(session)
            if any(k['key'] == public for k in config['ssh_keys']):
                raise ValueError('Этот ключ уже добавлен')
            config['ssh_keys'].append({'name': name, 'key': public})
            await save_config(session, 'ssh_keys', config['ssh_keys'], message.from_user.id)
            await service.schedule_key_updates(session, message.from_user.id)
        elif flow == 'extend':
            parts = value.split()
            if len(parts) not in {1, 2}:
                raise ValueError('Дни и необязательная стоимость')
            amount = float(parts[1]) if len(parts) == 2 else None
            if amount is not None and not 0 <= amount <= 100000000:
                raise ValueError('Некорректная сумма')
            node = await service.node(session, data['node_id'])
            deadline = await service.extend(session, node, int(parts[0]), amount)
            await message.answer(f'Оплачено до {deadline:%d.%m.%Y %H:%M UTC}')
        elif flow == 'edit':
            entries = value.splitlines()
            if len(entries) != 5:
                raise ValueError('Нужно пять строк: адрес, порт, логин, пароль, адрес host')
            address, port, user, password, host_address = entries
            address, host_address = validate_address(address), validate_address(host_address)
            port = int(port)
            if not 1 <= port <= 65535 or user != 'root' or not password:
                raise ValueError('Проверьте порт, root-логин и пароль')
            node = await service.node(session, data['node_id'])
            deployment = await service.deployment(session, node.id)
            public, fingerprint = await host_key(address, port)
            async with asyncssh.connect(host=address, port=port, username=user, password=password,
                    client_keys=[], known_hosts=([asyncssh.import_public_key(public)], [], []),
                    connect_timeout=20, login_timeout=20):
                pass  # Verify the supplied password before replacing saved access.
            if not deployment:
                panel_node = await service.api.call('GET', f'/nodes/{node.panel_uuid}')
                cp = panel_node['configProfile']
                inbounds = cp['activeInbounds']
                if len(inbounds) != 1 or not node.host_uuid:
                    raise ValueError('Для подключения требуется один inbound и однозначно связанный host')
                billing = await service.api.billing()
                entry = next((b for b in billing.get('billingNodes', []) if b['uuid'] == node.billing_uuid), None)
                if not entry:
                    raise ValueError('Сначала подключите ноду к Infra Billing в панели')
                deployment = Deployment(node_id=node.id, state='active', address=address, host_address=host_address,
                    ssh_port=port, ssh_user=user, credentials=service.cipher().encrypt(password.encode()).decode(),
                    host_public_key=public, profile_uuid=cp['activeConfigProfileUuid'],
                    inbound_uuid=inbounds[0]['uuid'] if isinstance(inbounds[0], dict) else inbounds[0],
                    node_version=(panel_node.get('versions') or {}).get('node') or 'unknown',
                    paid_until=datetime.fromisoformat(entry['nextBillingAt'].replace('Z', '+00:00')))
                session.add(deployment)
            else:
                if address != deployment.address:
                    raise ValueError('Для нового сервера используйте «Заменить VPS», чтобы сохранить историю')
                deployment.ssh_port, deployment.ssh_user = port, user
                deployment.credentials = service.cipher().encrypt(password.encode()).decode()
                deployment.host_public_key, deployment.host_address = public, host_address
            await service.api.call('PATCH', '/hosts', {'uuid': node.host_uuid, 'address': host_address})
            await session.commit()
            await service.publish_snapshots()
            await message.answer(f'SSH-доступ сохранён. Отпечаток: <code>{html.escape(fingerprint)}</code>')
        elif flow == 'sni':
            sni = validate_hostname(value)
            node = await service.node(session, data['node_id'])
            deployment = await service.deployment(session, node.id)
            if not deployment:
                raise ValueError('Сначала подключите доступ к ноде')
            inbounds = await service.panel.get_inbounds_by_profile_uuid(deployment.profile_uuid, refresh=True)
            inbound = next((i for i in inbounds or [] if i['uuid'] == deployment.inbound_uuid), None)
            raw = (inbound or {}).get('rawInbound') or {}
            if isinstance(raw, str):
                raw = json.loads(raw)
            reality = (raw.get('streamSettings') or {}).get('realitySettings')
            import fnmatch
            if reality and not any(fnmatch.fnmatchcase(sni, name) for name in reality.get('serverNames', [])):
                raise ValueError('Этот SNI не разрешён выбранным REALITY inbound')
            await service.api.call('PATCH', '/hosts', {'uuid': node.host_uuid, 'sni': sni})
            deployment.sni = sni
        else:
            raise ValueError('Форма закрыта')
        await session.commit()
        await state.clear()
        await message.answer('Сохранено.', reply_markup=keyboard(back()))
    except (ValueError, RuntimeError, asyncssh.Error) as exc:
        await session.rollback()
        safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'Ошибка SSH. Проверьте адрес и доступ.'
        await message.answer(html.escape(safe), reply_markup=keyboard(back()))
