"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import RepositoryDep

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: touches nothing."""
    return {"status": "ok"}


@router.get("/ready", response_model=None)
def ready(repository: RepositoryDep) -> dict[str, str] | JSONResponse:
    """Readiness: the repository must answer a ping."""
    try:
        repository.ping()
    except Exception as exc:
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "detail": type(exc).__name__}
        )
    return {"status": "ready", "repository": repository.backend_name}
