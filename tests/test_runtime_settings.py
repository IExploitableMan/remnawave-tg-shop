import importlib
import unittest


try:
    runtime_module = importlib.import_module("bot.services.runtime_settings_service")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        runtime_module = None
    else:
        raise


@unittest.skipIf(runtime_module is None, "project dependencies are not installed")
class RuntimeSettingsServiceTests(unittest.TestCase):
    def setUp(self):
        class FakeSettings:
            BASE_EXPIRY_WARNING_ENABLED = True
            BASE_EXPIRY_WARNING_HOURS_BEFORE = 72
            TRIAL_EXPIRY_WARNING_ENABLED = True
            TRIAL_EXPIRY_WARNING_HOURS_BEFORE = 24
            ADDON_EXPIRY_WARNING_ENABLED = False
            ADDON_EXPIRY_WARNING_HOURS_BEFORE = 12

        self.service = runtime_module.RuntimeSettingsService(FakeSettings())

    def test_bool_values_normalize_for_admin_input(self):
        self.assertEqual(self.service.validate("base_expiry_warning_enabled", "yes"), "true")
        self.assertEqual(self.service.validate("base_expiry_warning_enabled", "off"), "false")
        self.assertEqual(self.service.validate("trial_expiry_warning_enabled", "да"), "true")
        self.assertEqual(self.service.validate("trial_expiry_warning_enabled", "нет"), "false")

    def test_invalid_bool_rejected(self):
        with self.assertRaises(ValueError):
            self.service.validate("base_expiry_warning_enabled", "maybe")

    def test_hours_values_have_bounds(self):
        self.assertEqual(self.service.validate("base_expiry_warning_hours_before", "0"), "0")
        self.assertEqual(self.service.validate("base_expiry_warning_hours_before", "720"), "720")
        with self.assertRaises(ValueError):
            self.service.validate("base_expiry_warning_hours_before", "-1")
        with self.assertRaises(ValueError):
            self.service.validate("base_expiry_warning_hours_before", "721")

    def test_referral_factor_accepts_float_values(self):
        self.assertEqual(self.service.validate("referral_inviter_bonus_factor", "2"), "2")
        self.assertEqual(self.service.validate("referral_inviter_bonus_factor", "1.5"), "1.5")
        self.assertEqual(self.service.validate("referral_inviter_bonus_factor", "1,5"), "1.5")
        with self.assertRaises(ValueError):
            self.service.validate("referral_inviter_bonus_factor", "-0.1")

    def test_bonus_rounding_uses_half_up_math_rounding(self):
        self.assertEqual(runtime_module.round_bonus_days(1, 2.4), 2)
        self.assertEqual(runtime_module.round_bonus_days(1, 2.5), 3)

    def test_env_defaults_are_normalized_to_strings(self):
        spec = runtime_module.APP_SETTING_SPECS["addon_expiry_warning_enabled"]
        self.assertEqual(self.service.env_default(spec), "false")
        spec = runtime_module.APP_SETTING_SPECS["addon_expiry_warning_hours_before"]
        self.assertEqual(self.service.env_default(spec), "12")


@unittest.skipIf(runtime_module is None, "project dependencies are not installed")
class RuntimeSettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class FakeSettings:
            BASE_EXPIRY_WARNING_ENABLED = True
            BASE_EXPIRY_WARNING_HOURS_BEFORE = 72
            TRIAL_EXPIRY_WARNING_ENABLED = True
            TRIAL_EXPIRY_WARNING_HOURS_BEFORE = 24
            ADDON_EXPIRY_WARNING_ENABLED = False
            ADDON_EXPIRY_WARNING_HOURS_BEFORE = 12

        self.service = runtime_module.RuntimeSettingsService(FakeSettings())
        self.original_dal = runtime_module.app_settings_dal

    def tearDown(self):
        runtime_module.app_settings_dal = self.original_dal

    async def test_db_values_override_env_defaults_and_unknown_keys_are_ignored(self):
        class FakeDal:
            async def get_settings_map(self, session):
                return {
                    "base_expiry_warning_enabled": "false",
                    "base_expiry_warning_hours_before": "10",
                    "future_setting": "secret",
                }

        runtime_module.app_settings_dal = FakeDal()
        values = await self.service.get_raw_map(object())

        self.assertEqual(values["base_expiry_warning_enabled"], "false")
        self.assertEqual(values["base_expiry_warning_hours_before"], "10")
        self.assertEqual(values["trial_expiry_warning_hours_before"], "24")
        self.assertNotIn("future_setting", values)

    async def test_get_bool_and_get_int_parse_saved_values(self):
        class FakeDal:
            async def get_settings_map(self, session):
                return {
                    "base_expiry_warning_enabled": "yes",
                    "base_expiry_warning_hours_before": "bad",
                    "trial_expiry_warning_hours_before": "48",
                }

        runtime_module.app_settings_dal = FakeDal()

        self.assertTrue(await self.service.get_bool(object(), "base_expiry_warning_enabled"))
        self.assertEqual(await self.service.get_int(object(), "base_expiry_warning_hours_before"), 0)
        self.assertEqual(await self.service.get_int(object(), "trial_expiry_warning_hours_before"), 48)

    async def test_referral_bonus_maps_use_saved_days_and_factor(self):
        class FakeDal:
            async def get_settings_map(self, session):
                return {
                    "referral_inviter_bonus_days_1_month": "1",
                    "referral_inviter_bonus_days_trial": "1",
                    "referral_inviter_bonus_factor": "2.5",
                    "referral_referee_bonus_days_1_month": "1",
                    "referral_referee_bonus_factor": "2.4",
                }

        runtime_module.app_settings_dal = FakeDal()

        inviter = await self.service.get_referral_bonus_inviter(object())
        referee = await self.service.get_referral_bonus_referee(object())
        trial = await self.service.get_referral_trial_inviter_bonus_days(object())

        self.assertEqual(inviter[1], 3)
        self.assertEqual(referee[1], 2)
        self.assertEqual(trial, 3)

    async def test_set_value_validates_and_writes_normalized_value(self):
        calls = []

        class FakeDal:
            async def upsert_setting(self, session, key, value, updated_by=None):
                calls.append((key, value, updated_by))

        runtime_module.app_settings_dal = FakeDal()

        await self.service.set_value(object(), "base_expiry_warning_enabled", "off", updated_by=123)
        await self.service.set_value(object(), "base_expiry_warning_hours_before", "36", updated_by=123)

        self.assertEqual(
            calls,
            [
                ("base_expiry_warning_enabled", "false", 123),
                ("base_expiry_warning_hours_before", "36", 123),
            ],
        )
