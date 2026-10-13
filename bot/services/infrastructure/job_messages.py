"""Readable operation messages; Telegram failures never undo infrastructure work."""
import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from db.models import InfrastructureJob as Job, InfrastructureNode as Node, InfrastructureReport as Report, User
from bot.middlewares.i18n import get_i18n_instance

KINDS = {'provision': 'install', 'keys': 'keys', 'upgrade': 'upgrade', 'restart': 'restart',
         'small': 'small', 'medium': 'medium', 'large': 'large'}
STAGES = {'queued': 'queued', 'ansible': 'install', 'panel_node': 'panel', 'connection_check': 'check',
          'host': 'host', 'billing': 'billing', 'ssh_keys': 'keys', 'verification': 'check',
          'dependencies': 'dependencies', 'complete': 'complete'}


def kind_label(job, translate):
    return translate('infra_operation_' + KINDS.get(job.kind, 'unknown'))


def operation_label(job, node, translate):
    return f'{node.name} · {kind_label(job, translate)}'


def status_label(job, translate):
    return translate('infra_status_' + (job.status if job.status in
        {'pending', 'running', 'done', 'failed', 'cancelled', 'interrupted'} else 'unknown'))


def stage_label(job, translate):
    progress = re.fullmatch(r'test:(\d+):(\d+):(\d+):(\d+):([a-z_]+)', job.stage or '')
    if progress:
        done, total, iteration, repeats, test = progress.groups()
        test_label = translate('infra_check_' + test)
        if test_label == 'infra_check_' + test:
            test_label = translate('infra_stage_check')
        return translate('infra_progress_checks', p0=done, p1=total, p2=test_label) + (
            '\n' + translate('infra_progress_pass', p0=iteration, p1=repeats) if int(repeats) > 1 else '')
    if job.status == 'running' and job.stage == 'queued':
        return translate('infra_stage_starting')
    return translate('infra_stage_' + STAGES.get(job.stage, 'check'))


def operation_text(job, node, translate, ahead=None):
    title = html.escape(operation_label(job, node, translate))
    text = f'<b>{title}</b>\n{status_label(job, translate)}'
    if job.status == 'pending' and ahead is not None:
        text += '\n' + translate('infra_queue_ahead', p0=ahead)
    elif job.status == 'running':
        text += '\n' + stage_label(job, translate)
    if job.status in {'failed', 'interrupted'} and job.result:
        error = job.result[:500]
        catalog = get_i18n_instance()
        for key, value in catalog.locales_data.get('ru', {}).items():
            if key.startswith('infra_') and value == error:
                error = translate(key)
                break
        text += '\n' + html.escape(error)
    if job.finished_at and job.started_at:
        duration = max(0, int((job.finished_at - job.started_at).total_seconds()))
        text += '\n' + translate('infra_operation_duration', p0=f'{duration // 60}:{duration % 60:02}')
    return text


def operation_buttons(job, translate, report_id=None):
    buttons = []
    if job.status in {'pending', 'running'}:
        label = 'infra_cancel_queued' if job.status == 'pending' else 'infra_stop'
        buttons.append((translate(label), f'infra:cancel:{job.id}'))
    # Installation replay can change panel state; failed installations only.
    if job.status in {'failed', 'cancelled', 'interrupted'} or (
            job.status == 'done' and job.kind in {'small', 'medium', 'large', 'restart', 'keys'}):
        buttons.append((translate('infra_retry'), f'infra:upgrade:{job.node_id}' if job.kind == 'upgrade' else f'infra:retry:{job.id}'))
    if report_id:
        buttons.append((translate('infra_open_report'), f'infra:report:{report_id}'))
    buttons.append((translate('infra_open_node'), f'infra:node:{job.node_id}'))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=data)] for label, data in buttons])


