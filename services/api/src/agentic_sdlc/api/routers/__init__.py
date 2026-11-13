from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentic_sdlc.api.routers import (
    backlog,
    projects,
    quality,
    releases,
    requirements,
    runs,
    scim,
    security,
    sso,
    system,
)


def register_routers(app: FastAPI) -> None:
    app.include_router(system.router, prefix="/api/v1")
    app.include_router(sso.router, prefix="/api/v1")
    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(requirements.router, prefix="/api/v1")
    app.include_router(backlog.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")
    app.include_router(quality.router, prefix="/api/v1")
    app.include_router(releases.router, prefix="/api/v1")
    app.include_router(security.router, prefix="/api/v1")
    app.include_router(backlog.admin_router, prefix="/api/v1")
    app.include_router(scim.router)

    @app.exception_handler(scim.ScimError)
    async def scim_error(_: Request, exc: scim.ScimError) -> JSONResponse:
        return scim.error_response(exc)
