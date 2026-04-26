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
