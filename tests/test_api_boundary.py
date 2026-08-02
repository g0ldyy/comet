import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from comet.api import app as app_module
from comet.api.login_rate_limit import admit_login_attempt


def _request(
    path: str,
    *,
    client: tuple[str, int] = ("127.0.0.1", 1234),
    headers=(),
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": client,
            "server": ("test", 443),
        }
    )


class ApiBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_cors_never_authorizes_browser_credentials(self):
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/health",
                headers={"Origin": "https://unrelated.example"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", response.headers)

    async def test_admin_responses_are_private_and_non_storable(self):
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app),
            base_url="http://test",
        ) as client:
            response = await client.get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_request_failure_log_omits_raw_exception_message(self):
        application = FastAPI()

        @application.get("/private/{b64config}")
        async def fail_request(b64config: str):
            raise RuntimeError(f"credential-bearing failure: {b64config}")

        correlated_application = app_module.CorrelationMiddleware(application)
        with patch.object(app_module.log, "error") as log_error:
            async with AsyncClient(
                transport=ASGITransport(app=correlated_application),
                base_url="http://test",
            ) as client:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "credential-bearing failure",
                ):
                    await client.get("/private/secret-value")

        rendered_call = repr(log_error.call_args)
        self.assertIn("http.request.failed", rendered_call)
        self.assertNotIn("secret-value", rendered_call)
        self.assertNotIn("credential-bearing", rendered_call)

    async def test_login_admission_uses_only_the_direct_peer(self):
        acquire_window = AsyncMock(return_value=None)
        with patch("comet.api.login_rate_limit.ProviderGovernor") as governor_type:
            governor_type.return_value.acquire_window = acquire_window
            admitted = await admit_login_attempt(
                object(),
                _request(
                    "/api/v1/auth/login",
                    client=("10.0.0.7", 1234),
                    headers=((b"x-forwarded-for", b"203.0.113.9"),),
                ),
                "admin_login",
            )
            await admit_login_attempt(
                object(),
                _request(
                    "/api/v1/auth/login",
                    client=("10.0.0.7", 1234),
                    headers=((b"x-forwarded-for", b"198.51.100.4"),),
                ),
                "admin_login",
            )
            await admit_login_attempt(
                object(),
                _request(
                    "/api/v1/auth/login",
                    client=("10.0.0.8", 1234),
                    headers=((b"x-forwarded-for", b"203.0.113.9"),),
                ),
                "admin_login",
            )

        self.assertFalse(admitted)
        scopes = [entry.args[0] for entry in acquire_window.call_args_list]
        self.assertEqual(len(scopes[0]), 32)
        self.assertEqual(scopes[0], scopes[1])
        self.assertNotEqual(scopes[0], scopes[2])


if __name__ == "__main__":
    unittest.main()
