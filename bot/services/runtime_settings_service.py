from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import app_settings_dal

SettingType = Literal["bool", "int", "float"]
SettingSection = Literal["expiry", "referral"]


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    env_attr: str
    value_type: SettingType
    title_key: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    section: SettingSection = "expiry"
    default_value: Optional[str] = None


RUNTIME_SETTING_SECTIONS: Dict[str, str] = {
    "expiry": "admin_runtime_settings_expiry_section",
    "referral": "admin_runtime_settings_referral_section",
}

REFERRAL_INVITER_BONUS_FACTOR_KEY = "referral_inviter_bonus_factor"
REFERRAL_REFEREE_BONUS_FACTOR_KEY = "referral_referee_bonus_factor"

REFERRAL_INVITER_BONUS_KEYS: Dict[int, str] = {
    1: "referral_inviter_bonus_days_1_month",
    3: "referral_inviter_bonus_days_3_months",
    6: "referral_inviter_bonus_days_6_months",
    12: "referral_inviter_bonus_days_12_months",
}
REFERRAL_REFEREE_BONUS_KEYS: Dict[int, str] = {
    1: "referral_referee_bonus_days_1_month",
    3: "referral_referee_bonus_days_3_months",
    6: "referral_referee_bonus_days_6_months",
    12: "referral_referee_bonus_days_12_months",
}
REFERRAL_INVITER_TRIAL_BONUS_KEY = "referral_inviter_bonus_days_trial"


