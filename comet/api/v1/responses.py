"""Envelope helpers and private API error normalization."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from comet.observability import current_request_id, log


class ApiProblem(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def _request_id(request: Request) -> str:
    return current_request_id() or request.headers.get("x-request-id") or "unavailable"


def success_response(
    request: Request,
    data: Any,
) -> JSONResponse:
    return JSONResponse(
        {
            "data": jsonable_encoder(data),
            "meta": {"request_id": _request_id(request)},
        },
    )


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "details": details,
            }
        },
        status_code=status_code,
        headers=headers,
    )


def _default_error(status_code: int) -> tuple[str, str]:
    if status_code == 400:
        return "invalid_request", "The request is invalid."
    if status_code == 401:
        return "authentication_required", "Authentication is required."
    if status_code == 403:
        return "permission_denied", "The request is not permitted."
    if status_code == 404:
        return "not_found", "The requested resource was not found."
    if status_code == 409:
        return "conflict", "The request conflicts with current state."
    if status_code == 413:
        return "request_too_large", "The request is too large."
    if status_code == 422:
        return "validation_failed", "The request did not pass validation."
    if status_code == 429:
        return "rate_limited", "Too many requests were made."
    if status_code == 503:
        return "service_unavailable", "The service is temporarily unavailable."
    return "request_failed", "The request could not be completed."


def install_api_error_handlers(app: FastAPI) -> None:
    original_http = app.exception_handlers.get(HTTPException)
    original_validation = app.exception_handlers.get(RequestValidationError)
    original_exception = app.exception_handlers.get(Exception)

    @app.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, exc: ApiProblem):
        return error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def api_validation_handler(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/api/v1/"):
            if original_validation is not None:
                return await original_validation(request, exc)
            return await request_validation_exception_handler(request, exc)
        details = []
        for error in exc.errors():
            detail = {
                "location": [
                    str(part)
                    for part in error.get("loc", ())
                    if part not in {"body", "query", "header", "cookie"}
                ],
                "type": error.get("type", "validation_error"),
                "message": str(error.get("msg", "Invalid value")).removeprefix(
                    "Value error, "
                ),
            }
            details.append(detail)
        return error_response(
            request,
            status_code=422,
            code="validation_failed",
            message="The request did not pass validation.",
            details=details,
        )

    @app.exception_handler(HTTPException)
    async def api_http_handler(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api/v1/"):
            if original_http is not None:
                return await original_http(request, exc)
            return await http_exception_handler(request, exc)
        code, message = _default_error(exc.status_code)
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def api_exception_handler(request: Request, exc: Exception):
        if not request.url.path.startswith("/api/v1/"):
            if original_exception is not None:
                return await original_exception(request, exc)
            raise exc
        log.error(
            "api.request.failed",
            "Versioned API request failed",
            error_code="unexpected_failure",
            exc=exc,
        )
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message="The request could not be completed.",
        )
