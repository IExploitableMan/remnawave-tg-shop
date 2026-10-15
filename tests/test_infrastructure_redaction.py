import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.types import Update, Message, Chat, User
from bot.middlewares.action_logger_middleware import ActionLoggerMiddleware


class InfrastructureRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_is_redacted_even_when_handler_clears_state(self):
        state = SimpleNamespace(get_state=AsyncMock(return_value='InfrastructureStates:input'))
        user = User(id=111, is_bot=False, first_name='Admin')
        event = Update(update_id=1, message=Message(message_id=1,
            date=datetime.now(timezone.utc), chat=Chat(id=111, type='private'),
            from_user=user, text='very-secret-root-password'))
        middleware = ActionLoggerMiddleware(SimpleNamespace(ADMIN_IDS=[111],
            LOG_STORE_RAW_UPDATES=True, LOG_STORE_MESSAGE_CONTENT=True))
        async def handler(event, data):
            state.get_state.return_value = None
        data = {'state': state, 'session': object(), 'event_from_user': user}
        with patch('bot.middlewares.action_logger_middleware.user_dal.get_user_by_id', new=AsyncMock(return_value=True)), \
             patch('bot.middlewares.action_logger_middleware.message_log_dal.create_message_log_no_commit', new=AsyncMock()) as log:
            await middleware(handler, event, data)
        payload = log.call_args.args[1]
        self.assertIsNone(payload['raw_update_preview'])
        self.assertNotIn('very-secret', str(payload))
        self.assertEqual(payload['event_type'], 'message:infrastructure')
