import unittest
from unittest.mock import patch

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from comet.api.app import CorrelationMiddleware
from comet.observability import log


def wrapped(application: FastAPI):
    return CorrelationMiddleware(
        CORSMiddleware(
            application,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
    )


class HttpLoggingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_cacheable_response_replaces_all_spoofed_request_ids(self):
        application = FastAPI()

        @application.get("/private", name="private_result")
        async def private_result():
            return Response(
                "ok",
                headers={
                    "X-Request-ID": "application-canary",
                    "Cache-Control": "private, no-store",
                },
            )

        async with AsyncClient(
            transport=ASGITransport(app=wrapped(application)),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/private",
                headers={
                    "X-Request-ID": "client-canary",
                    "Origin": "https://browser.example",
                },
            )

        values = response.headers.get_list("x-request-id")
        self.assertEqual(len(values), 1)
        self.assertRegex(values[0], r"^[0-9a-f]{32}$")
        self.assertNotIn(values[0], {"application-canary", "client-canary"})
        self.assertEqual(
            response.headers["access-control-expose-headers"],
            "X-Request-ID",
        )

    async def test_shared_cache_response_has_no_origin_request_id(self):
        application = FastAPI()

        @application.get("/shared", name="manifest")
        async def manifest():
            return Response(
                "shared",
                headers={
                    "Cache-Control": "public, max-age=300",
                    "X-Request-ID": "origin-canary",
                },
            )

        async with AsyncClient(
            transport=ASGITransport(app=wrapped(application)),
            base_url="http://test",
        ) as client:
            response = await client.get("/shared")

        self.assertNotIn("x-request-id", response.headers)
        self.assertNotIn("origin-canary", repr(response.headers))

    async def test_unhandled_500_has_cors_one_id_and_one_http_terminal(self):
        application = FastAPI()

        @application.get("/failure", name="failure_route")
        async def failure_route():
            raise RuntimeError("credential-bearing exception canary")

        with (
            patch("comet.api.app.log.error") as error,
            patch("comet.api.app.log.verbose") as verbose,
        ):
            async with AsyncClient(
                transport=ASGITransport(
                    app=wrapped(application),
                    raise_app_exceptions=False,
                ),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/failure",
                    headers={"Origin": "https://browser.example"},
                )

        self.assertEqual(response.status_code, 500)
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        error.assert_called_once()
        verbose.assert_not_called()
        rendered = repr(error.call_args)
        self.assertIn("http.request.failed", rendered)
        self.assertNotIn("credential-bearing", rendered)

    async def test_explained_stream_failure_is_not_duplicated_as_http_error(self):
        application = FastAPI()

        async def body():
            try:
                yield b"first"
                raise RuntimeError("stream canary")
            finally:
                log.terminal(
                    "stream.completed",
                    "Stream completed",
                    outcome="failed",
                    transfer_mode="full",
                    transferred_bytes=5,
                    duration_ms=1,
                    error_code="transport_failure",
                    transport_failure_explained=True,
                )

        @application.get("/stream", name="proxy_stream")
        async def proxy_stream():
            return StreamingResponse(body())

        with (
            patch("comet.api.app.log.error") as error,
            patch("comet.api.app.log.verbose") as verbose,
        ):
            async with AsyncClient(
                transport=ASGITransport(
                    app=wrapped(application),
                    raise_app_exceptions=False,
                ),
                base_url="http://test",
            ) as client:
                await client.get("/stream")

        error.assert_not_called()
        verbose.assert_called_once()
        self.assertEqual(verbose.call_args.args[0], "http.request.completed")

    async def test_probe_route_never_emits_http_event_even_when_failing(self):
        application = FastAPI()

        @application.get("/ready", name="ready")
        async def ready():
            return Response("unready", status_code=503)

        with (
            patch("comet.api.app.log.error") as error,
            patch("comet.api.app.log.verbose") as verbose,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=wrapped(application)),
                base_url="http://test",
            ) as client:
                for _ in range(100):
                    response = await client.get("/ready")
                    self.assertEqual(response.status_code, 503)

        error.assert_not_called()
        verbose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
