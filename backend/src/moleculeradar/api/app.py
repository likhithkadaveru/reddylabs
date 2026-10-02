"""FastAPI application assembly.

Production safety is enforced at boot rather than left to deployment discipline:
`Settings.validate_for_production()` lists configuration problems and the
application refuses to start if any exist. A missing JWT secret or disabled auth
in production is a deployment that should fail loudly, not serve.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from .. import ENGINE_VERSION
from ..core.config import get_settings
from .routers import (
    auth,
    companies,
    evidence,
    feed,
    health,
    investigations,
    molecules,
    review,
    whitespace,
)

logger = logging.getLogger("moleculeradar.api")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security headers.

    The CSP is restrictive because this API serves JSON only — it has no need to
    load scripts, frame anything, or be framed.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        if get_settings().is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


def create_app() -> FastAPI:
    settings = get_settings()

    problems = settings.validate_for_production()
    if problems:
        # Refusing to boot is the correct behaviour: a production deployment with
        # auth disabled or no JWT secret must not quietly start serving.
        raise RuntimeError(
            "Refusing to start: production configuration problems:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    app = FastAPI(
        title="Molecule Radar API",
        description=(
            "Pharmaceutical opportunity intelligence by Llama Labs.\n\n"
            "Two measures are reported for every opportunity and must never be "
            "combined: **Opportunity Score** (how commercially interesting the "
            "situation looks) and **Evidence Confidence** (how well evidenced that "
            "picture is). Counts are scoped to Molecule Radar's current public-source "
            "coverage and are not complete global censuses. Every claim resolves to a "
            "source document through the evidence endpoint."
        ),
        version=ENGINE_VERSION,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[f"{settings.rate_limit_per_minute}/minute"],
    )
    app.state.limiter = limiter

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Rate limit exceeded."},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Invalid request.", "errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log the detail, return none of it. An internal error message can leak
        # schema and query structure.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error."},
        )

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(feed.router, prefix=api_prefix)
    app.include_router(molecules.router, prefix=api_prefix)
    app.include_router(companies.router, prefix=api_prefix)
    app.include_router(whitespace.router, prefix=api_prefix)
    app.include_router(evidence.router, prefix=api_prefix)
    app.include_router(investigations.router, prefix=api_prefix)
    app.include_router(review.router, prefix=api_prefix)
    app.include_router(health.router, prefix=api_prefix)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness only. Deliberately does not touch the database.

        A liveness probe that fails on a database blip causes a restart storm that
        makes the outage worse.
        """
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness: can we actually serve? Requires a working database."""
        from sqlalchemy import text

        from ..core.db import get_engine

        try:
            with get_engine().connect() as conn:
                conn.execute(text("select 1"))
        except Exception as exc:
            logger.error("Readiness check failed: %s", exc)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "detail": "database unreachable"},
            )
        return JSONResponse(content={"status": "ready"})

    return app


# Deliberately NOT instantiated at import time. `create_app()` refuses to run
# under invalid production configuration, and an import-time raise makes the
# module impossible to import for tests, migrations or tooling. Serve it with the
# factory flag instead:
#
#     uvicorn moleculeradar.api.app:create_app --factory

