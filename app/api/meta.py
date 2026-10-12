"""Build metadata and metrics."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import RepositoryDep, SettingsDep
from app.models.domain import Metrics
from app.services import metrics as metrics_service

router = APIRouter(tags=["meta"])


class VersionInfo(BaseModel):
    name: str
    version: str
    git_sha: str
    environment: str
    build_time: str | None
    repository: str
    persistent: bool


@router.get("/api/version", response_model=VersionInfo)
def version(settings: SettingsDep, repository: RepositoryDep) -> VersionInfo:
    return VersionInfo(
        name="stockflow",
        version=settings.app_version,
        git_sha=settings.git_sha,
        environment=settings.deploy_environment,
        build_time=settings.build_time,
        repository=repository.backend_name,
        persistent=repository.persistent,
    )


@router.get("/api/metrics", response_model=Metrics)
def metrics(settings: SettingsDep, repository: RepositoryDep) -> Metrics:
    return metrics_service.compute_metrics(
        repository, low_stock_threshold=settings.low_stock_threshold
    )
