import logging
from aiogram import Router, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import promo_code_dal
from bot.states.admin_states import AdminStates
from bot.keyboards.inline.admin_keyboards import get_back_to_admin_panel_keyboard, get_admin_panel_keyboard
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from bot.middlewares.i18n import JsonI18n

router = Router(name="promo_create_router")

PROMO_SCOPE_OPTIONS = {
    "base": (True, False, False, False),
    "combined": (False, True, False, False),
    "addon": (False, False, True, False),
    "topup": (False, False, False, True),
    "base_combined": (True, True, False, False),
    "base_addon": (True, False, True, False),
    "combined_addon": (False, True, True, False),
    "addon_topup": (False, False, True, True),
    "all": (True, True, True, True),
}


def _get_scope_keyboard(current_lang: str, i18n: JsonI18n):
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_base"), callback_data="promo_scope_select:base"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_combined"), callback_data="promo_scope_select:combined"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_addon"), callback_data="promo_scope_select:addon"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_topup"), callback_data="promo_scope_select:topup"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_base_combined"), callback_data="promo_scope_select:base_combined"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_base_addon"), callback_data="promo_scope_select:base_addon"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_combined_addon"), callback_data="promo_scope_select:combined_addon"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_addon_topup"), callback_data="promo_scope_select:addon_topup"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_scope_all"), callback_data="promo_scope_select:all"))
    builder.row(InlineKeyboardButton(text=_("admin_back_to_panel"), callback_data="admin_action:main"))
    return builder.as_markup()


def _get_boolean_choice_keyboard(current_lang: str, i18n: JsonI18n, prefix: str):
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("admin_promo_yes"), callback_data=f"{prefix}:yes"),
        InlineKeyboardButton(text=_("admin_promo_no"), callback_data=f"{prefix}:no"),
    )
    builder.row(InlineKeyboardButton(text=_("admin_back_to_panel"), callback_data="admin_action:main"))
    return builder.as_markup()


def _subscription_presence_label(mode: str, i18n: JsonI18n, current_lang: str) -> str:
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    mapping = {
        "any": _("admin_promo_presence_any"),
        "active_only": _("admin_promo_presence_active_only"),
        "inactive_only": _("admin_promo_presence_inactive_only"),
    }
    return mapping.get(mode or "any", _("admin_promo_presence_any"))


def _combined_discount_scope_label(scope: str, i18n: JsonI18n, current_lang: str) -> str:
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    mapping = {
        "base_only": _("admin_promo_combined_discount_base_only"),
        "full": _("admin_promo_combined_discount_full"),
    }
    return mapping.get(scope or "base_only", _("admin_promo_combined_discount_base_only"))


def _registration_rule_label_from_data(data: dict, i18n: JsonI18n, current_lang: str) -> str:
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    registration_date = data.get("min_user_registration_date")
    if not registration_date:
        return _("admin_promo_unlimited")

    direction = data.get("registration_date_direction", "after")
    if direction == "before":
        return _("admin_promo_registration_rule_before_value", value=registration_date.strftime("%Y-%m-%d"))
    return _("admin_promo_registration_rule_after_value", value=registration_date.strftime("%Y-%m-%d"))


def _scope_label_from_data(data: dict, i18n: JsonI18n, current_lang: str) -> str:
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    labels = []
    if data.get("applies_to_base_subscription"):
        labels.append(_("admin_promo_scope_short_base"))
    if data.get("applies_to_combined_subscription"):
        labels.append(_("admin_promo_scope_short_combined"))
    if data.get("applies_to_addon_subscription"):
        labels.append(_("admin_promo_scope_short_addon"))
    if data.get("applies_to_addon_traffic_topup"):
        labels.append(_("admin_promo_scope_short_topup"))
    return ", ".join(labels) if labels else _("admin_promo_scope_short_none")


def _get_registration_direction_keyboard(current_lang: str, i18n: JsonI18n):
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("admin_promo_registration_rule_none"), callback_data="promo_registration_mode:none"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_registration_rule_before"), callback_data="promo_registration_mode:before"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_registration_rule_after"), callback_data="promo_registration_mode:after"))
    builder.row(InlineKeyboardButton(text=_("admin_back_to_panel"), callback_data="admin_action:main"))
    return builder.as_markup()