async def publish_job(service, job_id, complete=False):
    if not hasattr(service, '_job_message_lock'):
        service._job_message_lock = asyncio.Lock()
        service._job_message_cache = {}
    if not hasattr(service, '_job_message_times'):
        service._job_message_times = {}
    async with service._job_message_lock:
        try:
            async with service.factory() as session:
                job = await session.get(Job, job_id)
                node = await session.get(Node, job.node_id)
                if node.panel_scope != service.scope:
                    return
                user = await session.get(User, job.requested_by)
                language = (user.language_code if user else None) or getattr(service.settings, 'DEFAULT_LANGUAGE', 'ru')
                catalog = get_i18n_instance()
                translate = lambda key, **values: catalog.gettext(language, key, **values)
                active = (await session.execute(select(Job.id).join(Node).where(
                    Node.panel_scope == service.scope, Job.status.in_(['pending', 'running']))
                    .order_by(Job.id))).scalars().all()
                ahead = active.index(job.id) if job.id in active else None
                text = operation_text(job, node, translate, ahead)
                report = (await session.execute(select(Report).where(Report.job_id == job.id))).scalar_one_or_none()
                markup = operation_buttons(job, translate, report.id if report else None)
                cache_key = job.id
                fingerprint = text, markup.model_dump_json()
                previous = service._job_message_cache.get(cache_key)
                if (not complete and job.progress_message_id and previous
                        and previous[0].split('\n')[1] == text.split('\n')[1]
                        and time.monotonic() - service._job_message_times.get(cache_key, 0) < 2):
                    return
                if service._job_message_cache.get(cache_key) != fingerprint or not job.progress_message_id:
                    if job.progress_message_id:
                        try:
                            await service.bot.edit_message_text(text, chat_id=job.requested_by,
                                message_id=job.progress_message_id, reply_markup=markup, parse_mode='HTML')
                        except Exception as exc:
                            if 'message is not modified' not in str(exc):
                                # A deleted/uneditable message is replaced on the next refresh.
                                if not any(x in str(exc).lower() for x in ('message to edit not found', "message can't be edited")):
                                    raise
                                job.progress_message_id = None
                    if not job.progress_message_id:
                        sent = await service.bot.send_message(job.requested_by, text, reply_markup=markup, parse_mode='HTML')
                        if isinstance(sent.message_id, int):
                            job.progress_message_id = sent.message_id
                    await session.commit()
                    service._job_message_cache[cache_key] = fingerprint
                    service._job_message_times[cache_key] = time.monotonic()
                if not complete or job.completion_message_id:
                    return
                files, directory = [], None
                if report:
                    manifest = json.loads(report.manifest)
                    directory = Path(service.settings.INFRA_ARTIFACT_DIR) / 'reports' / manifest['directory']
                    saved = json.loads((directory / 'results.json').read_text())
                    results = saved.get('results', []) if isinstance(saved, dict) else saved
                    ok = sum(r.get('status') == 'ok' for r in results)
                    text += '\n\n' + translate('infra_completed_checks', p0=ok, p1=len(results) - ok)
                    issues = [translate('infra_check_' + r['id']) if r.get('id') else r.get('title', '')
                              for r in results if r.get('status') != 'ok']
                    if issues:
                        if job.status == 'done':
                            text = text.replace(status_label(job, translate), translate('infra_status_done_issues'), 1)
                        text += '\n' + translate('infra_checks_issues', p0=html.escape(', '.join(issues[:5])))
                    if job.kind == 'small':
                        text += '\n' + translate('infra_small_checks_ok') if ok == len(results) else ''
                    files = manifest['files']
                sent = await service.bot.send_message(job.requested_by, text, reply_markup=markup, parse_mode='HTML')
                if isinstance(sent.message_id, int):
                    job.completion_message_id = sent.message_id
                await session.commit()
                if directory and job.kind != 'small':
                    from .diagnostics import send_artifacts
                    await send_artifacts(service, job.requested_by, directory, files, job.kind)
        except Exception:
            logging.warning('Infrastructure operation message delivery failed for job %s', job_id)


async def refresh_queue_messages(service):
    async with service.factory() as session:
        ids = (await session.execute(select(Job.id).join(Node).where(
            Node.panel_scope == service.scope, Job.status.in_(['pending', 'running']))
            .order_by(Job.id))).scalars().all()
    for job_id in ids:
        await publish_job(service, job_id)
