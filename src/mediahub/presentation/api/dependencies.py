"""FastAPI dependency providers.

This module - together with :mod:`~mediahub.presentation.api.app` and
:mod:`~mediahub.presentation.api.lifespan` - is the API's composition root. It
is the only part of the presentation layer allowed to import from
:mod:`mediahub.infrastructure`; routers depend on *use cases* and stay unaware
that a container exists at all.

That boundary is what makes a route testable: overriding one provider swaps a
single operation without constructing an application.

Imports here are deliberately made at runtime rather than under
``TYPE_CHECKING``: FastAPI resolves annotations when it builds the dependency
graph, so a name that only exists for the type checker would fail at startup.

The ``Annotated`` aliases keep route signatures readable::

    async def register(payload: RegisterMediaRequest, use_case: RegisterMediaDep) -> ...
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from mediahub.application.download.use_cases.cancel_download_job import CancelDownloadJob
from mediahub.application.download.use_cases.get_download_job import GetDownloadJob
from mediahub.application.download.use_cases.list_download_jobs import ListDownloadJobs
from mediahub.application.download.use_cases.request_download import RequestDownload
from mediahub.application.media.use_cases.archive_media import ArchiveMedia
from mediahub.application.media.use_cases.get_media import GetMedia
from mediahub.application.media.use_cases.list_media import ListMedia
from mediahub.application.media.use_cases.register_media import RegisterMedia
from mediahub.infrastructure.di.container import Container
from mediahub.shared.config.settings import Settings


def get_container(request: Request) -> Container:
    """Return the container attached to the application at startup."""
    return cast(Container, request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]


def get_active_settings(container: ContainerDep) -> Settings:
    """Return the active configuration."""
    return container.settings


SettingsDep = Annotated[Settings, Depends(get_active_settings)]


# -- Media use cases ---------------------------------------------------------


def get_register_media(container: ContainerDep) -> RegisterMedia:
    """Provide the "catalogue a new item" use case."""
    return container.register_media_use_case()


def get_get_media(container: ContainerDep) -> GetMedia:
    """Provide the "read one item" use case."""
    return container.get_media_use_case()


def get_list_media(container: ContainerDep) -> ListMedia:
    """Provide the "browse the catalogue" use case."""
    return container.list_media_use_case()


def get_archive_media(container: ContainerDep) -> ArchiveMedia:
    """Provide the "retire an item" use case."""
    return container.archive_media_use_case()


RegisterMediaDep = Annotated[RegisterMedia, Depends(get_register_media)]
GetMediaDep = Annotated[GetMedia, Depends(get_get_media)]
ListMediaDep = Annotated[ListMedia, Depends(get_list_media)]
ArchiveMediaDep = Annotated[ArchiveMedia, Depends(get_archive_media)]


# -- Download use cases ------------------------------------------------------


def get_request_download(container: ContainerDep) -> RequestDownload:
    """Provide the "queue an acquisition" use case."""
    return container.request_download_use_case()


def get_get_download_job(container: ContainerDep) -> GetDownloadJob:
    """Provide the "read one job" use case."""
    return container.get_download_job_use_case()


def get_list_download_jobs(container: ContainerDep) -> ListDownloadJobs:
    """Provide the "browse the queue" use case."""
    return container.list_download_jobs_use_case()


def get_cancel_download_job(container: ContainerDep) -> CancelDownloadJob:
    """Provide the "stop a job" use case."""
    return container.cancel_download_job_use_case()


RequestDownloadDep = Annotated[RequestDownload, Depends(get_request_download)]
GetDownloadJobDep = Annotated[GetDownloadJob, Depends(get_get_download_job)]
ListDownloadJobsDep = Annotated[ListDownloadJobs, Depends(get_list_download_jobs)]
CancelDownloadJobDep = Annotated[CancelDownloadJob, Depends(get_cancel_download_job)]
