"""The one function that turns a name into a path.

``docs/architecture/14-security-architecture.md`` §14.4 asks for exactly one
place to audit, and this is it. Everything the workspace writes, reads, deletes
or hands to an external tool goes through :func:`resolve_within`; nothing else
in the codebase joins a path to the workspace root.

Two distinct attacks are covered, and they need different answers:

* **Traversal.** ``../../etc/passwd`` is refused by resolving the candidate and
  proving the result is still under the lease root. Comparing unresolved paths
  is not enough - the traversal happens in the kernel, not in the string.
* **Symlinks.** A resolved path that stays inside the root can still *be* a
  symlink, or sit behind one. Reading through it is a containment failure and
  writing through it is worse, so :func:`open_exclusive` refuses to follow a
  link at all: the file it creates is created by it, or it fails.

Both are one-way. There is no flag to relax them and no fallback that skips
them, because the moment a caller can opt out, every caller eventually does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from mediahub.domain.workspace.errors import PathEscapesWorkspaceError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
"""``O_NOFOLLOW`` where the platform has it; Windows relies on ``O_EXCL``."""

_BINARY: Final[int] = getattr(os, "O_BINARY", 0)
"""``O_BINARY`` on Windows, where text mode would otherwise mangle bytes."""

_CREATE_FLAGS: Final[int] = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW | _BINARY

_FILE_MODE: Final[int] = 0o600
"""Owner-only. Downloaded media is personal content on a shared device."""


def resolve_within(root: Path, candidate: Path | str) -> Path:
    """Return ``candidate`` resolved, proving it stays under ``root``.

    Args:
        root: The lease directory. Resolved too, so a workspace reached through
            a symlinked mount does not fail its own check.
        candidate: An absolute path, or one relative to ``root``.

    Returns:
        The resolved, contained path.

    Raises:
        PathEscapesWorkspaceError: If the resolved path is anywhere else.
    """
    base = root.resolve()
    target = Path(candidate)
    absolute = target if target.is_absolute() else base / target
    resolved = absolute.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise PathEscapesWorkspaceError(str(candidate))
    return resolved


def is_within(root: Path, candidate: Path | str) -> bool:
    """Return whether ``candidate`` really resolves inside ``root``."""
    try:
        resolve_within(root, candidate)
    except PathEscapesWorkspaceError:
        return False
    return True


def open_exclusive(path: Path) -> int:
    """Create ``path`` and return a file descriptor open for writing.

    Exclusive creation is what makes the temporary file safe: if anything is
    already at that path - a leftover, a planted symlink, another attempt - the
    call fails rather than writing through it. On platforms that have
    ``O_NOFOLLOW`` a symlink is refused explicitly as well.

    Args:
        path: Where to create the file. Must already be contained.

    Returns:
        An open file descriptor, owned by the caller.

    Raises:
        FileExistsError: If anything already exists at ``path``.
        OSError: If the file cannot be created.
    """
    return os.open(path, _CREATE_FLAGS, _FILE_MODE)


def iter_regular_files(directory: Path) -> Iterator[Path]:
    """Yield every regular file under ``directory``, never following links.

    ``Path.rglob`` walks into symlinked directories, which would let a link
    inside a lease enrol the whole filesystem in a size calculation. This walks
    real directories only.
    """
    if not directory.is_dir() or directory.is_symlink():
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from iter_regular_files(entry)
        elif entry.is_file():
            yield entry


def iter_entries(directory: Path) -> Iterator[Path]:
    """Yield every entry under ``directory``, links included but not followed.

    The counterpart to :func:`iter_regular_files`: consistency checking needs to
    *see* a symlink in order to refuse it, and must still not walk through one.
    """
    if not directory.is_dir() or directory.is_symlink():
        return
    for entry in sorted(directory.iterdir()):
        yield entry
        if entry.is_dir() and not entry.is_symlink():
            yield from iter_entries(entry)


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry to disk, where the platform supports it.

    A rename is atomic but not necessarily durable: without this, a power cut
    moments after publishing an artifact can leave the file present and its
    directory entry not. Windows has no directory handle to sync, and failing
    there would be worse than the guarantee is worth.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(descriptor)