def _get_subscription_presence_keyboard(current_lang: str, i18n: JsonI18n):
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("admin_promo_presence_any"), callback_data="promo_presence:any"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_presence_active_only"), callback_data="promo_presence:active_only"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_presence_inactive_only"), callback_data="promo_presence:inactive_only"))
    builder.row(InlineKeyboardButton(text=_("admin_back_to_panel"), callback_data="admin_action:main"))
    return builder.as_markup()


def _get_combined_discount_scope_keyboard(current_lang: str, i18n: JsonI18n):
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_("admin_promo_combined_discount_base_only"), callback_data="promo_combined_discount_scope:base_only"))
    builder.row(InlineKeyboardButton(text=_("admin_promo_combined_discount_full"), callback_data="promo_combined_discount_scope:full"))
    builder.row(InlineKeyboardButton(text=_("admin_back_to_panel"), callback_data="admin_action:main"))
    return builder.as_markup()


async def _prompt_promo_scope_selection(target, state: FSMContext, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    text = _("admin_promo_scope_prompt")
    if hasattr(target, "message"):
        try:
            await target.message.edit_text(text, reply_markup=_get_scope_keyboard(current_lang, i18n), parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=_get_scope_keyboard(current_lang, i18n), parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=_get_scope_keyboard(current_lang, i18n), parse_mode="HTML")


async def _prompt_promo_registration_mode(target, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    text = _("admin_promo_step6_registration_rule")
    if hasattr(target, "message"):
        try:
            await target.message.edit_text(
                text,
                reply_markup=_get_registration_direction_keyboard(current_lang, i18n),
                parse_mode="HTML",
            )
        except Exception:
            await target.message.answer(
                text,
                reply_markup=_get_registration_direction_keyboard(current_lang, i18n),
                parse_mode="HTML",
            )
        await target.answer()
    else:
        await target.answer(
            text,
            reply_markup=_get_registration_direction_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )


async def _prompt_promo_min_registration_date(target, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    text = _("admin_promo_step6_min_registration_date")
    if hasattr(target, "message"):
        try:
            await target.message.edit_text(
                text,
                reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                parse_mode="HTML",
            )
        except Exception:
            await target.message.answer(
                text,
                reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                parse_mode="HTML",
            )
        await target.answer()
    else:
        await target.answer(
            text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )


async def _prompt_promo_subscription_presence(target, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    text = _("admin_promo_step7_subscription_presence")
    reply_markup = _get_subscription_presence_keyboard(current_lang, i18n)
    if hasattr(target, "message"):
        try:
            await target.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=reply_markup, parse_mode="HTML")


async def _prompt_combined_discount_scope(target, i18n_data: dict, settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    text = _("admin_promo_step8_combined_discount_scope")
    reply_markup = _get_combined_discount_scope_keyboard(current_lang, i18n)
    if hasattr(target, "message"):
        try:
            await target.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=reply_markup, parse_mode="HTML")


async def create_promo_prompt_handler(callback: types.CallbackQuery,
                                      state: FSMContext, i18n_data: dict,
                                      settings: Settings,
                                      session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing promo creation.",
                              show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # Step 0: Ask for promo type (bonus_days or discount)
    prompt_text = _(
        "admin_promo_step0_type"
    )

    # Create keyboard for type selection
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=_("admin_promo_type_bonus_days"),
            callback_data="promo_type_select:bonus_days"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_promo_type_discount"),
            callback_data="promo_type_select:discount"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_promo_type_traffic_voucher"),
            callback_data="promo_type_select:traffic_gb"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_back_to_panel"),
            callback_data="admin_action:main"
        )
    )

    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML")
    except Exception as e:
        logging.warning(
            f"Could not edit message for promo type prompt: {e}. Sending new.")
        await callback.message.answer(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML")
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_promo_type_selection)


# Step 0: Process type selection
@router.callback_query(F.data.startswith("promo_type_select:"), StateFilter(AdminStates.waiting_for_promo_type_selection))
async def process_promo_type_selection(callback: types.CallbackQuery,
                                       state: FSMContext,
                                       i18n_data: dict,
                                       settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error processing type selection.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        promo_type = callback.data.split(":")[-1]  # "bonus_days" or "discount"
        await state.update_data(promo_type=promo_type)

        # Step 1: Ask for promo code
        prompt_text = _(
            "admin_promo_step1_code"
        )

        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
        await callback.answer()
        await state.set_state(AdminStates.waiting_for_promo_code)

    except Exception as e:
        logging.error(f"Error processing promo type selection: {e}")
        await callback.message.answer(_("error_occurred_try_again"))
        await callback.answer()


# Step 1: Process promo code
@router.message(AdminStates.waiting_for_promo_code, F.text)
async def process_promo_code_handler(message: types.Message,
                                    state: FSMContext,
                                    i18n_data: dict,
                                    settings: Settings,
                                    session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        code_str = message.text.strip().upper()
        if not (3 <= len(code_str) <= 30 and code_str.isalnum()):
            await message.answer(_(
                "admin_promo_invalid_code_format"
            ))
            return
        
        # Check if code already exists
        existing_promo = await promo_code_dal.get_promo_code_by_code(session, code_str)
        if existing_promo:
            await message.answer(_(
                "admin_promo_code_already_exists"
            ))
            return
        
        await state.update_data(promo_code=code_str)

        # Get promo type from state
        data = await state.get_data()
        promo_type = data.get("promo_type", "bonus_days")

        # Step 2: Ask for bonus days OR discount percentage based on type
        if promo_type == "discount":
            prompt_text = _(
                "admin_promo_step2_discount_percentage",
                code=code_str
            )
            next_state = AdminStates.waiting_for_promo_discount_percentage
        elif promo_type == "traffic_gb":
            prompt_text = _(
                "admin_promo_step2_traffic_gb",
                code=code_str,
            )
            next_state = AdminStates.waiting_for_promo_traffic_gb
        else:
            prompt_text = _(
                "admin_promo_step2_bonus_days",
                code=code_str
            )
            next_state = AdminStates.waiting_for_promo_bonus_days

        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
        await state.set_state(next_state)
        
    except Exception as e:
        logging.error(f"Error processing promo code: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 2: Process bonus days
@router.message(AdminStates.waiting_for_promo_bonus_days, F.text)
async def process_promo_bonus_days_handler(message: types.Message,
                                          state: FSMContext,
                                          i18n_data: dict,
                                          settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        bonus_days = int(message.text.strip())
        if not (1 <= bonus_days <= 365):
            await message.answer(_(
                "admin_promo_invalid_bonus_days"
            ))
            return

        await state.update_data(bonus_days=bonus_days)

        # Step 3: Ask for max activations
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step3_max_activations",
            code=data.get("promo_code"),
            bonus_days=bonus_days
        )

        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_promo_max_activations)

    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number"
        ))
    except Exception as e:
        logging.error(f"Error processing promo bonus days: {e}")
        await message.answer(_("error_occurred_try_again"))


@router.message(AdminStates.waiting_for_promo_traffic_gb, F.text)
async def process_promo_traffic_gb_handler(
    message: types.Message,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        traffic_gb = float(message.text.strip().replace(",", "."))
        if traffic_gb <= 0:
            await message.answer(_("admin_promo_invalid_traffic_gb"))
            return

        await state.update_data(traffic_amount_gb=round(traffic_gb, 2))
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step3_max_activations_traffic",
            code=data.get("promo_code"),
            traffic_gb=f"{float(traffic_gb):g}",
        )
        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_max_activations)
    except ValueError:
        await message.answer(_("admin_promo_invalid_traffic_gb"))
    except Exception as e:
        logging.error(f"Error processing promo traffic voucher amount: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 2: Process discount percentage
@router.message(AdminStates.waiting_for_promo_discount_percentage, F.text)
async def process_promo_discount_percentage_handler(message: types.Message,
                                                    state: FSMContext,
                                                    i18n_data: dict,
                                                    settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        discount_percentage = int(message.text.strip())
        if not (1 <= discount_percentage <= 100):
            await message.answer(_("admin_promo_invalid_discount_percentage"))
            return

        await state.update_data(discount_percentage=discount_percentage)

        prompt_text = _(
            "admin_promo_step3_discount_cap",
            code=(await state.get_data()).get("promo_code"),
            discount_percentage=discount_percentage,
        )

        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_promo_discount_cap)

    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number"
        ))
    except Exception as e:
        logging.error(f"Error processing discount percentage: {e}")
        await message.answer(_("error_occurred_try_again"))


