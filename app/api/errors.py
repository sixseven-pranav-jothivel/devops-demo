"""Exception handlers producing the consistent error body."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.errors import DomainError

logger = logging.getLogger("stockflow")

_HTTP_CODES: dict[int, tuple[str, str]] = {
    404: ("not_found", "Resource not found."),
    405: ("method_not_allowed", "Method not allowed."),
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or []}}
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_request: Request, error: Exception) -> JSONResponse:
        exc = cast(DomainError, error)
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, error: Exception) -> JSONResponse:
        exc = cast(RequestValidationError, error)
        details = [
            {
                "field": ".".join(str(part) for part in err["loc"][1:]) or str(err["loc"][0]),
                "message": str(err["msg"]),
            }
            for err in exc.errors()
        ]
        return error_response(422, "validation_error", "Request validation failed.", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, error: Exception) -> JSONResponse:
        exc = cast(StarletteHTTPException, error)
        code, message = _HTTP_CODES.get(exc.status_code, ("http_error", str(exc.detail)))
        return error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error", exc_info=exc)
        return error_response(500, "internal_error", "An internal error occurred.")
