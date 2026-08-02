import unittest
from unittest.mock import AsyncMock, patch

from comet.services import trackers as tracker_service


class TrackerDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = list(tracker_service.trackers)

    def tearDown(self):
        tracker_service.trackers[:] = self.original

    async def test_download_uses_bounded_public_fetch_and_deduplicates(self):
        document = (
            b"udp://tracker.example:80/announce\n"
            b"\n"
            b"udp://tracker.example:80/announce\n"
            b"https://tracker-two.example/announce\n"
        )
        with patch.object(
            tracker_service,
            "fetch_http_bytes",
            new=AsyncMock(return_value=document),
        ) as fetch:
            await tracker_service.download_best_trackers()

        self.assertEqual(
            tracker_service.trackers,
            [
                "udp://tracker.example:80/announce",
                "https://tracker-two.example/announce",
            ],
        )
        fetch.assert_awaited_once_with(
            tracker_service._TRACKERS_URL,
            max_bytes=tracker_service._MAX_TRACKER_DOCUMENT_BYTES,
            headers={"Accept": "text/plain"},
            redirects=1,
        )

    async def test_unusable_document_keeps_last_good_trackers_and_is_observed(self):
        tracker_service.trackers[:] = ["udp://last-good.example:80/announce"]
        document = b"unsupported://tracker.example/announce\n"

        with (
            patch.object(
                tracker_service,
                "fetch_http_bytes",
                new=AsyncMock(return_value=document),
            ),
            patch.object(tracker_service.log, "warning") as warning,
        ):
            await tracker_service.download_best_trackers()

        self.assertEqual(
            tracker_service.trackers,
            ["udp://last-good.example:80/announce"],
        )
        warning.assert_called_once()

    async def test_unusable_lines_do_not_discard_valid_or_future_urls(self):
        document = (
            b"\xff\n"
            b"unsupported://tracker.example/announce\n"
            b"https://user:secret@t\xc3\xa4cker.example/announce#channel\n"
        )

        with patch.object(
            tracker_service,
            "fetch_http_bytes",
            new=AsyncMock(return_value=document),
        ):
            await tracker_service.download_best_trackers()

        self.assertEqual(
            tracker_service.trackers,
            ["https://user:secret@täcker.example/announce#channel"],
        )

    async def test_internal_download_failure_is_not_swallowed(self):
        with (
            patch.object(
                tracker_service,
                "fetch_http_bytes",
                new=AsyncMock(side_effect=AssertionError("implementation")),
            ),
            self.assertRaisesRegex(AssertionError, "implementation"),
        ):
            await tracker_service.download_best_trackers()
