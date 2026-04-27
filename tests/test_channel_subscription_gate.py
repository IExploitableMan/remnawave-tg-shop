import importlib
import unittest
from types import SimpleNamespace


try:
    middleware_module = importlib.import_module("bot.middlewares.channel_subscription")
    gate_module = importlib.import_module("bot.utils.channel_gate")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        middleware_module = None
        gate_module = None
    else:
        raise


class FakeSettings:
    REQUIRED_CHANNEL_SUBSCRIBE_TO_USE = True
    REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "immediate"
    REQUIRED_CHANNEL_ID = -100123
    REQUIRED_CHANNEL_LINK = "https://t.me/example"
    DEFAULT_LANGUAGE = "en"
    ADMIN_IDS = []


class FakeI18n:
    def gettext(self, _lang, key, **_kwargs):
        return key


class FakeMessage:
    text = "hello"

    def __init__(self, user_id=100):
        self.answers = []
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeUserDal:
    def __init__(self, db_user):
        self.db_user = db_user

    async def get_user_by_id(self, _session, _user_id):
        return self.db_user


class FakeSubscriptionDal:
    def __init__(self, has_paid):
        self.has_paid = has_paid
        self.calls = []

    async def has_paid_subscription_for_user(self, _session, user_id):
        self.calls.append(user_id)
        return self.has_paid


@unittest.skipIf(middleware_module is None, "project dependencies are not installed")
class ChannelSubscriptionGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_user_dal = middleware_module.user_dal
        self.original_subscription_dal = gate_module.subscription_dal
        self.original_keyboard = middleware_module.get_channel_subscription_keyboard
        middleware_module.get_channel_subscription_keyboard = lambda *_args, **_kwargs: "keyboard"

    def tearDown(self):
        middleware_module.user_dal = self.original_user_dal
        gate_module.subscription_dal = self.original_subscription_dal
        middleware_module.get_channel_subscription_keyboard = self.original_keyboard

    async def run_middleware(self, settings, *, has_paid=False, verified=False):
        db_user = SimpleNamespace(
            channel_subscription_verified=verified,
            channel_subscription_verified_for=settings.REQUIRED_CHANNEL_ID if verified else None,
        )
        middleware_module.user_dal = FakeUserDal(db_user)
        fake_subscription_dal = FakeSubscriptionDal(has_paid)
        gate_module.subscription_dal = fake_subscription_dal

        middleware = middleware_module.ChannelSubscriptionMiddleware(settings, FakeI18n())
        message = FakeMessage()
        event = SimpleNamespace(message=message, callback_query=None)
        handled = []

        async def handler(_event, _data):
            handled.append(True)
            return "handled"

        result = await middleware(
            handler,
            event,
            {
                "event_from_user": SimpleNamespace(id=100),
                "session": object(),
                "i18n_data": {"current_language": "en", "i18n_instance": FakeI18n()},
                "bot": SimpleNamespace(),
            },
        )
        return result, handled, message.answers, fake_subscription_dal.calls

    async def test_immediate_mode_blocks_before_paid_subscription(self):
        settings = FakeSettings()

        result, handled, answers, calls = await self.run_middleware(settings, has_paid=False)

        self.assertIsNone(result)
        self.assertEqual(handled, [])
        self.assertEqual(answers, [("channel_subscription_required", "keyboard")])
        self.assertEqual(calls, [])

    async def test_after_paid_mode_allows_users_without_paid_subscription(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "after_paid_subscription"

        result, handled, answers, calls = await self.run_middleware(settings, has_paid=False)

        self.assertEqual(result, "handled")
        self.assertEqual(handled, [True])
        self.assertEqual(answers, [])
        self.assertEqual(calls, [100])

    async def test_after_paid_mode_blocks_after_paid_subscription_exists(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "after_paid_subscription"

        result, handled, answers, calls = await self.run_middleware(settings, has_paid=True)

        self.assertIsNone(result)
        self.assertEqual(handled, [])
        self.assertEqual(answers, [("channel_subscription_required", "keyboard")])
        self.assertEqual(calls, [100])

    async def test_after_paid_mode_allows_verified_paid_user(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "after_paid_subscription"

        result, handled, answers, calls = await self.run_middleware(
            settings,
            has_paid=True,
            verified=True,
        )

        self.assertEqual(result, "handled")
        self.assertEqual(handled, [True])
        self.assertEqual(answers, [])
        self.assertEqual(calls, [100])
