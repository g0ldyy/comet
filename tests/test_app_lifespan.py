import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import comet.api.app as app_module


class ApplicationLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_task_surfaces_an_existing_failure(self):
        async def fail():
            raise RuntimeError("background failure")

        task = app_module.create_detached_task(fail(), name="test.failure")
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "background failure"):
            await app_module._cancel_task(task)

    async def test_partial_startup_failure_cleans_initialized_resources(self):
        shutdown_order = []
        setup_database = AsyncMock()
        teardown_database = AsyncMock(
            side_effect=lambda: shutdown_order.append("database")
        )
        shutdown_capability_refreshes = AsyncMock(
            side_effect=lambda: shutdown_order.append("capabilities")
        )
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
            patch.object(
                app_module,
                "shutdown_capability_refreshes",
                new=shutdown_capability_refreshes,
            ),
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
                async with app_module.lifespan(app_module.fastapi_app):
                    pass

        setup_database.assert_awaited_once_with()
        logging_settings = app_module.current_settings()
        setup_executor.assert_called_once_with(
            1,
            logging_settings.LOG_PROFILE.value,
            logging_settings.LOG_FORMAT.value,
            logging_settings.no_color,
        )
        http_init.assert_awaited_once_with()
        add_queue_stop.assert_awaited_once_with()
        update_queue_stop.assert_awaited_once_with()
        network_close.assert_awaited_once_with()
        http_close.assert_awaited_once_with()
        shutdown_executor.assert_called_once_with()
        shutdown_capability_refreshes.assert_awaited_once_with()
        teardown_database.assert_awaited_once_with()
        self.assertLess(
            shutdown_order.index("capabilities"),
            shutdown_order.index("database"),
        )