@router.message(AdminStates.waiting_for_promo_discount_cap, F.text)
async def process_promo_discount_cap_handler(
    message: types.Message,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        raw_value = message.text.strip().replace(",", ".")
        parsed_amount = float(raw_value)
        if parsed_amount < 0:
            await message.answer(_("admin_promo_invalid_discount_cap"))
            return

        max_discount_amount = None if parsed_amount == 0 else round(parsed_amount, 2)
        await state.update_data(max_discount_amount=max_discount_amount)

        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step4_max_activations_discount",
            code=data.get("promo_code"),
            discount_percentage=data.get("discount_percentage"),
            max_discount_amount=_("admin_promo_unlimited") if max_discount_amount is None else max_discount_amount,
        )
        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_max_activations)
    except ValueError:
        await message.answer(_("admin_promo_invalid_discount_cap"))
    except Exception as e:
        logging.error(f"Error processing discount cap: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 3: Process max activations
@router.message(AdminStates.waiting_for_promo_max_activations, F.text)
async def process_promo_max_activations_handler(message: types.Message,
                                               state: FSMContext,
                                               i18n_data: dict,
                                               settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        max_activations = int(message.text.strip())
        if not (1 <= max_activations <= 10000):
            await message.answer(_(
                "admin_promo_invalid_max_activations"
            ))
            return
        
        await state.update_data(max_activations=max_activations)

        # Step 4: Ask for validity
        data = await state.get_data()
        promo_type = data.get("promo_type", "bonus_days")

        if promo_type == "discount":
            prompt_text = _(
                "admin_promo_step5_validity_discount",
                code=data.get("promo_code"),
                discount_percentage=data.get("discount_percentage"),
                max_activations=max_activations
            )
        elif promo_type == "traffic_gb":
            prompt_text = _(
                "admin_promo_step4_validity_traffic",
                code=data.get("promo_code"),
                traffic_gb=f"{float(data.get('traffic_amount_gb') or 0):g}",
                max_activations=max_activations,
            )
        else:
            prompt_text = _(
                "admin_promo_step4_validity",
                code=data.get("promo_code"),
                bonus_days=data.get("bonus_days"),
                max_activations=max_activations
            )
        
        # Create keyboard for validity options
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_unlimited_validity"),
                callback_data="promo_unlimited_validity"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_set_validity_days"),
                callback_data="promo_set_validity"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_back_to_panel"),
                callback_data="admin_action:main"
            )
        )
        
        await message.answer(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_promo_validity_days)
        
    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number"
        ))
    except Exception as e:
        logging.error(f"Error processing promo max activations: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 4: Handle unlimited validity
@router.callback_query(F.data == "promo_unlimited_validity", StateFilter(AdminStates.waiting_for_promo_validity_days))
async def process_promo_unlimited_validity(callback: types.CallbackQuery,
                                          state: FSMContext,
                                          i18n_data: dict,
                                          settings: Settings,
                                          session: AsyncSession):
    await state.update_data(validity_days=None)
    data = await state.get_data()
    if data.get("promo_type") == "traffic_gb":
        await state.update_data(
            applies_to_base_subscription=False,
            applies_to_combined_subscription=False,
            applies_to_addon_subscription=False,
            applies_to_addon_traffic_topup=True,
            promo_scope_code="topup",
        )
        await _prompt_promo_registration_mode(callback, i18n_data, settings)
        await state.set_state(AdminStates.waiting_for_promo_registration_date_mode)
        return
    await _prompt_promo_scope_selection(callback, state, i18n_data, settings)


# Step 4: Handle set validity
@router.callback_query(F.data == "promo_set_validity", StateFilter(AdminStates.waiting_for_promo_validity_days))
async def process_promo_set_validity(callback: types.CallbackQuery,
                                    state: FSMContext,
                                    i18n_data: dict,
                                    settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error processing validity.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    data = await state.get_data()
    promo_type = data.get("promo_type", "bonus_days")

    # Display the correct text based on promo type
    if promo_type == "discount":
        value_info = f"{data.get('discount_percentage')}%"
    else:
        value_info = f"{data.get('bonus_days')} дней"

    prompt_text = _("admin_promo_enter_validity_days")
    
    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
    await callback.answer()


# Step 4: Process validity days
@router.message(AdminStates.waiting_for_promo_validity_days, F.text)
async def process_promo_validity_days_handler(message: types.Message,
                                             state: FSMContext,
                                             i18n_data: dict,
                                             settings: Settings,
                                             session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        validity_days = int(message.text.strip())
        if not (1 <= validity_days <= 365):
            await message.answer(_(
                "admin_promo_invalid_validity_days"
            ))
            return
        
        await state.update_data(validity_days=validity_days)
        data = await state.get_data()
        if data.get("promo_type") == "traffic_gb":
            await state.update_data(
                applies_to_base_subscription=False,
                applies_to_combined_subscription=False,
                applies_to_addon_subscription=False,
                applies_to_addon_traffic_topup=True,
                promo_scope_code="topup",
            )
            await _prompt_promo_registration_mode(message, i18n_data, settings)
            await state.set_state(AdminStates.waiting_for_promo_registration_date_mode)
            return
        await _prompt_promo_scope_selection(message, state, i18n_data, settings)
        
    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number"
        ))
    except Exception as e:
        logging.error(f"Error processing promo validity days: {e}")
        await message.answer(_("error_occurred_try_again"))


@router.callback_query(F.data.startswith("promo_scope_select:"), StateFilter(AdminStates.waiting_for_promo_validity_days))
async def process_promo_scope_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    scope_code = callback.data.split(":", 1)[1]
    applies = PROMO_SCOPE_OPTIONS.get(scope_code)
    if not applies:
        await callback.answer("Invalid scope", show_alert=True)
        return
    await state.update_data(
        applies_to_base_subscription=applies[0],
        applies_to_combined_subscription=applies[1],
        applies_to_addon_subscription=applies[2],
        applies_to_addon_traffic_topup=applies[3],
        promo_scope_code=scope_code,
    )
    await _prompt_promo_registration_mode(callback, i18n_data, settings)
    await state.set_state(AdminStates.waiting_for_promo_registration_date_mode)

@router.callback_query(
    F.data.startswith("promo_registration_mode:"),
    StateFilter(AdminStates.waiting_for_promo_registration_date_mode),
)
async def process_promo_registration_mode_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    mode = callback.data.split(":", 1)[1]
    if mode == "none":
        await state.update_data(
            min_user_registration_date=None,
            registration_date_direction="after",
        )
        await _prompt_promo_subscription_presence(callback, i18n_data, settings)
        await state.set_state(AdminStates.waiting_for_promo_subscription_presence_mode)
        return

    await state.update_data(registration_date_direction=mode)
    await _prompt_promo_min_registration_date(callback, i18n_data, settings)
    await state.set_state(AdminStates.waiting_for_promo_min_user_registration_date)


@router.message(AdminStates.waiting_for_promo_min_user_registration_date, F.text)
async def process_promo_min_registration_date_handler(
    message: types.Message,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    raw_value = message.text.strip()
    if raw_value.lower() in {"0", "-", "none", "skip", "нет"}:
        await state.update_data(min_user_registration_date=None)
        await _prompt_promo_subscription_presence(message, i18n_data, settings)
        await state.set_state(AdminStates.waiting_for_promo_subscription_presence_mode)
        return

    try:
        min_registration_date = datetime.strptime(raw_value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer(_("admin_promo_invalid_min_registration_date"))
        return

    await state.update_data(min_user_registration_date=min_registration_date)
    await _prompt_promo_subscription_presence(message, i18n_data, settings)
    await state.set_state(AdminStates.waiting_for_promo_subscription_presence_mode)


@router.callback_query(
    F.data.startswith("promo_presence:"),
    StateFilter(AdminStates.waiting_for_promo_subscription_presence_mode),
)
async def process_promo_subscription_presence_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    subscription_presence_mode = callback.data.split(":", 1)[1]
    renewal_only = subscription_presence_mode == "active_only"
    await state.update_data(
        renewal_only=renewal_only,
        subscription_presence_mode=subscription_presence_mode,
    )
    data = await state.get_data()
    if (
        data.get("promo_type") == "discount"
        and data.get("applies_to_combined_subscription")
    ):
        await _prompt_combined_discount_scope(callback, i18n_data, settings)
        await state.set_state(AdminStates.waiting_for_promo_combined_discount_scope)
        return

    await create_promo_code_final(callback, state, i18n_data, settings, session)


@router.callback_query(
    F.data.startswith("promo_combined_discount_scope:"),
    StateFilter(AdminStates.waiting_for_promo_combined_discount_scope),
)
async def process_promo_combined_discount_scope_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    scope_value = callback.data.split(":", 1)[1]
    await state.update_data(combined_discount_scope=scope_value)
    await create_promo_code_final(callback, state, i18n_data, settings, session)


async def create_promo_code_final(callback_or_message,
                                 state: FSMContext,
                                 i18n_data: dict,
                                 settings: Settings,
                                 session: AsyncSession):
    """Final step - create the promo code in database"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        data = await state.get_data()
        promo_type = data.get("promo_type", "bonus_days")

        # Prepare promo code data
        promo_data = {
            "code": data["promo_code"],
            "promo_type": promo_type,
            "max_activations": data["max_activations"],
            "current_activations": 0,
            "is_active": True,
            "created_by_admin_id": callback_or_message.from_user.id,
            "created_at": datetime.now(timezone.utc),
            "applies_to_base_subscription": data.get("applies_to_base_subscription", True),
            "applies_to_combined_subscription": data.get("applies_to_combined_subscription", False),
            "applies_to_addon_subscription": data.get("applies_to_addon_subscription", False),
            "applies_to_addon_traffic_topup": data.get("applies_to_addon_traffic_topup", False),
            "min_user_registration_date": data.get("min_user_registration_date"),
            "renewal_only": bool(data.get("renewal_only", False)),
            "registration_date_direction": data.get("registration_date_direction", "after"),
            "subscription_presence_mode": data.get("subscription_presence_mode", "any"),
            "combined_discount_scope": data.get("combined_discount_scope", "base_only"),
        }

        # Set type-specific fields
        if promo_type == "discount":
            promo_data["discount_percentage"] = data["discount_percentage"]
            promo_data["max_discount_amount"] = data.get("max_discount_amount")
            promo_data["bonus_days"] = None
            promo_data["traffic_amount_gb"] = None
        elif promo_type == "traffic_gb":
            promo_data["bonus_days"] = None
            promo_data["discount_percentage"] = None
            promo_data["max_discount_amount"] = None
            promo_data["traffic_amount_gb"] = data.get("traffic_amount_gb")
        else:
            promo_data["bonus_days"] = data["bonus_days"]
            promo_data["discount_percentage"] = None
            promo_data["max_discount_amount"] = None
            promo_data["traffic_amount_gb"] = None

        # Set validity
        if data.get("validity_days"):
            promo_data["valid_until"] = datetime.now(timezone.utc) + timedelta(days=data["validity_days"])
        else:
            promo_data["valid_until"] = None

        # Create promo code
        created_promo = await promo_code_dal.create_promo_code(session, promo_data)
        await session.commit()

        # Log successful creation
        logging.info(f"Promo code '{data['promo_code']}' ({promo_type}) created with ID {created_promo.promo_code_id}")

        # Success message
        valid_until_str = _("admin_promo_unlimited") if not data.get("validity_days") else f"{data['validity_days']} дней"
        restriction_lines = [
            _("admin_promo_card_scope", scope=_scope_label_from_data(data, i18n, current_lang)),
            _("admin_promo_card_registration_rule", value=_registration_rule_label_from_data(data, i18n, current_lang)),
            _("admin_promo_card_subscription_presence", value=_subscription_presence_label(data.get("subscription_presence_mode", "any"), i18n, current_lang)),
        ]
        if promo_type == "discount" and data.get("applies_to_combined_subscription"):
            restriction_lines.append(
                _("admin_promo_card_combined_discount_scope", value=_combined_discount_scope_label(data.get("combined_discount_scope", "base_only"), i18n, current_lang))
            )

        # Format success message based on type
        if promo_type == "discount":
            success_text = _(
                "admin_promo_created_success_discount",
                code=data["promo_code"],
                discount_percentage=data['discount_percentage'],
                max_activations=data["max_activations"],
                valid_until_str=valid_until_str,
                max_discount_amount=_("admin_promo_unlimited") if data.get("max_discount_amount") is None else data.get("max_discount_amount"),
                restrictions="\n".join(restriction_lines),
            )
        elif promo_type == "traffic_gb":
            success_text = _(
                "admin_promo_created_success_traffic",
                code=data["promo_code"],
                traffic_gb=f"{float(data.get('traffic_amount_gb') or 0):g}",
                max_activations=data["max_activations"],
                valid_until_str=valid_until_str,
                restrictions="\n".join(restriction_lines),
            )
        else:
            success_text = _(
                "admin_promo_created_success",
                code=data["promo_code"],
                bonus_days=data['bonus_days'],
                max_activations=data["max_activations"],
                valid_until_str=valid_until_str,
                restrictions="\n".join(restriction_lines),
            )
        
        if hasattr(callback_or_message, 'message'):  # CallbackQuery
            try:
                await callback_or_message.message.edit_text(
                    success_text,
                    reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                    parse_mode="HTML"
                )
            except Exception:
                await callback_or_message.message.answer(
                    success_text,
                    reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                    parse_mode="HTML"
                )
            await callback_or_message.answer()
        else:  # Message
            await callback_or_message.answer(
                success_text,
                reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                parse_mode="HTML"
            )
        
        await state.clear()
        
    except Exception as e:
        logging.error(f"Error creating promo code: {e}")
        error_text = _("error_occurred_try_again")
        
        if hasattr(callback_or_message, 'message'):  # CallbackQuery
            await callback_or_message.message.answer(error_text)
            await callback_or_message.answer()
        else:  # Message
            await callback_or_message.answer(error_text)
        
        await state.clear()


# Cancel promo creation
@router.callback_query(
    F.data == "admin_action:main",
    StateFilter(
        AdminStates.waiting_for_promo_type_selection,
        AdminStates.waiting_for_promo_code,
        AdminStates.waiting_for_promo_bonus_days,
        AdminStates.waiting_for_promo_traffic_gb,
        AdminStates.waiting_for_promo_discount_percentage,
        AdminStates.waiting_for_promo_discount_cap,
        AdminStates.waiting_for_promo_max_activations,
        AdminStates.waiting_for_promo_validity_days,
        AdminStates.waiting_for_promo_registration_date_mode,
        AdminStates.waiting_for_promo_min_user_registration_date,
        AdminStates.waiting_for_promo_subscription_presence_mode,
        AdminStates.waiting_for_promo_combined_discount_scope,
    ),
)
async def cancel_promo_creation_state_to_menu(callback: types.CallbackQuery,
                                              state: FSMContext,
                                              settings: Settings,
                                              i18n_data: dict,
                                              session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error cancelling.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        await callback.message.edit_text(
            _(key="admin_panel_title"),
            reply_markup=get_admin_panel_keyboard(i18n, current_lang, settings)
        )
    except Exception:
        await callback.message.answer(
            _(key="admin_panel_title"),
            reply_markup=get_admin_panel_keyboard(i18n, current_lang, settings)
        )
    
    await callback.answer(_("admin_promo_creation_cancelled"))
    await state.clear()
