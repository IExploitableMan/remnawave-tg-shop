from dataclasses import dataclass
from typing import Dict, Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import app_settings_dal

SettingType = Literal["bool", "int"]


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    env_attr: str
    value_type: SettingType
    title_key: str
    min_value: Optional[int] = None
    max_value: Optional[int] = None


APP_SETTING_SPECS: Dict[str, RuntimeSettingSpec] = {
    "base_expiry_warning_enabled": RuntimeSettingSpec(
        "base_expiry_warning_enabled",
        "BASE_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_base_expiry_warning_enabled",
    ),
    "base_expiry_warning_hours_before": RuntimeSettingSpec(
        "base_expiry_warning_hours_before",
        "BASE_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_base_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
    ),
    "trial_expiry_warning_enabled": RuntimeSettingSpec(
        "trial_expiry_warning_enabled",
        "TRIAL_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_trial_expiry_warning_enabled",
    ),
    "trial_expiry_warning_hours_before": RuntimeSettingSpec(
        "trial_expiry_warning_hours_before",
        "TRIAL_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_trial_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
    ),
    "addon_expiry_warning_enabled": RuntimeSettingSpec(
        "addon_expiry_warning_enabled",
        "ADDON_EXPIRY_WARNING_ENABLED",
        "bool",
        "admin_setting_addon_expiry_warning_enabled",
    ),
    "addon_expiry_warning_hours_before": RuntimeSettingSpec(
        "addon_expiry_warning_hours_before",
        "ADDON_EXPIRY_WARNING_HOURS_BEFORE",
        "int",
        "admin_setting_addon_expiry_warning_hours_before",
        min_value=0,
        max_value=24 * 30,
    ),
}


class RuntimeSettingsService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def env_default(self, spec: RuntimeSettingSpec) -> str:
        value = getattr(self.settings, spec.env_attr)
        if spec.value_type == "bool":
            return "true" if bool(value) else "false"
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
        try:
            return int(str(values.get(key, "0")).strip())
        except (TypeError, ValueError):
            return 0

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

        number = int(raw)
        if spec.min_value is not None and number < spec.min_value:
            raise ValueError("below_min")
        if spec.max_value is not None and number > spec.max_value:
            raise ValueError("above_max")
        return str(number)

    async def set_value(
        self,
        session: AsyncSession,
        key: str,
        value: str,
        updated_by: Optional[int] = None,
    ) -> None:
        normalized = self.validate(key, value)
        await app_settings_dal.upsert_setting(session, key, normalized, updated_by=updated_by)
