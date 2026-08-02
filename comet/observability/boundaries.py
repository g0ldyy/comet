"""Reusable operation owners for HTTP playback and streamed response bodies."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from fastapi import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse

from comet.observability.logging import log

_P = ParamSpec("_P")
_R = TypeVar("_R", bound=Response)


def playback_boundary(
    *,
    default_mode: str,
    default_source_type: str = "unknown",
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Own one playback terminal and, when applicable, one stream terminal."""

    def decorate(
        function: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(function)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            started_at = time.monotonic_ns()
            request = _request_from(args, kwargs)
            try:
                response = await function(*args, **kwargs)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                content_id = _request_content_id(request)
                log.terminal(
                    "playback.completed",
                    "Playback preparation completed",
                    outcome="failed",
                    playback_mode=default_mode,
                    source_type=_request_source_type(
                        request,
                        default_source_type,
                    ),
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                    error_code="playback_failure",
                    **({"content_id": content_id} if content_id else {}),
                    exc=exc,
                )
                raise

            mode = _response_mode(response, default_mode)
            source_type = _response_source_type(
                response,
                _request_source_type(request, default_source_type),
            )
            status_code = response.status_code
            content_id = _response_content_id(
                response,
                _request_content_id(request),
            )
            if status_code >= 500:
                outcome = "failed"
            elif status_code >= 400:
                outcome = "rejected"
            else:
                outcome = "ok"
            log.terminal(
                "playback.completed",
                "Playback preparation completed",
                outcome=outcome,
                playback_mode=mode,
                source_type=source_type,
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                **({"http_status": status_code} if status_code != 200 else {}),
                **({"content_id": content_id} if content_id else {}),
            )
            if isinstance(response, StreamingResponse):
                if (
                    request is not None
                    and request.method == "HEAD"
                    or status_code == 304
                ):
                    return response
                range_requested = (
                    request is not None and request.headers.get("range") is not None
                )
                response.body_iterator = _observed_body(
                    response.body_iterator,
                    playback_mode=mode,
                    range_requested=range_requested,
                    content_id=content_id,
                )
            return response

        return wrapped

    return decorate


def _request_from(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    request = kwargs.get("request")
    if isinstance(request, Request):
        return request
    return next((value for value in args if isinstance(value, Request)), None)


def _response_mode(response: Response, default_mode: str) -> str:
    if isinstance(response, RedirectResponse):
        return "redirect"
    explicit = getattr(response, "comet_playback_mode", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return default_mode


def _response_source_type(response: Response, default_source_type: str) -> str:
    explicit = getattr(response, "comet_source_type", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return default_source_type


def _request_source_type(
    request: Request | None,
    default_source_type: str,
) -> str:
    explicit = (
        getattr(request.state, "comet_source_type", None)
        if request is not None
        else None
    )
    if isinstance(explicit, str) and explicit:
        return explicit
    return default_source_type


def _request_content_id(request: Request | None) -> str | None:
    explicit = (
        getattr(request.state, "comet_content_id", None)
        if request is not None
        else None
    )
    return explicit if isinstance(explicit, str) and explicit else None


def _response_content_id(
    response: Response,
    default_content_id: str | None,
) -> str | None:
    explicit = getattr(response, "comet_content_id", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return default_content_id


async def _observed_body(
    source: AsyncIterator[bytes],
    *,
    playback_mode: str,
    range_requested: bool,
    content_id: str | None,
) -> AsyncIterator[bytes]:
    started_at = time.monotonic_ns()
    transferred_bytes = 0
    try:
        async for chunk in source:
            transferred_bytes += len(chunk)
            yield chunk
    except asyncio.CancelledError:
        log.terminal(
            "stream.completed",
            "Stream transfer completed",
            outcome="cancelled",
            playback_mode=playback_mode,
            transfer_mode="range" if range_requested else "full",
            transferred_bytes=transferred_bytes,
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            transport_failure_explained=True,
            **({"content_id": content_id} if content_id else {}),
        )
        raise
    except Exception as exc:
        log.terminal(
            "stream.completed",
            "Stream transfer completed",
            outcome="failed",
            playback_mode=playback_mode,
            transfer_mode="range" if range_requested else "full",
            transferred_bytes=transferred_bytes,
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            error_code="stream_failure",
            transport_failure_explained=True,
            **({"content_id": content_id} if content_id else {}),
            exc=exc,
        )
        raise
    else:
        log.terminal(
            "stream.completed",
            "Stream transfer completed",
            outcome="ok",
            playback_mode=playback_mode,
            transfer_mode="range" if range_requested else "full",
            transferred_bytes=transferred_bytes,
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            **({"content_id": content_id} if content_id else {}),
        )


__all__ = ("playback_boundary",)
