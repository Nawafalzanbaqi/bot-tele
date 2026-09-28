"""Checks that what landed on disk is the media it claims to be.

A download can end "successfully" with a file that is not a video: a container
whose data stopped a third of the way through, a merge that wrote only one of
its two streams, a body that was an HTML error page with an ``.mp4`` name. The
engine reports the bytes it received, not whether they decode, and the
destination accepts anything of the right size. Nothing between the two used to
look inside the file.

This module does, with ``ffprobe`` - which is on the image already, because the
merge needs its sibling. It answers three questions: does the file hold at least
one decodable stream, does it hold the *kind* of stream that was asked for, and
is it as long as the source said it would be. A file that fails is refused
before upload, with a code the chat can turn into "it arrived incomplete, try
again" rather than a silent poster that will not play.

Kept separate from the adapter so the check can be injected, faked in tests,
and switched off on a device without the tool.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from loguru import logger

from mediahub.application.download.errors import DownloadIncompleteError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

INSPECT_TIMEOUT_SECONDS: Final[float] = 60.0
"""How long one ``ffprobe`` run may take.

Reading container headers is milliseconds; a minute is for the pathological
file that makes the tool scan to the end, and for a device under load.
"""

DURATION_TOLERANCE_SECONDS: Final[float] = 5.0
DURATION_TOLERANCE_RATIO: Final[float] = 0.05
"""How much shorter than declared a file may be and still count as complete.

The larger of the two applies. Sources round their durations, fragmented
formats end on a segment boundary, and a merge drops a few frames at the join;
five seconds or five percent covers all of that on every source measured, and
a truncated download is short by far more.
"""

_FFPROBE_ARGUMENTS: Final[tuple[str, ...]] = (
    "-v",
    "error",
    "-show_entries",
    "format=duration:stream=codec_type,codec_name,width,height",
    "-of",
    "json",
)


@dataclass(frozen=True, slots=True)
class StreamReport:
    """What an inspection found inside a file.

    Attributes:
        duration_seconds: Container duration, when the file declares one.
        video_codecs: Codec names of the video streams, in file order.
        audio_codecs: Codec names of the audio streams, in file order.
        width: Frame width of the first video stream, when known.
        height: Frame height of the first video stream, when known.
    """

    duration_seconds: float | None
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]
    width: int | None = None
    height: int | None = None

    @property
    def has_video(self) -> bool:
        """Return whether at least one video stream was found."""
        return bool(self.video_codecs)

    @property
    def has_audio(self) -> bool:
        """Return whether at least one audio stream was found."""
        return bool(self.audio_codecs)

    @property
    def has_streams(self) -> bool:
        """Return whether anything decodable was found at all."""
        return self.has_video or self.has_audio


class StreamInspector(Protocol):
    """Looks inside a media file."""

    async def inspect(self, path: Path) -> StreamReport | None:
        """Describe the streams in ``path``.

        Returns ``None`` when the inspection could not be *performed* - the tool
        is missing or hung - which is a fact about this device, not the file,
        and is reported as a skipped check rather than a failed one. A file the
        tool rejects yields a report with no streams.
        """
        ...


class FfprobeInspector:
    """Inspects files with ``ffprobe``."""

    __slots__ = ("_executable", "_missing", "_timeout")

    def __init__(
        self, executable: str = "ffprobe", *, timeout_seconds: float = INSPECT_TIMEOUT_SECONDS
    ) -> None:
        """Bind to an executable, remembered as absent after the first failure to start it."""
        self._executable = executable
        self._timeout = timeout_seconds
        self._missing = False

    async def inspect(self, path: Path) -> StreamReport | None:
        """Run ``ffprobe`` on ``path`` and parse its JSON."""
        if self._missing:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                *_FFPROBE_ARGUMENTS,
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError):
            # Said once. Every download after this one would otherwise log the
            # same line, and the fix is the same each time: install ffmpeg.
            self._missing = True
            logger.bind(executable=self._executable).warning(
                "The stream inspector is not available; delivered files are not being "
                "checked for completeness"
            )
            return None

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.bind(name=path.name).warning("Inspecting a file took too long; check skipped")
            return None

        if process.returncode != 0:
            # The tool ran and could make nothing of the file. That *is* an
            # answer: whatever this is, it is not playable media.
            logger.bind(name=path.name, detail=stderr.decode(errors="replace").strip()[:200]).info(
                "The stream inspector could not read the file"
            )
            return StreamReport(duration_seconds=None, video_codecs=(), audio_codecs=())
        return parse_report(stdout.decode(errors="replace"))


def parse_report(text: str) -> StreamReport:
    """Turn ``ffprobe -of json`` output into a :class:`StreamReport`.

    Lenient on purpose: a field the tool omits is unknown, not an error, and
    output that is not JSON at all is a file with no streams.
    """
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return StreamReport(duration_seconds=None, video_codecs=(), audio_codecs=())
    if not isinstance(payload, dict):
        return StreamReport(duration_seconds=None, video_codecs=(), audio_codecs=())

    video: list[str] = []
    audio: list[str] = []
    width: int | None = None
    height: int | None = None
    streams = payload.get("streams")
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        codec = str(stream.get("codec_name") or "unknown")
        kind = stream.get("codec_type")
        if kind == "video":
            video.append(codec)
            if width is None:
                width = _integer(stream.get("width"))
                height = _integer(stream.get("height"))
        elif kind == "audio":
            audio.append(codec)

    duration: float | None = None
    container = payload.get("format")
    if isinstance(container, dict):
        duration = _number(container.get("duration"))

    return StreamReport(
        duration_seconds=duration,
        video_codecs=tuple(video),
        audio_codecs=tuple(audio),
        width=width,
        height=height,
    )


def verify_complete(
    report: StreamReport,
    *,
    expected_seconds: float | None,
    expect_video: bool,
    url: str,
) -> None:
    """Refuse a file that is not the media that was asked for.

    Args:
        report: What the inspection found.
        expected_seconds: The duration the source declared, when it did.
        expect_video: Whether the selection was for video. An audio-only
            request is complete without a picture.
        url: For the message; never reaches the chat, which renders the code.

    Raises:
        DownloadIncompleteError: If the file has no decodable stream, lacks the
            video stream that was asked for, or is materially shorter than the
            source declared.
    """
    if not report.has_streams:
        message = f"'{url}' produced a file with no decodable stream"
        raise DownloadIncompleteError(message)
    if expect_video and not report.has_video:
        message = f"'{url}' produced a file without the video stream that was requested"
        raise DownloadIncompleteError(message)
    if expected_seconds is None or report.duration_seconds is None:
        return
    tolerance = max(DURATION_TOLERANCE_SECONDS, expected_seconds * DURATION_TOLERANCE_RATIO)
    if report.duration_seconds < expected_seconds - tolerance:
        message = (
            f"'{url}' produced a file of {report.duration_seconds:.1f}s where "
            f"{expected_seconds:.1f}s was declared"
        )
        raise DownloadIncompleteError(
            message,
            expected_seconds=expected_seconds,
            actual_seconds=report.duration_seconds,
        )


def _number(value: Any) -> float | None:
    """Return a float from ffprobe's string-typed numbers, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _integer(value: Any) -> int | None:
    """Return a positive integer, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
