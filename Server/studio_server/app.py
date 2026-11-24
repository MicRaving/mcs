"""The FastAPI application (INTERFACES §3.1, §3.10).

Only the foundation routers live here; the sync/lock/presence/done/assignment
routers are picked up when their modules exist, so they can be added without
editing this file.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION
from .auth import APIError, CODE_MESSAGES, _STATUS_CODES
from .config import Config, load_config
from .db import init_db
from .security import LoginRateLimiter

logger = logging.getLogger(__name__)

#: Routers owned by other tasks; included when importable (no app.py edit).
OPTIONAL_ROUTERS = ("api_sync", "api_locks", "api_presence", "api_done", "api_assign")

_MESSAGES = dict(CODE_MESSAGES)


def _contract_error(status_code: int, detail: Any) -> tuple[str, str]:
    if isinstance(detail, str) and detail in _MESSAGES:
        return detail, _MESSAGES[detail]
    code = _STATUS_CODES.get(status_code, "bad_request")
    message = detail if isinstance(detail, str) and detail else _MESSAGES.get(code, code)
    return code, message


def _error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": code, "message": message},
        headers=headers,
    )


def create_app(config: Config | None = None) -> FastAPI:
    """Build the application; the schema is created/checked immediately."""
    cfg = config or load_config()
    cfg.ensure_dirs()
    init_db(cfg.data_dir)

    app = FastAPI(
        title=SERVER_NAME,
        version=SERVER_VERSION,
        description="CodingStudio online-project sync server",
    )
    app.state.cfg = cfg
    app.state.rate_limiter = LoginRateLimiter(
        limit=cfg.rate_limit_failures, window_secs=cfg.rate_limit_window_secs
    )

    # ── errors: {"detail": "<code>", "message": "<human>"} (§3.10) ─────
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc, APIError):
            return _error_response(exc.status_code, exc.code, exc.message, exc.headers)
        code, message = _contract_error(exc.status_code, exc.detail)
        return _error_response(exc.status_code, code, message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422, "bad_request", "Invalid request body or parameters.", None
        )

    # ── client/server protocol guard (§3.10) ───────────────────────────
    @app.middleware("http")
    async def protocol_guard(request: Request, call_next):
        value = request.headers.get("X-Studio-Protocol")
        if value is not None and value.strip() != str(PROTOCOL_VERSION):
            return _error_response(
                426,
                "protocol_mismatch",
                _MESSAGES["protocol_mismatch"],
                {"Upgrade": f"studio/{PROTOCOL_VERSION}"},
            )
        return await call_next(request)

    # ── health (no auth) ───────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": SERVER_VERSION, "protocol": PROTOCOL_VERSION}

    # ── foundation routers ─────────────────────────────────────────────
    from . import api_admin, api_auth, api_projects

    app.include_router(api_auth.router)
    app.include_router(api_admin.router)
    app.include_router(api_projects.router)

    # ── routers owned by other tasks (present or not) ──────────────────
    for name in OPTIONAL_ROUTERS:
        module_name = f"{__package__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name in (module_name, name):
                logger.debug("optional router %s not installed", module_name)
                continue
            raise
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
            logger.debug("included optional router %s", module_name)

    return app


__all__ = ["create_app"]
