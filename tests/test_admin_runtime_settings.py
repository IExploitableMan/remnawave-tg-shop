import importlib
import unittest
from types import SimpleNamespace


try:
    admin_module = importlib.import_module("bot.handlers.admin.runtime_settings")
    runtime_module = importlib.import_module("bot.services.runtime_settings_service")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        admin_module = None
    else:
        raise


class FakeSettings:
    DEFAULT_LANGUAGE = "en"
    BASE_EXPIRY_WARNING_ENABLED = True
    BASE_EXPIRY_WARNING_HOURS_BEFORE = 72
    TRIAL_EXPIRY_WARNING_ENABLED = True
    TRIAL_EXPIRY_WARNING_HOURS_BEFORE = 24
    ADDON_EXPIRY_WARNING_ENABLED = False
    ADDON_EXPIRY_WARNING_HOURS_BEFORE = 12


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        text = key
        if kwargs:
            text += "|" + "|".join(f"{name}={value}" for name, value in sorted(kwargs.items()))
        return text


class FakeMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.from_user = SimpleNamespace(id=777)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None
        self.cleared = False

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data.clear()


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeDal:
    def __init__(self):
        self.values = {
            "base_expiry_warning_enabled": "true",
            "base_expiry_warning_hours_before": "72",
        }
        self.upserts = []

    async def get_settings_map(self, session):
        return dict(self.values)

    async def upsert_setting(self, session, key, value, updated_by=None):
        self.values[key] = value
        self.upserts.append((key, value, updated_by))


@unittest.skipIf(admin_module is None, "project dependencies are not installed")
class AdminRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_dal = runtime_module.app_settings_dal
        self.fake_dal = FakeDal()
        runtime_module.app_settings_dal = self.fake_dal
        self.i18n_data = {"current_language": "en", "i18n_instance": FakeI18n()}
        self.settings = FakeSettings()

    def tearDown(self):
        runtime_module.app_settings_dal = self.original_dal

    async def test_bool_setting_toggle_saves_and_rerenders_menu(self):
        callback = FakeCallback("runtime_setting:edit:base_expiry_warning_enabled")
        session = FakeSession()

        await admin_module.edit_runtime_setting_callback(
            callback,
            FakeState(),
            self.i18n_data,
            self.settings,
            session,
        )

        self.assertEqual(self.fake_dal.upserts, [("base_expiry_warning_enabled", "false", 777)])
        self.assertEqual(session.commits, 1)
        self.assertEqual(callback.message.edits[-1][0], "admin_runtime_settings_text")
        self.assertEqual(callback.answers[-1], (None, False))

    async def test_int_setting_opens_input_state_without_saving(self):
        callback = FakeCallback("runtime_setting:edit:base_expiry_warning_hours_before")
        state = FakeState()
        session = FakeSession()

        await admin_module.edit_runtime_setting_callback(
            callback,
            state,
            self.i18n_data,
            self.settings,
            session,
        )

        self.assertEqual(self.fake_dal.upserts, [])
        self.assertEqual(session.commits, 0)
        self.assertEqual(state.data["runtime_setting_key"], "base_expiry_warning_hours_before")
        self.assertIsNotNone(state.state)
        self.assertIn("admin_runtime_setting_enter_value", callback.message.edits[-1][0])

    async def test_int_setting_message_saves_value_and_clears_state(self):
        state = FakeState()
        state.data["runtime_setting_key"] = "base_expiry_warning_hours_before"
        session = FakeSession()
        message = SimpleNamespace(text="36", from_user=SimpleNamespace(id=888), answer=FakeMessage().answer)

        await admin_module.process_runtime_setting_value(
            message,
            state,
            self.i18n_data,
            self.settings,
            session,
        )

        self.assertEqual(self.fake_dal.upserts, [("base_expiry_warning_hours_before", "36", 888)])
        self.assertEqual(session.commits, 1)
        self.assertTrue(state.cleared)

    async def test_invalid_int_value_rolls_back_and_keeps_state(self):
        state = FakeState()
        state.data["runtime_setting_key"] = "base_expiry_warning_hours_before"
        session = FakeSession()
        msg = FakeMessage()
        message = SimpleNamespace(text="bad", from_user=SimpleNamespace(id=888), answer=msg.answer)

        await admin_module.process_runtime_setting_value(
            message,
            state,
            self.i18n_data,
            self.settings,
            session,
        )

        self.assertEqual(self.fake_dal.upserts, [])
        self.assertEqual(session.rollbacks, 1)
        self.assertFalse(state.cleared)
        self.assertEqual(msg.answers[-1][0], "admin_runtime_setting_invalid_value")
