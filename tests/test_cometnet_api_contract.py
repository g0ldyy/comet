import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from comet.cometnet.standalone import (
    BroadcastRequest,
    CreateInviteRequest,
    JoinPoolRequest,
    StandaloneCometNet,
)
from comet.core.live_settings import SettingsApplication
from comet.core.models import settings
from comet.observability.logging import current_settings


class CometNetRequestSchemaTests(unittest.TestCase):
    def test_standalone_models_ignore_extensions_but_reject_type_coercion(self):
        self.assertEqual(
            JoinPoolRequest.model_validate(
                {"invite_code": "code", "extension": "value"}
            ).invite_code,
            "code",
        )
        invalid_cases = (
            (CreateInviteRequest, {"expires_in": True}),
            (
                BroadcastRequest,
                {"info_hash": "a" * 40, "title": "Title", "size": "123"},
            ),
        )
        for model, payload in invalid_cases:
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)


class CometNetStandaloneLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_settings_replace_only_the_standalone_p2p_service(self):
        standalone = object.__new__(StandaloneCometNet)
        previous = Mock(stop=AsyncMock(), start=AsyncMock())
        replacement = Mock(start=AsyncMock())
        standalone.service = previous
        candidate = settings.active_snapshot().model_copy(
            update={"COMETNET_LISTEN_PORT": 9876}
        )
        application = SettingsApplication(2, (), ("COMETNET_LISTEN_PORT",), ())
        prepared = SimpleNamespace(
            current=settings.active_snapshot(),
            candidate=candidate,
            changed_keys=("COMETNET_LISTEN_PORT",),
            application=application,
            logging=current_settings(),
            logging_changed=False,
        )

        with (
            patch(
                "comet.cometnet.standalone.prepare_settings_application",
                new=AsyncMock(return_value=prepared),
            ),
            patch.object(standalone, "_build_service", return_value=replacement),
            patch.object(standalone, "_configure_service"),
            patch.object(type(settings), "publish") as publish,
            patch("comet.cometnet.standalone.record_settings_application") as record,
        ):
            self.assertTrue(await standalone.apply_settings())

        previous.stop.assert_awaited_once_with()
        replacement.start.assert_awaited_once_with()
        self.assertIs(standalone.service, replacement)
        publish.assert_called_once_with(candidate)
        record.assert_called_once_with(application)

    async def test_failed_standalone_replacement_restores_the_previous_service(self):
        standalone = object.__new__(StandaloneCometNet)
        previous = Mock(stop=AsyncMock(), start=AsyncMock())
        replacement = Mock(start=AsyncMock(side_effect=RuntimeError("offline")))
        standalone.service = previous
        candidate = settings.active_snapshot().model_copy(
            update={"COMETNET_LISTEN_PORT": 9876}
        )
        prepared = SimpleNamespace(
            current=settings.active_snapshot(),
            candidate=candidate,
            changed_keys=("COMETNET_LISTEN_PORT",),
            application=SettingsApplication(
                2,
                (),
                ("COMETNET_LISTEN_PORT",),
                (),
            ),
            logging=current_settings(),
            logging_changed=False,
        )

        with (
            patch(
                "comet.cometnet.standalone.prepare_settings_application",
                new=AsyncMock(return_value=prepared),
            ),
            patch.object(standalone, "_build_service", return_value=replacement),
            patch.object(standalone, "_configure_service"),
            patch.object(type(settings), "publish") as publish,
            self.assertRaisesRegex(RuntimeError, "offline"),
        ):
            await standalone.apply_settings()

        previous.stop.assert_awaited_once_with()
        previous.start.assert_awaited_once_with()
        publish.assert_not_called()

    async def test_http_api_listens_on_all_available_address_families(self):
        standalone = object.__new__(StandaloneCometNet)
        standalone.app = object()
        standalone.http_port = 8766
        server = Mock(serve=AsyncMock())

        with (
            patch(
                "comet.cometnet.standalone.uvicorn.Config",
                return_value=object(),
            ) as config,
            patch(
                "comet.cometnet.standalone.uvicorn.Server",
                return_value=server,
            ),
        ):
            await standalone.run()

        self.assertIsNone(config.call_args.kwargs["host"])
        self.assertEqual(config.call_args.kwargs["port"], standalone.http_port)
        server.serve.assert_awaited_once_with()

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
        start_event_persistence = Mock()
        stop_event_persistence = Mock()
        queue_stop = AsyncMock()

        with (
            patch("comet.cometnet.standalone.setup_database", new=setup_database),
            patch("comet.cometnet.standalone.teardown_database", new=teardown_database),
            patch("comet.cometnet.standalone.setup_executor", new=setup_executor),
            patch("comet.cometnet.standalone.shutdown_executor", new=shutdown_executor),
            patch(
                "comet.cometnet.standalone.start_event_persistence",
                new=start_event_persistence,
            ),
            patch(
                "comet.cometnet.standalone.stop_event_persistence",
                new=stop_event_persistence,
            ),
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
        start_event_persistence.assert_called_once()
        stop_event_persistence.assert_called_once_with()
        teardown_database.assert_awaited_once_with()
