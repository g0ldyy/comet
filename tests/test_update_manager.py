import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from comet.utils.update import UpdateManager, UpdateStatus, VersionInfo, settings


class _ResponseContext:
    class _Content:
        def __init__(self, body):
            self.body = body

        async def read(self, _size):
            body, self.body = self.body, b""
            return body

    def __init__(self, *, status=200, payload=None, body=None):
        self.status = status
        self.payload = payload
        if body is None:
            body = json.dumps(payload).encode()
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        self.content = self._Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class UpdateManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        UpdateManager._check_task = None
        UpdateManager._update_status = None
        UpdateManager._version_info = None

    def tearDown(self):
        UpdateManager._version_info = None

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

    def test_docker_version_fields_use_bounded_current_shapes(self):
        UpdateManager._version_info = None
        with patch.multiple(
            settings,
            COMET_COMMIT_HASH="a" * 40,
            COMET_BUILD_DATE="2026-07-22T08:00:00Z",
            COMET_BRANCH="feature/current",
        ):
            current = UpdateManager.get_version_info()

        self.assertEqual(current.commit_hash, "a" * 7)
        self.assertEqual(current.build_date, "2026-07-22T08:00:00Z")
        self.assertEqual(current.branch, "feature/current")
        self.assertTrue(current.is_docker)

    async def test_unshipped_branch_checks_the_development_channel(self):
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
        self.assertIn("/commits/development", session.get.call_args.args[0])
        request_kwargs = session.get.call_args.kwargs
        self.assertFalse(request_kwargs["allow_redirects"])
        self.assertEqual(request_kwargs["headers"]["Accept-Encoding"], "identity")

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

    async def test_unbounded_or_malformed_responses_have_one_safe_error(self):
        for response in (
            _ResponseContext(body=b"{" + b"x" * (64 * 1024)),
            _ResponseContext(body=b'{"sha": 1, "sha": 2}'),
        ):
            session = SimpleNamespace(get=unittest.mock.Mock(return_value=response))
            with (
                self.subTest(response=response),
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

            self.assertIn(
                status.error,
                {
                    "GitHub API returned an invalid response",
                    "GitHub commit response has an invalid SHA",
                },
            )

    async def test_transport_details_are_not_retained_or_logged(self):
        secret = "proxy-user:proxy-password"
        session = SimpleNamespace(
            get=unittest.mock.Mock(side_effect=RuntimeError(secret))
        )
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

        self.assertEqual(status.error, "GitHub API request failed")


if __name__ == "__main__":
    unittest.main()
