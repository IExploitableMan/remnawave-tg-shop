import importlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


try:
    subscription_module = importlib.import_module("bot.services.subscription_service")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        subscription_module = None
    else:
        raise


class FakeSettings:
    DEFAULT_LANGUAGE = "en"
    EXPIRY_WARNING_WORKER_INTERVAL_SECONDS = 900
    ADDON_TRAFFIC_WORKER_INTERVAL_SECONDS = 300


class FakeI18n:
    def gettext(self, lang, key, **kwargs):
        parts = [key, f"lang={lang}"]
        parts.extend(f"{name}={value}" for name, value in sorted(kwargs.items()))
        return "|".join(parts)


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    async def send_message(self, user_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append({"user_id": user_id, "text": text, "reply_markup": reply_markup})


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.commits += 1


class FakeSessionFactory:
    def __init__(self):
        self.session = FakeSession()

    def __call__(self):
        return self.session


class FakeRuntime:
    def __init__(self, bools, ints):
        self.bools = bools
        self.ints = ints
        self.bool_calls = []
        self.int_calls = []

    async def get_bool(self, session, key):
        self.bool_calls.append(key)
        return self.bools.get(key, False)

    async def get_int(self, session, key):
        self.int_calls.append(key)
        return self.ints.get(key, 0)


class FakeSubscriptionDal:
    def __init__(self, due_by_key=None):
        self.due_by_key = due_by_key or {}
        self.queries = []
        self.updates = []

    async def get_subscriptions_due_for_expiry_warning(
        self,
        session,
        hours_before,
        *,
        kind,
        trial,
        include_skipped=False,
        limit=200,
    ):
        call = {
            "hours_before": hours_before,
            "kind": kind,
            "trial": trial,
            "include_skipped": include_skipped,
            "limit": limit,
        }
        self.queries.append(call)
        return self.due_by_key.get((kind, trial, include_skipped), [])

    async def update_subscription_notification_time(self, session, subscription_id, notification_time):
        self.updates.append((subscription_id, notification_time))


@unittest.skipIf(subscription_module is None, "project dependencies are not installed")
class SubscriptionExpiryWarningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_runtime = subscription_module.RuntimeSettingsService
        self.original_dal = subscription_module.subscription_dal

    def tearDown(self):
        subscription_module.RuntimeSettingsService = self.original_runtime
        subscription_module.subscription_dal = self.original_dal

    def make_service(self, *, bot=None, runtime=None, dal=None):
        if runtime is not None:
            subscription_module.RuntimeSettingsService = lambda settings: runtime
        if dal is not None:
            subscription_module.subscription_dal = dal
        service = subscription_module.SubscriptionService(
            FakeSettings(),
            panel_service=SimpleNamespace(),
            bot=bot if bot is not None else FakeBot(),
            i18n=FakeI18n(),
        )
        service._async_session_factory = FakeSessionFactory()
        return service

    @staticmethod
    def sub(subscription_id, user_id, hours_left, *, lang="en", status="ACTIVE"):
        return SimpleNamespace(
            subscription_id=subscription_id,
            user_id=user_id,
            end_date=datetime.now(timezone.utc) + timedelta(hours=hours_left),
            status_from_panel=status,
            user=SimpleNamespace(first_name=f"User{user_id}", language_code=lang),
        )

    async def test_worker_queries_base_trial_and_addon_with_separate_thresholds(self):
        base_sub = self.sub(1, 101, 70)
        trial_sub = self.sub(2, 102, 20, lang="ru", status="TRIAL")
        addon_sub = self.sub(3, 103, 8)
        dal = FakeSubscriptionDal(
            {
                ("base", False, False): [base_sub],
                ("base", True, False): [trial_sub],
                ("addon", False, True): [addon_sub],
            }
        )
        runtime = FakeRuntime(
            {
                "base_expiry_warning_enabled": True,
                "trial_expiry_warning_enabled": True,
                "addon_expiry_warning_enabled": True,
            },
            {
                "base_expiry_warning_hours_before": 72,
                "trial_expiry_warning_hours_before": 24,
                "addon_expiry_warning_hours_before": 12,
            },
        )
        bot = FakeBot()
        service = self.make_service(bot=bot, runtime=runtime, dal=dal)

        await service._process_expiry_warnings_once()

        self.assertEqual(
            dal.queries,
            [
                {"hours_before": 72, "kind": "base", "trial": False, "include_skipped": False, "limit": 200},
                {"hours_before": 24, "kind": "base", "trial": True, "include_skipped": False, "limit": 200},
                {"hours_before": 12, "kind": "addon", "trial": False, "include_skipped": True, "limit": 200},
            ],
        )
        self.assertEqual([msg["user_id"] for msg in bot.messages], [101, 102, 103])
        self.assertIn("subscription_expiry_warning_base", bot.messages[0]["text"])
        self.assertIn("subscription_expiry_warning_trial", bot.messages[1]["text"])
        self.assertIn("lang=ru", bot.messages[1]["text"])
        self.assertIn("subscription_expiry_warning_addon", bot.messages[2]["text"])
        self.assertEqual([item[0] for item in dal.updates], [1, 2, 3])
        self.assertEqual(service._async_session_factory.session.commits, 1)

    async def test_worker_skips_disabled_and_zero_hour_settings(self):
        dal = FakeSubscriptionDal({("addon", False, True): [self.sub(4, 104, 4)]})
        runtime = FakeRuntime(
            {
                "base_expiry_warning_enabled": False,
                "trial_expiry_warning_enabled": True,
                "addon_expiry_warning_enabled": True,
            },
            {
                "trial_expiry_warning_hours_before": 0,
                "addon_expiry_warning_hours_before": 6,
            },
        )
        service = self.make_service(runtime=runtime, dal=dal)

        await service._process_expiry_warnings_once()

        self.assertEqual(
            dal.queries,
            [{"hours_before": 6, "kind": "addon", "trial": False, "include_skipped": True, "limit": 200}],
        )

    async def test_warning_send_sets_notification_marker_only_on_success(self):
        dal = FakeSubscriptionDal()
        bot = FakeBot()
        service = self.make_service(bot=bot, dal=dal)
        await service._send_expiry_warning(FakeSession(), self.sub(5, 105, 25), "subscription_expiry_warning_base")
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual([item[0] for item in dal.updates], [5])
        self.assertIn("hours_left=25", bot.messages[0]["text"])

        failing_bot = FakeBot(fail=True)
        failing_service = self.make_service(bot=failing_bot, dal=dal)
        await failing_service._send_expiry_warning(FakeSession(), self.sub(6, 106, 2), "subscription_expiry_warning_base")
        self.assertEqual([item[0] for item in dal.updates], [5])

    async def test_worker_noops_without_bot_i18n_or_session_factory(self):
        runtime = FakeRuntime({"base_expiry_warning_enabled": True}, {"base_expiry_warning_hours_before": 72})
        dal = FakeSubscriptionDal()
        service = self.make_service(bot=None, runtime=runtime, dal=dal)
        service.bot = None
        await service._process_expiry_warnings_once()
        self.assertEqual(dal.queries, [])

        service = self.make_service(runtime=runtime, dal=dal)
        service.i18n = None
        await service._process_expiry_warnings_once()
        self.assertEqual(dal.queries, [])

        service = self.make_service(runtime=runtime, dal=dal)
        service._async_session_factory = None
        await service._process_expiry_warnings_once()
        self.assertEqual(dal.queries, [])
