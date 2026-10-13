"""Request-local infrastructure localization using the bot's existing catalog."""
from contextvars import ContextVar
from bot.middlewares.i18n import get_i18n_instance

_language = ContextVar('infrastructure_language', default='ru')
_catalog = ContextVar('infrastructure_catalog', default=None)


def bind_language(i18n_data, settings, event):
    data = i18n_data or {}
    _language.set(data.get('current_language') or getattr(settings, 'DEFAULT_LANGUAGE', 'ru'))
    _catalog.set(data.get('i18n_instance') or get_i18n_instance())


def tr(key, **values):
    catalog = _catalog.get() or get_i18n_instance()
    return catalog.gettext(_language.get(), key, **values)


def localize_error(message):
    catalog = _catalog.get() or get_i18n_instance()
    for key, value in catalog.locales_data.get('ru', {}).items():
        if key.startswith('infra_') and value == message:
            return tr(key)
    return message


async def admin_text(service, admin_id, key, **values):
    """Background deliveries use the recipient's language, not a callback context."""
    from db.models import User
    language = getattr(service.settings, 'DEFAULT_LANGUAGE', 'ru')
    if service.factory:
        async with service.factory() as session:
            user = await session.get(User, admin_id)
            if user and user.language_code:
                language = user.language_code
    return get_i18n_instance().gettext(language, key, **values)


async def admin_error(service, admin_id, message):
    catalog = get_i18n_instance()
    for key, value in catalog.locales_data.get('ru', {}).items():
        if key.startswith('infra_') and value == message:
            return await admin_text(service, admin_id, key)
    return message