APP_SETTING_SPECS: Dict[str, RuntimeSettingSpec] = {
    "base_expiry_warning_enabled": RuntimeSettingSpec(
        "base_expiry_warning_enabled",
        "BASE_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_base_expiry_warning_enabled",
        default_value="true",
    ),
    "base_expiry_warning_hours_before": RuntimeSettingSpec(
        "base_expiry_warning_hours_before",
        "BASE_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_base_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
        default_value="72",
    ),
    "trial_expiry_warning_enabled": RuntimeSettingSpec(
        "trial_expiry_warning_enabled",
        "TRIAL_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_trial_expiry_warning_enabled",
        default_value="true",
    ),
    "trial_expiry_warning_hours_before": RuntimeSettingSpec(
        "trial_expiry_warning_hours_before",
        "TRIAL_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_trial_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
        default_value="24",
    ),
    "addon_expiry_warning_enabled": RuntimeSettingSpec(
        "addon_expiry_warning_enabled",
        "ADDON_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_addon_expiry_warning_enabled",
        default_value="true",
    ),
    "addon_expiry_warning_hours_before": RuntimeSettingSpec(
        "addon_expiry_warning_hours_before",
        "ADDON_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_addon_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
        default_value="24",
    ),
    REFERRAL_INVITER_BONUS_FACTOR_KEY: RuntimeSettingSpec(
        REFERRAL_INVITER_BONUS_FACTOR_KEY,
        "REFERRAL_BONUS_FACTOR_INVITER",
        "float",
        "admin_setting_referral_inviter_bonus_factor",
        min_value=0,
        max_value=100,
        section="referral",
        default_value="1",
    ),
    REFERRAL_REFEREE_BONUS_FACTOR_KEY: RuntimeSettingSpec(
        REFERRAL_REFEREE_BONUS_FACTOR_KEY,
        "REFERRAL_BONUS_FACTOR_REFEREE",
        "float",
        "admin_setting_referral_referee_bonus_factor",
        min_value=0,
        max_value=100,
        section="referral",
        default_value="1",
    ),
    "referral_inviter_bonus_days_1_month": RuntimeSettingSpec(
        "referral_inviter_bonus_days_1_month",
        "REFERRAL_BONUS_DAYS_INVITER_1_MONTH",
        "int",
        "admin_setting_referral_inviter_bonus_days_1_month",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="3",
    ),
    "referral_inviter_bonus_days_3_months": RuntimeSettingSpec(
        "referral_inviter_bonus_days_3_months",
        "REFERRAL_BONUS_DAYS_INVITER_3_MONTHS",
        "int",
        "admin_setting_referral_inviter_bonus_days_3_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="7",
    ),
    "referral_inviter_bonus_days_6_months": RuntimeSettingSpec(
        "referral_inviter_bonus_days_6_months",
        "REFERRAL_BONUS_DAYS_INVITER_6_MONTHS",
        "int",
        "admin_setting_referral_inviter_bonus_days_6_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="15",
    ),
    "referral_inviter_bonus_days_12_months": RuntimeSettingSpec(
        "referral_inviter_bonus_days_12_months",
        "REFERRAL_BONUS_DAYS_INVITER_12_MONTHS",
        "int",
        "admin_setting_referral_inviter_bonus_days_12_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="30",
    ),
    REFERRAL_INVITER_TRIAL_BONUS_KEY: RuntimeSettingSpec(
        REFERRAL_INVITER_TRIAL_BONUS_KEY,
        "REFERRAL_BONUS_DAYS_INVITER_TRIAL",
        "int",
        "admin_setting_referral_inviter_bonus_days_trial",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="1",
    ),
    "referral_referee_bonus_days_1_month": RuntimeSettingSpec(
        "referral_referee_bonus_days_1_month",
        "REFERRAL_BONUS_DAYS_REFEREE_1_MONTH",
        "int",
        "admin_setting_referral_referee_bonus_days_1_month",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="1",
    ),
    "referral_referee_bonus_days_3_months": RuntimeSettingSpec(
        "referral_referee_bonus_days_3_months",
        "REFERRAL_BONUS_DAYS_REFEREE_3_MONTHS",
        "int",
        "admin_setting_referral_referee_bonus_days_3_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="3",
    ),
    "referral_referee_bonus_days_6_months": RuntimeSettingSpec(
        "referral_referee_bonus_days_6_months",
        "REFERRAL_BONUS_DAYS_REFEREE_6_MONTHS",
        "int",
        "admin_setting_referral_referee_bonus_days_6_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="7",
    ),
    "referral_referee_bonus_days_12_months": RuntimeSettingSpec(
        "referral_referee_bonus_days_12_months",
        "REFERRAL_BONUS_DAYS_REFEREE_12_MONTHS",
        "int",
        "admin_setting_referral_referee_bonus_days_12_months",
        min_value=0,
        max_value=3650,
        section="referral",
        default_value="15",
    ),
}


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def round_bonus_days(days: int, factor: float) -> int:
    result = Decimal(str(days)) * Decimal(str(factor))
    if not result.is_finite():
        return 0
    return int(result.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class RuntimeSettingsService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def env_default(self, spec: RuntimeSettingSpec) -> str:
        value = getattr(self.settings, spec.env_attr, spec.default_value)
        if value is None:
            return spec.default_value or "0"
        if spec.value_type == "bool":
            return "true" if bool(value) else "false"
        if spec.value_type == "float":
            return _format_decimal(Decimal(str(value)))
        return str(int(value))

    async def get_raw_map(self, session: AsyncSession) -> Dict[str, str]:
        db_values = await app_settings_dal.get_settings_map(session)
        values = {key: self.env_default(spec) for key, spec in APP_SETTING_SPECS.items()}
        values.update({key: value for key, value in db_values.items() if key in APP_SETTING_SPECS})
        return values

    async def get_bool(self, session: AsyncSession, key: str) -> bool:
        values = await self.get_raw_map(session)
        return str(values.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}

    async def get_int(self, session: AsyncSession, key: str) -> int:
        values = await self.get_raw_map(session)
        return self._parse_int_value(values.get(key, "0"))

    async def get_float(self, session: AsyncSession, key: str) -> float:
        values = await self.get_raw_map(session)
        return self._parse_float_value(values.get(key, "0"))

    def validate(self, key: str, value: str) -> str:
        spec = APP_SETTING_SPECS[key]
        raw = str(value).strip()
        if spec.value_type == "bool":
            lowered = raw.lower()
            if lowered in {"1", "true", "yes", "on", "y", "да"}:
                return "true"
            if lowered in {"0", "false", "no", "off", "n", "нет"}:
                return "false"
            raise ValueError("invalid_bool")

        if spec.value_type == "float":
            try:
                number_decimal = Decimal(raw.replace(",", "."))
            except InvalidOperation as exc:
                raise ValueError("invalid_float") from exc
            if not number_decimal.is_finite():
                raise ValueError("invalid_float")
            if spec.min_value is not None and number_decimal < Decimal(str(spec.min_value)):
                raise ValueError("below_min")
            if spec.max_value is not None and number_decimal > Decimal(str(spec.max_value)):
                raise ValueError("above_max")
            return _format_decimal(number_decimal)

        number = int(raw)
        if spec.min_value is not None and number < spec.min_value:
            raise ValueError("below_min")
        if spec.max_value is not None and number > spec.max_value:
            raise ValueError("above_max")
        return str(number)

    async def get_referral_bonus_inviter(self, session: AsyncSession) -> Dict[int, int]:
        values = await self.get_raw_map(session)
        factor = self._parse_float_value(values.get(REFERRAL_INVITER_BONUS_FACTOR_KEY, "0"))
        return self._get_referral_bonus_map(values, REFERRAL_INVITER_BONUS_KEYS, factor)

    async def get_referral_bonus_referee(self, session: AsyncSession) -> Dict[int, int]:
        values = await self.get_raw_map(session)
        factor = self._parse_float_value(values.get(REFERRAL_REFEREE_BONUS_FACTOR_KEY, "0"))
        return self._get_referral_bonus_map(values, REFERRAL_REFEREE_BONUS_KEYS, factor)

    async def get_referral_trial_inviter_bonus_days(self, session: AsyncSession) -> int:
        values = await self.get_raw_map(session)
        base_days = self._parse_int_value(values.get(REFERRAL_INVITER_TRIAL_BONUS_KEY, "0"))
        factor = self._parse_float_value(values.get(REFERRAL_INVITER_BONUS_FACTOR_KEY, "0"))
        return round_bonus_days(base_days, factor)

    def _get_referral_bonus_map(
        self,
        values: Dict[str, str],
        key_map: Dict[int, str],
        factor: float,
    ) -> Dict[int, int]:
        bonuses: Dict[int, int] = {}
        for months, key in key_map.items():
            base_days = self._parse_int_value(values.get(key, "0"))
            bonuses[months] = round_bonus_days(base_days, factor)
        return bonuses

    def _parse_int_value(self, raw: object) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0

    def _parse_float_value(self, raw: object) -> float:
        try:
            parsed = Decimal(str(raw).strip().replace(",", "."))
        except (TypeError, ValueError):
            return 0.0
        except InvalidOperation:
            return 0.0
        if not parsed.is_finite():
            return 0.0
        return float(parsed)

    async def set_value(
        self,
        session: AsyncSession,
        key: str,
        value: str,
        updated_by: Optional[int] = None,
    ) -> None:
        normalized = self.validate(key, value)
        await app_settings_dal.upsert_setting(session, key, normalized, updated_by=updated_by)
