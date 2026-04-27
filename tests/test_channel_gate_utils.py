import importlib
import unittest


try:
    gate_module = importlib.import_module("bot.utils.channel_gate")
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        gate_module = None
    else:
        raise


class FakeSettings:
    REQUIRED_CHANNEL_SUBSCRIBE_TO_USE = True
    REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "immediate"
    REQUIRED_CHANNEL_ID = -100123
    ADMIN_IDS = []


class FakeSubscriptionDal:
    def __init__(self, has_paid):
        self.has_paid = has_paid
        self.calls = []

    async def has_paid_subscription_for_user(self, _session, user_id):
        self.calls.append(user_id)
        return self.has_paid


@unittest.skipIf(gate_module is None, "project dependencies are not installed")
class ChannelGateUtilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_subscription_dal = gate_module.subscription_dal

    def tearDown(self):
        gate_module.subscription_dal = self.original_subscription_dal

    async def test_immediate_mode_enforces_without_paid_lookup(self):
        settings = FakeSettings()
        fake_dal = FakeSubscriptionDal(False)
        gate_module.subscription_dal = fake_dal

        result = await gate_module.should_enforce_channel_subscription_gate(
            settings,
            object(),
            100,
        )

        self.assertTrue(result)
        self.assertEqual(fake_dal.calls, [])

    async def test_after_paid_mode_skips_users_without_paid_subscription(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "after_paid_subscription"
        fake_dal = FakeSubscriptionDal(False)
        gate_module.subscription_dal = fake_dal

        result = await gate_module.should_enforce_channel_subscription_gate(
            settings,
            object(),
            100,
        )

        self.assertFalse(result)
        self.assertEqual(fake_dal.calls, [100])

    async def test_after_paid_mode_enforces_after_paid_subscription(self):
        settings = FakeSettings()
        settings.REQUIRED_CHANNEL_SUBSCRIPTION_MODE = "after_paid_subscription"
        fake_dal = FakeSubscriptionDal(True)
        gate_module.subscription_dal = fake_dal

        result = await gate_module.should_enforce_channel_subscription_gate(
            settings,
            object(),
            100,
        )

        self.assertTrue(result)
        self.assertEqual(fake_dal.calls, [100])

    async def test_admin_never_enforced(self):
        settings = FakeSettings()
        settings.ADMIN_IDS = [100]
        fake_dal = FakeSubscriptionDal(True)
        gate_module.subscription_dal = fake_dal

        result = await gate_module.should_enforce_channel_subscription_gate(
            settings,
            object(),
            100,
        )

        self.assertFalse(result)
        self.assertEqual(fake_dal.calls, [])

    def test_mode_aliases_normalize_to_after_paid(self):
        for value in ("after_paid", "after_payment", "paid", "post_paid", "after_paid_subscription"):
            with self.subTest(value=value):
                self.assertEqual(
                    gate_module.normalize_channel_gate_mode(value),
                    gate_module.CHANNEL_GATE_MODE_AFTER_PAID_SUBSCRIPTION,
                )

    def test_unknown_mode_normalizes_to_immediate(self):
        self.assertEqual(
            gate_module.normalize_channel_gate_mode("surprise"),
            gate_module.CHANNEL_GATE_MODE_IMMEDIATE,
        )
