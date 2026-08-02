import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from comet.core import database as database_module


class UsenetLifecycleCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_includes_bounded_state_cleanup(self):
        capabilities = AsyncMock()
        governor = AsyncMock()
        playback = AsyncMock()
        provider_preparation = AsyncMock()
        rendered_release = AsyncMock()
        provider_export = AsyncMock()
        resolution = AsyncMock()
        artifacts = AsyncMock()
        with (
            patch(
                "comet.core.capability_states.CapabilityStateRepository",
                return_value=capabilities,
            ),
            patch(
                "comet.core.provider_governor.ProviderGovernor",
                return_value=governor,
            ),
            patch(
                "comet.playback.preparations.PlaybackPreparationRepository",
                return_value=playback,
            ),
            patch(
                "comet.playback.provider_preparations.ProviderPreparationRepository",
                return_value=provider_preparation,
            ),
            patch(
                "comet.playback.repository.RenderedReleaseRepository",
                return_value=rendered_release,
            ),
            patch(
                "comet.usenet.provider_exports.NzbProviderExportRepository",
                return_value=provider_export,
            ),
            patch(
                "comet.playback.resolution_cache.ProviderResolutionCacheRepository",
                return_value=resolution,
            ),
            patch(
                "comet.usenet.artifact_gc.SharedArtifactGarbageCollector",
                return_value=artifacts,
            ),
        ):
            await database_module._perform_usenet_lifecycle_cleanup(123.0)

        capabilities.cleanup_expired.assert_awaited_once_with(now=123.0)
        governor.collect_expired.assert_awaited_once_with(
            now=123.0,
            batch_size=10_000,
        )
        playback.garbage_collect.assert_awaited_once_with(now=123.0)
        provider_preparation.garbage_collect.assert_awaited_once_with(now=123.0)
        rendered_release.garbage_collect.assert_awaited_once_with(now=123.0)
        provider_export.garbage_collect.assert_awaited_once_with(now=123.0)
        resolution.cleanup_expired.assert_awaited_once_with(now=123.0)
        artifacts.collect.assert_awaited_once_with(now=123.0)

    async def test_one_cleanup_requires_the_shared_database_lock(self):
        @asynccontextmanager
        async def denied_lock():
            yield False

        cleanup = AsyncMock()
        with (
            patch.object(
                database_module,
                "_startup_cleanup_lock",
                denied_lock,
            ),
            patch.object(
                database_module,
                "_perform_usenet_lifecycle_cleanup",
                cleanup,
            ),
        ):
            acquired = await database_module._run_usenet_lifecycle_cleanup(123.0)

        self.assertFalse(acquired)
        cleanup.assert_not_awaited()

    async def test_one_cleanup_holds_the_shared_database_lock(self):
        @asynccontextmanager
        async def acquired_lock():
            yield True

        cleanup = AsyncMock()
        with (
            patch.object(
                database_module,
                "_startup_cleanup_lock",
                acquired_lock,
            ),
            patch.object(
                database_module,
                "_perform_usenet_lifecycle_cleanup",
                cleanup,
            ),
        ):
            acquired = await database_module._run_usenet_lifecycle_cleanup(456.0)

        self.assertTrue(acquired)
        cleanup.assert_awaited_once_with(456.0)

    async def test_periodic_cleanup_waits_and_survives_one_storage_failure(self):
        sleeps = 0

        async def bounded_sleep(delay):
            nonlocal sleeps
            self.assertEqual(
                delay,
                database_module._USENET_LIFECYCLE_CLEANUP_INTERVAL_SECONDS,
            )
            sleeps += 1
            if sleeps == 2:
                raise asyncio.CancelledError

        cleanup = AsyncMock(side_effect=OSError("temporary database outage"))
        with (
            patch.object(database_module.asyncio, "sleep", bounded_sleep),
            patch.object(
                database_module,
                "_run_usenet_lifecycle_cleanup",
                cleanup,
            ),
            patch.object(database_module.time, "time", return_value=789.0),
            self.assertRaises(asyncio.CancelledError),
        ):
            await database_module.cleanup_expired_usenet_state()

        cleanup.assert_awaited_once_with(789.0)

    async def test_periodic_cleanup_exposes_unexpected_failures(self):
        cleanup = AsyncMock(side_effect=RuntimeError("implementation failed"))
        with (
            patch.object(database_module.asyncio, "sleep", AsyncMock()),
            patch.object(
                database_module,
                "_run_usenet_lifecycle_cleanup",
                cleanup,
            ),
            patch.object(database_module.time, "time", return_value=789.0),
            self.assertRaisesRegex(RuntimeError, "implementation failed"),
        ):
            await database_module.cleanup_expired_usenet_state()

        cleanup.assert_awaited_once_with(789.0)

    async def test_startup_cleanup_exposes_unexpected_failures(self):
        @asynccontextmanager
        async def acquired_lock():
            yield True

        cleanup = AsyncMock(side_effect=RuntimeError("implementation failed"))
        with (
            patch.object(
                database_module.settings,
                "DATABASE_STARTUP_CLEANUP_INTERVAL",
                0,
            ),
            patch.object(database_module, "_startup_cleanup_lock", acquired_lock),
            patch.object(database_module, "_perform_startup_cleanup", cleanup),
            self.assertRaisesRegex(RuntimeError, "implementation failed"),
        ):
            await database_module._run_startup_cleanup()

        cleanup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
