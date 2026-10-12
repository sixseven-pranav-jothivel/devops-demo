"""FastAPI dependencies; everything is read from `request.app.state`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.repository.base import Repository


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_repository(request: Request) -> Repository:
    repository: Repository = request.app.state.repository
    return repository


SettingsDep = Annotated[Settings, Depends(get_settings)]
RepositoryDep = Annotated[Repository, Depends(get_repository)]
