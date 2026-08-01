"""A scriptable stand-in for ``yt_dlp.YoutubeDL``.

The real engine is replaced at its narrowest seam - the factory that builds a
``YoutubeDL`` - so the adapter under test is the real one: real options, real
hooks, real error classification, real verification. Only the network is fake.

``FakeYoutubeDL`` can raise, write files into the directory the adapter told it
to use, and drive the progress hooks, which is everything needed to exercise
progress reporting, cancellation, ceilings and cleanup.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

ScriptStep = "Callable[[FakeYoutubeDL], None]"


def video_info(**overrides: Any) -> dict[str, Any]:
    """Return a plausible single-video info dictionary."""
    info: dict[str, Any] = {
        "id": "abc123",
        "title": "A Test Video",
        "extractor_key": "TestSite",
        "extractor": "testsite",
        "duration": 125,
        "ext": "mp4",
        "is_live": False,
        "uploader": "Someone",
        "upload_date": "20260101",
        "description": "A description.",
        "age_limit": 0,
        "filesize_approx": 12_000_000,
        "thumbnails": [
            {"url": "https://cdn.example.com/small.jpg", "width": 120, "height": 90},
            {"url": "https://cdn.example.com/big.jpg", "width": 1920, "height": 1080},
        ],
        "formats": [
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "width": 640,
                "height": 360,
                "fps": 30,
                "tbr": 700.5,
                "filesize": 11_000_000,
                "format_note": "360p",
            },
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "tbr": 4200.0,
                "filesize_approx": 60_000_000,
                "format_note": "1080p",
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128.0,
                "asr": 44100,
                "audio_channels": 2,
                "filesize": 2_000_000,
                "language": "en",
            },
            {
                "format_id": "sb0",
                "ext": "mhtml",
                "vcodec": "none",
                "acodec": "none",
                "format_note": "storyboard",
            },
        ],
    }
    info.update(overrides)
    return info


def playlist_info(entry_count: int = 12, **overrides: Any) -> dict[str, Any]:
    """Return a plausible playlist info dictionary."""
    info: dict[str, Any] = {
        "_type": "playlist",
        "id": "PL999",
        "title": "A Test Playlist",
        "extractor_key": "TestSite",
        "playlist_count": entry_count,
        "entries": [],
    }
    info.update(overrides)
    return info


class FakeYoutubeDL:
    """Records the options it was built with and replays a scripted behaviour."""

    instances: list[FakeYoutubeDL] = []  # noqa: RUF012 - a test-only registry

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        info: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        script: Sequence[Callable[[FakeYoutubeDL], None]] = (),
    ) -> None:
        """Capture the options and the behaviour to replay."""
        self.options = dict(options)
        self.info = dict(info) if info is not None else None
        self.error = error
        self.script = list(script)
        self.closed = False
        self.extract_calls: list[tuple[str, bool]] = []
        FakeYoutubeDL.instances.append(self)

    # -- The slice of the real API the adapter uses --------------------------

    def extract_info(self, url: str, *, download: bool = True) -> Mapping[str, Any] | None:
        """Replay the scripted behaviour and return the info dictionary.

        The script runs only when downloading: a probe writes nothing and fires
        no progress hooks, so one fake can serve both calls the way the real
        engine does.
        """
        self.extract_calls.append((url, download))
        if self.error is not None:
            raise self.error
        if download:
            for step in self.script:
                step(self)
        return self.info

    def close(self) -> None:
        """Record that the engine was released."""
        self.closed = True

    # -- Helpers for scripts -------------------------------------------------

    @property
    def home(self) -> Path:
        """Return the directory the adapter told the engine to write into."""
        paths = self.options.get("paths") or {}
        return Path(paths["home"])

    def progress(self, payload: Mapping[str, Any]) -> None:
        """Invoke every registered download progress hook."""
        for hook in self.options.get("progress_hooks", []):
            hook(dict(payload))

    def postprocessor(self, payload: Mapping[str, Any]) -> None:
        """Invoke every registered post-processor hook."""
        for hook in self.options.get("postprocessor_hooks", []):
            hook(dict(payload))

    def write_file(self, name: str, size_bytes: int = 1024) -> Path:
        """Create a file of ``size_bytes`` inside the engine's home directory."""
        path = self.home / name
        path.write_bytes(b"\0" * size_bytes)
        return path


def factory_for(
    *,
    info: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
    script: Sequence[Callable[[FakeYoutubeDL], None]] = (),
) -> Callable[[Mapping[str, Any]], FakeYoutubeDL]:
    """Return a ``YoutubeDLFactory`` that builds scripted fakes."""

    def build(options: Mapping[str, Any]) -> FakeYoutubeDL:
        return FakeYoutubeDL(options, info=info, error=error, script=script)

    return build


def writes(name: str, size_bytes: int = 1024) -> list[Callable[[FakeYoutubeDL], None]]:
    """Build a script whose only effect is to create one file."""

    def run(engine: FakeYoutubeDL) -> None:
        engine.write_file(name, size_bytes)

    return [run]


def download_script(
    *,
    name: str = "abc123.mp4",
    size_bytes: int = 4096,
    chunks: Sequence[int] = (),
    extras: Sequence[tuple[str, int]] = (),
) -> list[Callable[[FakeYoutubeDL], None]]:
    """Build a script that reports progress and writes the resulting files.

    Args:
        name: Name of the primary file the engine "downloads".
        size_bytes: Its size.
        chunks: Cumulative byte counts to report before finishing.
        extras: Additional ``(name, size)`` files, e.g. a thumbnail.
    """

    def run(engine: FakeYoutubeDL) -> None:
        for downloaded in chunks:
            engine.progress(
                {
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": size_bytes,
                    "speed": 1024.0,
                    "eta": 3,
                    "filename": str(engine.home / name),
                }
            )
        engine.write_file(name, size_bytes)
        for extra_name, extra_size in extras:
            engine.write_file(extra_name, extra_size)
        engine.progress(
            {
                "status": "finished",
                "downloaded_bytes": size_bytes,
                "total_bytes": size_bytes,
                "filename": str(engine.home / name),
            }
        )

    return [run]
