import asyncio
import unittest
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse

from comet.observability.boundaries import playback_boundary
from comet.observability.context import request_context


def _request(*, method: str = "GET", range_header: str | None = None) -> Request:
    headers = []
    if range_header is not None:
        headers.append((b"range", range_header.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/playback",
            "raw_path": b"/playback",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )


async def _body(*chunks: bytes):
    for chunk in chunks:
        yield chunk


class PlaybackLoggingBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise(self, response: Response, request: Request):
        @playback_boundary(default_mode="proxy", default_source_type="torrent")
        async def endpoint(request: Request):
            return response

        events = []
        with (
            request_context(),
            patch(
                "comet.observability.boundaries.log.terminal",
                side_effect=lambda event, message, **fields: events.append(
                    (event, message, fields)
                ),
            ),
        ):
            observed = await endpoint(request)
            if isinstance(observed, StreamingResponse):
                async for _chunk in observed.body_iterator:
                    pass
        return events

    async def test_redirect_has_no_stream_terminal(self):
        events = await self._exercise(
            RedirectResponse("https://example.invalid/media"),
            _request(),
        )
        self.assertEqual([event[0] for event in events], ["playback.completed"])
        self.assertEqual(events[0][2]["playback_mode"], "redirect")
        self.assertEqual(events[0][2]["source_type"], "torrent")

    async def test_status_video_is_identified_as_status_playback(self):
        response = Response()
        response.comet_playback_mode = "status"
        events = await self._exercise(response, _request())

        self.assertEqual(events[0][2]["playback_mode"], "status")

    async def test_future_internal_modes_are_preserved_for_observability(self):
        response = Response()
        response.comet_playback_mode = "future_mode"
        response.comet_source_type = "future_transport"

        events = await self._exercise(response, _request())

        self.assertEqual(events[0][2]["playback_mode"], "future_mode")
        self.assertEqual(events[0][2]["source_type"], "future_transport")

    async def test_strict_range_has_one_playback_and_one_stream_terminal(self):
        response = StreamingResponse(_body(b"abc", b"de"), status_code=206)
        response.comet_playback_mode = "strict"
        events = await self._exercise(response, _request(range_header="bytes=0-4"))
        self.assertEqual(
            [event[0] for event in events],
            ["playback.completed", "stream.completed"],
        )
        self.assertEqual(events[0][2]["playback_mode"], "strict")
        self.assertEqual(events[1][2]["playback_mode"], "strict")
        self.assertEqual(events[1][2]["transfer_mode"], "range")
        self.assertEqual(events[1][2]["transferred_bytes"], 5)

    async def test_native_head_and_not_modified_never_emit_stream_terminal(self):
        for response, request in (
            (StreamingResponse(_body(), status_code=200), _request(method="HEAD")),
            (StreamingResponse(_body(), status_code=304), _request()),
        ):
            response.comet_playback_mode = "native"
            with self.subTest(method=request.method, status=response.status_code):
                events = await self._exercise(response, request)
                self.assertEqual(
                    [event[0] for event in events],
                    ["playback.completed"],
                )
                self.assertEqual(events[0][2]["playback_mode"], "native")

    async def test_immediate_disconnect_emits_cancelled_zero_byte_terminal(self):
        async def disconnected():
            raise asyncio.CancelledError
            yield b"unreachable"

        response = StreamingResponse(disconnected())
        response.comet_playback_mode = "proxy"

        @playback_boundary(default_mode="proxy")
        async def endpoint(request: Request):
            return response

        events = []
        with (
            request_context(),
            patch(
                "comet.observability.boundaries.log.terminal",
                side_effect=lambda event, message, **fields: events.append(
                    (event, message, fields)
                ),
            ),
        ):
            observed = await endpoint(_request())
            with self.assertRaises(asyncio.CancelledError):
                async for _chunk in observed.body_iterator:
                    pass
        self.assertEqual(
            [event[0] for event in events],
            ["playback.completed", "stream.completed"],
        )
        self.assertEqual(events[1][2]["outcome"], "cancelled")
        self.assertEqual(events[1][2]["transferred_bytes"], 0)
