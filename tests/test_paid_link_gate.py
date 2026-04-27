import importlib
import unittest
from types import SimpleNamespace


try:
    paid_link_module = importlib.import_module("bot.utils.paid_link_gate")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        paid_link_module = None
    else:
        raise


class FakeSettings:
    REQUIRED_CHANNEL_SUBSCRIBE_TO_USE = True
    REQUIRED_CHANNEL_ID = -100123
    ADMIN_IDS = []


class FakeI18n:
    def gettext(self, lang, key, **_kwargs):
        return f"{lang}:{key}"


class FakeUserDal:
    def __init__(self, db_user):
        self.db_user = db_user
        self.calls = []

    async def get_user_by_id(self, _session, user_id):
        self.calls.append(user_id)
        return self.db_user


@unittest.skipIf(paid_link_module is None, "project dependencies are not installed")
class PaidLinkGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_user_dal = paid_link_module.user_dal
        self.original_prepare_config_links = paid_link_module.prepare_config_links

    def tearDown(self):
        paid_link_module.user_dal = self.original_user_dal
        paid_link_module.prepare_config_links = self.original_prepare_config_links

    async def test_disabled_gate_returns_prepared_link(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIBE_TO_USE = False
        user_dal = FakeUserDal(None)
        prepare_calls = []
        paid_link_module.user_dal = user_dal

        async def fake_prepare_config_links(_settings, raw_link):
            prepare_calls.append(raw_link)
            return "display-link", "button-link"

        paid_link_module.prepare_config_links = fake_prepare_config_links

        display, button, blocked = await paid_link_module.prepare_paid_config_links(
            settings,
            object(),
            100,
            FakeI18n(),
            "en",
            "https://panel/sub",
        )

        self.assertEqual((display, button, blocked), ("display-link", "button-link", False))
        self.assertEqual(prepare_calls, ["https://panel/sub"])
        self.assertEqual(user_dal.calls, [])

    async def test_unverified_required_channel_hides_link(self):
        settings = FakeSettings()
        db_user = SimpleNamespace(
            channel_subscription_verified=False,
            channel_subscription_verified_for=None,
        )
        user_dal = FakeUserDal(db_user)
        paid_link_module.user_dal = user_dal

        async def fail_prepare_config_links(_settings, _raw_link):
            raise AssertionError("config link must not be prepared when channel gate blocks")

        paid_link_module.prepare_config_links = fail_prepare_config_links

        display, button, blocked = await paid_link_module.prepare_paid_config_links(
            settings,
            object(),
            100,
            FakeI18n(),
            "ru",
            "https://panel/sub",
        )

        self.assertEqual(display, "ru:config_link_requires_channel_subscription")
        self.assertIsNone(button)
        self.assertTrue(blocked)
        self.assertEqual(user_dal.calls, [100])

    async def test_verified_required_channel_returns_link(self):
        settings = FakeSettings()
        db_user = SimpleNamespace(
            channel_subscription_verified=True,
            channel_subscription_verified_for=settings.REQUIRED_CHANNEL_ID,
        )
        paid_link_module.user_dal = FakeUserDal(db_user)

        async def fake_prepare_config_links(_settings, raw_link):
            return f"display:{raw_link}", f"button:{raw_link}"

        paid_link_module.prepare_config_links = fake_prepare_config_links

        display, button, blocked = await paid_link_module.prepare_paid_config_links(
            settings,
            object(),
            100,
            FakeI18n(),
            "en",
            "https://panel/sub",
        )

        self.assertEqual(display, "display:https://panel/sub")
        self.assertEqual(button, "button:https://panel/sub")
        self.assertFalse(blocked)

    async def test_admin_bypasses_link_block(self):
        settings = FakeSettings()
        settings.ADMIN_IDS = [100]
        user_dal = FakeUserDal(None)
        paid_link_module.user_dal = user_dal

        async def fake_prepare_config_links(_settings, raw_link):
            return raw_link, None

        paid_link_module.prepare_config_links = fake_prepare_config_links

        display, button, blocked = await paid_link_module.prepare_paid_config_links(
            settings,
            object(),
            100,
            FakeI18n(),
            "en",
            "https://panel/sub",
        )

        self.assertEqual((display, button, blocked), ("https://panel/sub", None, False))
        self.assertEqual(user_dal.calls, [])

    async def test_missing_user_hides_link_when_channel_required(self):
        settings = FakeSettings()
        paid_link_module.user_dal = FakeUserDal(None)

        blocked = await paid_link_module.is_paid_config_link_blocked(
            settings,
            object(),
            100,
        )

        self.assertTrue(blocked)
