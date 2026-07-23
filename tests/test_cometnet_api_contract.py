import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from comet.api.endpoints.cometnet_ui import (
    CreateInviteRequest as UiCreateInviteRequest,
)
from comet.api.endpoints.cometnet_ui import (
    CreatePoolRequest as UiCreatePoolRequest,
)
from comet.api.endpoints.cometnet_ui import JoinPoolRequest as UiJoinPoolRequest
from comet.api.endpoints.cometnet_ui import create_pool, join_pool
from comet.cometnet.standalone import (
    BroadcastRequest,
    CreateInviteRequest,
    JoinPoolRequest,
    StandaloneCometNet,
)


class CometNetRequestSchemaTests(unittest.TestCase):
    def test_admin_models_forbid_legacy_fields_and_type_coercion(self):
        invalid_cases = (
            (
                UiCreatePoolRequest,
                {"pool_id": "pool", "display_name": "Pool", "old": 1},
            ),
            (UiCreateInviteRequest, {"max_uses": "2"}),
            (UiJoinPoolRequest, {"invite_code": None}),
        )
        for model, payload in invalid_cases:
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    def test_standalone_models_forbid_legacy_fields_and_type_coercion(self):
        invalid_cases = (
            (CreateInviteRequest, {"expires_in": True}),
            (JoinPoolRequest, {"invite_code": "code", "legacy": "value"}),
            (
                BroadcastRequest,
                {"info_hash": "a" * 40, "title": "Title", "size": "123"},
            ),
        )
        for model, payload in invalid_cases:
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)


class CometNetEndpointErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_failure_preserves_forbidden_status(self):
        backend = AsyncMock()
        backend.join_pool_with_invite.return_value = False

        with self.assertRaises(HTTPException) as caught:
            await join_pool(
                "pool-a",
                UiJoinPoolRequest(invite_code="invite"),
                backend,
            )

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "Failed to join pool")

    async def test_unexpected_backend_error_is_not_returned_as_client_detail(self):
        backend = AsyncMock()
        backend.join_pool_with_invite.side_effect = RuntimeError("secret transport")

        with self.assertRaisesRegex(RuntimeError, "secret transport"):
            await join_pool(
                "pool-a",
                UiJoinPoolRequest(invite_code="invite"),
                backend,
            )

    async def test_expected_pool_validation_uses_fixed_client_detail(self):
        backend = AsyncMock()
        backend.create_pool.side_effect = ValueError("secret internal path")

        with self.assertRaises(HTTPException) as caught:
            await create_pool(
                UiCreatePoolRequest(pool_id="pool-a", display_name="Pool"),
                backend,
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Invalid pool request")
        self.assertNotIn("secret", caught.exception.detail)


class CometNetStandaloneLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_startup_failure_runs_every_registered_cleanup(self):
        standalone = object.__new__(StandaloneCometNet)
        standalone.ws_port = 8765
        standalone.http_port = 8766
        standalone._start_time = 0
        standalone._broadcasts_received = 0
        standalone._broadcasts_success = 0
        standalone.service = Mock(
            set_save_torrent_callback=Mock(),
            set_check_torrents_exist_callback=Mock(),
            start=AsyncMock(side_effect=RuntimeError("startup failed")),
            stop=AsyncMock(),
        )

        setup_database = AsyncMock()
        teardown_database = AsyncMock()
        setup_executor = Mock()
        shutdown_executor = Mock()
        queue_stop = AsyncMock()

        with (
            patch("comet.cometnet.standalone.setup_database", new=setup_database),
            patch("comet.cometnet.standalone.teardown_database", new=teardown_database),
            patch("comet.cometnet.standalone.setup_executor", new=setup_executor),
            patch("comet.cometnet.standalone.shutdown_executor", new=shutdown_executor),
            patch(
                "comet.cometnet.standalone.torrent_update_queue.stop",
                new=queue_stop,
            ),
        ):
            app = standalone._create_app()
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                async with app.router.lifespan_context(app):
                    pass

        standalone.service.stop.assert_awaited_once_with()
        queue_stop.assert_awaited_once_with()
        shutdown_executor.assert_called_once_with()
        teardown_database.assert_awaited_once_with()
