import logging
from typing import Optional

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.admin_keyboards import get_admin_panel_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.runtime_settings_service import APP_SETTING_SPECS, RuntimeSettingsService
from bot.states.admin_states import AdminStates
from config.settings import Settings

router = Router(name="admin_runtime_settings_router")


def _display_value(raw: str, value_type: str, lang: str) -> str:
    if value_type == "bool":
        enabled = str(raw).lower() in {"1", "true", "yes", "on"}
        if lang == "ru":
            return "вкл" if enabled else "выкл"
        return "on" if enabled else "off"
    return str(raw)


async def _settings_keyboard(i18n: JsonI18n, lang: str, settings: Settings, session: AsyncSession):
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    service = RuntimeSettingsService(settings)
    values = await service.get_raw_map(session)
    builder = InlineKeyboardBuilder()
    for key, spec in APP_SETTING_SPECS.items():
        value = _display_value(values[key], spec.value_type, lang)
        builder.button(
            text=f"{_(spec.title_key)}: {value}",
            callback_data=f"runtime_setting:edit:{key}",
        )
    builder.button(text=_("back_to_admin_panel_button"), callback_data="admin_action:main")
    builder.adjust(1)
    return builder.as_markup()


async def show_runtime_settings_handler(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    await callback.message.edit_text(
        _("admin_runtime_settings_text"),
        reply_markup=await _settings_keyboard(i18n, lang, settings, session),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("runtime_setting:edit:"))
async def edit_runtime_setting_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    key = callback.data.rsplit(":", 1)[-1]
    if key not in APP_SETTING_SPECS:
        await callback.answer(_("admin_runtime_setting_unknown"), show_alert=True)
        return
    spec = APP_SETTING_SPECS[key]
    service = RuntimeSettingsService(settings)
    values = await service.get_raw_map(session)

    if spec.value_type == "bool":
        current = str(values[key]).lower() in {"1", "true", "yes", "on"}
        await service.set_value(session, key, "false" if current else "true", updated_by=callback.from_user.id)
        await session.commit()
        await show_runtime_settings_handler(callback, i18n_data, settings, session)
        return

    await state.update_data(runtime_setting_key=key)
    await state.set_state(AdminStates.waiting_for_runtime_setting_value)
    builder = InlineKeyboardBuilder()
    builder.button(text=_("back_to_runtime_settings_button"), callback_data="admin_action:runtime_settings")
    await callback.message.edit_text(
        _("admin_runtime_setting_enter_value", setting=_(spec.title_key), current=values[key]),
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_runtime_setting_value)
async def process_runtime_setting_value(
    message: types.Message,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.answer("Language error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    data = await state.get_data()
    key = data.get("runtime_setting_key")
    if key not in APP_SETTING_SPECS:
        await state.clear()
        await message.answer(
            _("admin_runtime_setting_unknown"),
            reply_markup=get_admin_panel_keyboard(i18n, lang, settings),
        )
        return

    service = RuntimeSettingsService(settings)
    try:
        await service.set_value(session, key, message.text or "", updated_by=message.from_user.id)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.info("Runtime setting validation failed for %s: %s", key, exc)
        await message.answer(_("admin_runtime_setting_invalid_value"))
        return

    await state.clear()
    await message.answer(
        _("admin_runtime_setting_saved"),
        reply_markup=await _settings_keyboard(i18n, lang, settings, session),
    )
