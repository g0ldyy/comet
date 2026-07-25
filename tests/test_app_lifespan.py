import unittest
from unittest.mock import AsyncMock, Mock, patch

import comet.api.app as app_module


class ApplicationLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_startup_failure_cleans_initialized_resources(self):
        setup_database = AsyncMock()
        teardown_database = AsyncMock()
        setup_executor = Mock()
        shutdown_executor = Mock()
        http_init = AsyncMock()
        http_close = AsyncMock()
        network_close = AsyncMock()
        add_queue_stop = AsyncMock()
        update_queue_stop = AsyncMock()
        tracker_download = AsyncMock(side_effect=RuntimeError("tracker startup failed"))

        with (
            patch.object(app_module, "setup_database", new=setup_database),
            patch.object(app_module, "teardown_database", new=teardown_database),
            patch.object(app_module, "setup_executor", new=setup_executor),
            patch.object(app_module, "shutdown_executor", new=shutdown_executor),
            patch.object(app_module.http_client_manager, "init", new=http_init),
            patch.object(app_module.http_client_manager, "close", new=http_close),
            patch.object(app_module.network_manager, "close_all", new=network_close),
            patch.object(app_module.add_torrent_queue, "stop", new=add_queue_stop),
            patch.object(
                app_module.torrent_update_queue, "stop", new=update_queue_stop
            ),
            patch.object(app_module, "download_best_trackers", new=tracker_download),
            patch.object(app_module.settings, "DOWNLOAD_GENERIC_TRACKERS", True),
        ):
            with self.assertRaisesRegex(RuntimeError, "tracker startup failed"):
                async with app_module.lifespan(app_module.app):
                    pass

        setup_database.assert_awaited_once_with()
        setup_executor.assert_called_once_with()
        http_init.assert_awaited_once_with()
        add_queue_stop.assert_awaited_once_with()
        update_queue_stop.assert_awaited_once_with()
        network_close.assert_awaited_once_with()
        http_close.assert_awaited_once_with()
        shutdown_executor.assert_called_once_with()
        teardown_database.assert_awaited_once_with()
