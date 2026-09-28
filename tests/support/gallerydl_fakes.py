"""A stand-in for the ``gallery_dl`` package.

The adapter reaches the library through three doors - a process-global
configuration store, two job classes it subclasses at call time, and an
extractor lookup - and this reproduces exactly those, with the behaviour the
adapter has to cope with: a runner that swallows its own exceptions and reports
them as status bits, a constructor that raises when no extractor claims the URL,
and downloads that land wherever the configuration points.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _Config:
    """gallery-dl's module-level settings, as a dictionary keyed by option name."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.clears = 0

    def clear(self) -> None:
        self.values.clear()
        self.clears += 1

    def set(self, path: tuple[str, ...], key: str, value: Any) -> None:
        del path
        self.values[key] = value


class NoExtractorError(Exception):
    """Raised by the real library's job constructor when nothing claims a URL."""

    code = 32


class FakeGalleryDl:
    """The slice of ``gallery_dl`` the adapter touches, scriptable per URL.

    Attributes:
        items: What each URL offers: ``(item_url, kwdict)`` pairs. A kwdict may
            carry ``_size`` to control the bytes a download writes.
        status: Status bits a job ends with, per URL, as the real runner sets
            them after catching one of its own exceptions.
        raises: An exception ``run()`` lets escape, per URL.
        unsupported: URLs no extractor claims; constructing a job raises.
        download_delay: Seconds each downloaded item takes.
        jobs: Every job constructed, in order.
    """

    def __init__(self) -> None:
        self.items: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.status: dict[str, int] = {}
        self.raises: dict[str, Exception] = {}
        self.unsupported: set[str] = set()
        self.download_delay = 0.0
        self.jobs: list[Any] = []
        self.config = _Config()
        self.version = SimpleNamespace(__version__="fake-1.0")
        self.exception = SimpleNamespace(NoExtractorError=NoExtractorError)
        self.extractor = SimpleNamespace(find=self._find)
        job_class = self._job_class()
        self.job = SimpleNamespace(Job=job_class, DownloadJob=self._download_job_class(job_class))

    # -- extractor lookup ----------------------------------------------------

    def _find(self, url: str) -> object | None:
        return None if url in self.unsupported else object()

    # -- jobs --------------------------------------------------------------------

    def _job_class(self) -> type:
        fake = self

        class Job:
            def __init__(self, extr: str, parent: object | None = None) -> None:
                del parent
                if extr in fake.unsupported:
                    raise NoExtractorError
                self.url = extr
                self.status = 0
                fake.jobs.append(self)

            def run(self) -> None:
                if self.url in fake.raises:
                    raise fake.raises[self.url]
                for item_url, kwdict in fake.items.get(self.url, []):
                    self.handle_url(item_url, dict(kwdict))
                self.status |= fake.status.get(self.url, 0)

            def handle_url(self, url: str, kwdict: dict[str, Any]) -> None:
                del url, kwdict

            def handle_queue(self, url: str, kwdict: dict[str, Any]) -> None:
                del url, kwdict

        return Job

    def _download_job_class(self, base: type) -> type:
        fake = self

        class DownloadJob(base):  # type: ignore[misc]
            def __init__(self, url: str, parent: object | None = None) -> None:
                super().__init__(url, parent)
                self.written: list[Path] = []

            def handle_url(self, url: str, kwdict: dict[str, Any]) -> None:
                del url
                if fake.download_delay:
                    time.sleep(fake.download_delay)
                directory = Path(fake.config.values["base-directory"])
                template = str(fake.config.values.get("filename", "{num}.{extension}"))
                name = template.format(
                    num=len(self.written) + 1, extension=kwdict.get("extension", "jpg")
                )
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / name
                target.write_bytes(b"\xff" * int(kwdict.get("_size", 1024)))
                self.written.append(target)

        return DownloadJob


def item(
    number: int, *, size: int = 1024, extension: str = "jpg", **fields: Any
) -> tuple[str, dict[str, Any]]:
    """Build one scripted item with sensible post metadata."""
    kwdict: dict[str, Any] = {
        "category": "fakegram",
        "id": "post123",
        "content": "A carousel of three",
        "author": "someone",
        "extension": extension,
        "num": number,
        "_size": size,
    }
    kwdict.update(fields)
    return (f"https://cdn.example.com/{number}.{extension}", kwdict)
