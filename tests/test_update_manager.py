import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from comet.utils.update import UpdateManager, UpdateStatus, VersionInfo


class _ResponseContext:
    def __init__(self, *, status=200, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self.payload


class UpdateManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        UpdateManager._check_task = None
        UpdateManager._update_status = None

    async def test_concurrent_checks_share_one_owned_request(self):
        started = asyncio.Event()
        release = asyncio.Event()
        status = UpdateStatus(has_update=False)

        async def fetch():
            started.set()
            await release.wait()
            return status

        with patch.object(
            UpdateManager, "_fetch_update_status", new=AsyncMock(side_effect=fetch)
        ) as mocked_fetch:
            first = asyncio.create_task(UpdateManager.check_for_updates())
            await started.wait()
            second = asyncio.create_task(UpdateManager.check_for_updates())
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)

        self.assertEqual(results, [status, status])
        self.assertEqual(mocked_fetch.await_count, 1)
        self.assertIsNone(UpdateManager._check_task)

    async def test_current_github_commit_schema_is_validated_and_branch_is_quoted(self):
        sha = "b" * 40
        payload = {
            "sha": sha,
            "html_url": f"https://github.com/g0ldyy/comet/commit/{sha}",
            "commit": {"committer": {"date": "2026-07-22T08:00:00Z"}},
        }
        response = _ResponseContext(payload=payload)
        session = SimpleNamespace(get=unittest.mock.Mock(return_value=response))

        with (
            patch.object(
                UpdateManager,
                "get_version_info",
                return_value=VersionInfo(
                    commit_hash="a" * 7,
                    build_date="2026-07-21T08:00:00+00:00",
                    branch="feature/current",
                ),
            ),
            patch(
                "comet.utils.update.http_client_manager.get_session",
                new=AsyncMock(return_value=session),
            ),
        ):
            status = await UpdateManager.check_for_updates()

        self.assertTrue(status.has_update)
        self.assertEqual(status.latest_commit_hash, "b" * 7)
        self.assertEqual(status.latest_url, payload["html_url"])
        self.assertIsNotNone(status.checked_at.tzinfo)
        self.assertIn("feature%2Fcurrent", session.get.call_args.args[0])

    async def test_missing_sha_is_an_error_not_an_up_to_date_result(self):
        payload = {
            "html_url": "https://github.com/g0ldyy/comet/commit/invalid",
            "commit": {"committer": {"date": "2026-07-22T08:00:00Z"}},
        }
        response = _ResponseContext(payload=payload)
        session = SimpleNamespace(get=unittest.mock.Mock(return_value=response))

        with (
            patch.object(
                UpdateManager,
                "get_version_info",
                return_value=VersionInfo(
                    commit_hash="a" * 7,
                    build_date="2026-07-21T08:00:00+00:00",
                ),
            ),
            patch(
                "comet.utils.update.http_client_manager.get_session",
                new=AsyncMock(return_value=session),
            ),
        ):
            status = await UpdateManager.check_for_updates()

        self.assertFalse(status.has_update)
        self.assertIn("invalid SHA", status.error)
        self.assertIsNone(status.latest_commit_hash)

    async def test_missing_current_date_is_an_error_not_a_false_negative(self):
        sha = "b" * 40
        payload = {
            "sha": sha,
            "html_url": f"https://github.com/g0ldyy/comet/commit/{sha}",
            "commit": {"committer": {"date": "2026-07-22T08:00:00Z"}},
        }
        response = _ResponseContext(payload=payload)
        session = SimpleNamespace(get=unittest.mock.Mock(return_value=response))

        with (
            patch.object(
                UpdateManager,
                "get_version_info",
                return_value=VersionInfo(commit_hash="a" * 7, build_date=None),
            ),
            patch(
                "comet.utils.update.http_client_manager.get_session",
                new=AsyncMock(return_value=session),
            ),
        ):
            status = await UpdateManager.check_for_updates()

        self.assertFalse(status.has_update)
        self.assertEqual(status.error, "commit dates are unavailable")


if __name__ == "__main__":
    unittest.main()
