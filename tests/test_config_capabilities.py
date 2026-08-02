import base64
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import orjson
from starlette.requests import Request

from comet.api.endpoints import config as config_endpoint
from comet.core.capability_states import EffectiveCapabilityState
from comet.core.models import native_usenet_sources

ROOT = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")


def _request(
    payload: bytes,
    *,
    cookies: dict[str, str] | None = None,
    query_string: bytes = b"",
) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    headers = []
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{key}={value}" for key, value in cookies.items()).encode(),
            )
        )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/configure/capabilities/test",
            "query_string": query_string,
            "headers": headers,
        },
        receive,
    )


def _config(provider_id: str) -> dict:
    return {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": provider_id,
                "displayName": "Easynews",
                "kind": "easynews",
                "enabled": True,
                "options": {
                    "username": "member",
                    "password": "secret",
                },
            }
        ],
        "discoverySources": [],
    }


class ConfigureCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_native_sources_follow_the_instance_configuration(self):
        with (
            patch.object(
                config_endpoint.settings, "USENET_NATIVE_SERVERS", (object(),)
            ),
            patch.object(
                config_endpoint.settings, "USENET_NATIVE_ALLOW_USER_SERVERS", True
            ),
        ):
            self.assertEqual(
                native_usenet_sources(config_endpoint.settings),
                ("instance_pool", "personal_servers"),
            )

    async def test_explicit_test_persists_and_returns_safe_binding_states(self):
        provider_id = str(uuid.uuid4())
        valid = EffectiveCapabilityState("valid", True, False, False)
        playback = AsyncMock(return_value={provider_id: valid})
        discovery = AsyncMock(return_value={})
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=playback,
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=discovery,
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(orjson.dumps(_config(provider_id)))
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            orjson.loads(response.body),
            {
                "version": 1,
                "ok": True,
                "bindings": [
                    {
                        "configuration_id": provider_id,
                        "display_name": "Easynews",
                        "state": "valid",
                        "eligible": True,
                        "degraded": False,
                        "error_code": None,
                        "retry_after": None,
                    }
                ],
            },
        )
        self.assertTrue(playback.await_args.kwargs["force_retest"])
        self.assertTrue(discovery.await_args.kwargs["force_retest"])
        self.assertIn("no-cache", response.headers["Cache-Control"])

    async def test_targeted_provider_test_excludes_every_other_binding(self):
        provider_id = str(uuid.uuid4())
        valid = EffectiveCapabilityState("valid", True, False, False)
        playback = AsyncMock(return_value={provider_id: valid})
        discovery = AsyncMock(return_value={})
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=playback,
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=discovery,
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(
                    orjson.dumps(_config(provider_id)),
                    query_string=f"configuration_id={provider_id}".encode(),
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            playback.await_args.kwargs["provider_configuration_ids"],
            frozenset({provider_id}),
        )
        self.assertEqual(
            discovery.await_args.kwargs["source_configuration_ids"],
            frozenset(),
        )

    async def test_targeted_source_test_excludes_every_provider(self):
        provider_id = str(uuid.uuid4())
        source_id = str(uuid.uuid4())
        config = _config(provider_id)
        config["discoverySources"] = [
            {
                "configurationId": source_id,
                "kind": "newznab",
                "enabled": True,
                "options": {
                    "endpoint": "https://indexer.example/api",
                    "apiKey": "secret",
                },
            }
        ]
        valid = EffectiveCapabilityState("valid", True, False, False)
        playback = AsyncMock(return_value={})
        discovery = AsyncMock(return_value={source_id: valid})
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=playback,
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=discovery,
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(
                    orjson.dumps(config),
                    query_string=f"configuration_id={source_id}".encode(),
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            playback.await_args.kwargs["provider_configuration_ids"],
            frozenset(),
        )
        self.assertEqual(
            discovery.await_args.kwargs["source_configuration_ids"],
            frozenset({source_id}),
        )
        self.assertEqual(
            orjson.loads(response.body)["bindings"][0]["configuration_id"],
            source_id,
        )

    async def test_unknown_or_disabled_target_is_not_testable(self):
        provider_id = str(uuid.uuid4())
        unknown_id = str(uuid.uuid4())
        playback = AsyncMock()
        discovery = AsyncMock()
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=playback,
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=discovery,
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(
                    orjson.dumps(_config(provider_id)),
                    query_string=f"configuration_id={unknown_id}".encode(),
                )
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            orjson.loads(response.body)["code"],
            "binding_not_testable",
        )
        playback.assert_not_awaited()
        discovery.assert_not_awaited()

    async def test_full_test_rejects_a_source_without_compatible_playback(self):
        source_id = str(uuid.uuid4())
        config = _config(str(uuid.uuid4()))
        config["playbackProviders"] = []
        config["discoverySources"] = [
            {
                "configurationId": source_id,
                "kind": "newznab",
                "enabled": True,
                "options": {
                    "endpoint": "https://indexer.example/api",
                    "apiKey": "secret",
                },
            }
        ]
        valid = EffectiveCapabilityState("valid", True, False, False)
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=AsyncMock(return_value={source_id: valid}),
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(orjson.dumps(config))
            )

        payload = orjson.loads(response.body)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["bindings"],
            [
                {
                    "configuration_id": source_id,
                    "display_name": "newznab",
                    "state": "plan_incompatible",
                    "eligible": False,
                    "degraded": False,
                    "error_code": "no_compatible_playback_provider",
                    "retry_after": None,
                }
            ],
        )

    async def test_full_test_keeps_a_source_with_compatible_playback(self):
        provider_id = str(uuid.uuid4())
        source_id = str(uuid.uuid4())
        config = _config(provider_id)
        config["discoverySources"] = [
            {
                "configurationId": source_id,
                "kind": "easynews",
                "enabled": True,
                "options": {
                    "username": "member",
                    "password": "secret",
                },
            }
        ]
        valid = EffectiveCapabilityState("valid", True, False, False)
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=AsyncMock(return_value={provider_id: valid}),
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=AsyncMock(return_value={source_id: valid}),
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(orjson.dumps(config))
            )

        payload = orjson.loads(response.body)
        self.assertTrue(payload["ok"])
        self.assertTrue(all(binding["eligible"] for binding in payload["bindings"]))

    async def test_full_test_normalizes_debrid_accounts_for_capability_bindings(self):
        provider_id = str(uuid.uuid4())
        account_id = str(uuid.uuid4())
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent", "usenet"],
            "accounts": {
                account_id: {
                    "kind": "torbox",
                    "apiKey": "torbox-secret",
                }
            },
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "displayName": "TorBox",
                    "kind": "torbox",
                    "enabled": True,
                    "accountId": account_id,
                }
            ],
            "discoverySources": [],
        }
        valid = EffectiveCapabilityState("valid", True, False, False)
        playback = AsyncMock(return_value={provider_id: valid})
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(config_endpoint.settings, "COMET_CAPABILITY_SECRET", ROOT),
            patch.object(
                config_endpoint.http_client_manager,
                "get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                config_endpoint,
                "ensure_playback_capability_states",
                new=playback,
            ),
            patch.object(
                config_endpoint,
                "ensure_discovery_capability_states",
                new=AsyncMock(return_value={}),
            ),
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(orjson.dumps(config))
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            orjson.loads(response.body)["bindings"][0]["display_name"],
            "TorBox",
        )
        self.assertEqual(
            playback.await_args.args[0]["_debridEntries"],
            [
                {
                    "configurationId": provider_id,
                    "service": "torbox",
                    "apiKey": "torbox-secret",
                }
            ],
        )

    async def test_invalid_or_oversized_configuration_is_rejected_before_validation(
        self,
    ):
        with (
            patch.object(config_endpoint.settings, "CONFIGURE_PAGE_PASSWORD", None),
            patch.object(config_endpoint.settings, "USENET_ENABLED", True),
            patch.object(
                config_endpoint.settings,
                "COMET_CAPABILITY_SECRET",
                ROOT,
            ),
        ):
            invalid = await config_endpoint.test_configure_capabilities(
                _request(b"not-json")
            )
            oversized = await config_endpoint.test_configure_capabilities(
                _request(b"x" * (config_endpoint._MAX_CAPABILITY_TEST_BODY_BYTES + 1))
            )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(oversized.status_code, 413)

    async def test_private_configure_page_requires_its_signed_session(self):
        with (
            patch.object(
                config_endpoint.settings,
                "CONFIGURE_PAGE_PASSWORD",
                "protected",
            ),
            patch.object(
                config_endpoint,
                "configure_session_active",
                new=AsyncMock(return_value=False),
            ) as verify,
        ):
            response = await config_endpoint.test_configure_capabilities(
                _request(b"{}", cookies={"configure_session": "invalid"})
            )

        self.assertEqual(response.status_code, 401)
        verify.assert_awaited_once_with("invalid")
