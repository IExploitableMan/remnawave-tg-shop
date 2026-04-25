import unittest

try:
    from bot.services.panel_api_service import PanelApiService
except ModuleNotFoundError as exc:
    if exc.name in {"aiohttp", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise


class FakePanelApiService(PanelApiService):
    def __init__(self, response_data):
        self._response_data = response_data
        self._profile_inbounds_cache = {}

    async def _request(self, *_args, **_kwargs):
        return self._response_data


class PanelApiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_inbounds_by_profile_uuid_reads_openapi_response_shape(self):
        service = FakePanelApiService(
            {
                "response": {
                    "total": 1,
                    "inbounds": [
                        {
                            "uuid": "11111111-1111-4111-8111-111111111111",
                            "tag": "vless-basic",
                        }
                    ],
                }
            }
        )

        inbounds = await service.get_inbounds_by_profile_uuid("profile-1")

        self.assertEqual(inbounds[0]["tag"], "vless-basic")

    async def test_get_inbounds_by_profile_uuid_keeps_legacy_list_shape(self):
        service = FakePanelApiService(
            {
                "response": [
                    {
                        "uuid": "11111111-1111-4111-8111-111111111111",
                        "tag": "vless-basic",
                    }
                ]
            }
        )

        inbounds = await service.get_inbounds_by_profile_uuid("profile-1")

        self.assertEqual(inbounds[0]["uuid"], "11111111-1111-4111-8111-111111111111")
