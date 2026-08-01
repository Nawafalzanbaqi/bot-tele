"""The lease manifest: what a directory says about itself.

A directory cannot answer "who owns you, and were you mid-download or already
being deleted?", and after a crash those are the only questions that matter. So
every lease writes a small JSON file beside its files, and recovery reads it.

Three decisions here are deliberate:

* **The manifest sits outside the artifact directory.** Artifacts live in
  ``<lease>/files/``; the manifest is ``<lease>/lease.json``. A bookkeeping file
  that appeared in :meth:`WorkspaceScope.names` would be delivered as media by
  something, eventually.
* **It is written atomically and durably.** Temp file, ``fsync``, rename,
  ``fsync`` of the directory. The rename alone is atomic but *not* durable: on a
  power cut the new name can be visible with none of its bytes behind it, which
  is the corrupt-but-parseable state this file exists to avoid. Two syncs on a
  400-byte file, once per lease, is a price worth paying on the device where a
  power cut is a weekly event rather than an incident.
* **Reading never raises.** Anything unreadable, malformed, from a future
  version or failing domain validation comes back as ``None`` and is treated as
  a directory nobody claims. A recovery sweep that can be stopped by one bad
  file is a sweep that stops running.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from mediahub.domain.common.errors import DomainError
from mediahub.domain.workspace.entities import WorkspaceLease
from mediahub.domain.workspace.enums import LeaseState
from mediahub.domain.workspace.identifiers import LeaseId
from mediahub.domain.workspace.value_objects import LeaseOwner
from mediahub.infrastructure.workspace import containment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

MANIFEST_NAME: Final[str] = "lease.json"
"""Name of the manifest inside a lease directory."""

STAGING_NAME: Final[str] = f".{MANIFEST_NAME}.writing"
"""Where a manifest is assembled before the rename that publishes it."""

ARTIFACT_DIRECTORY_NAME: Final[str] = "files"
"""Subdirectory holding the artifacts, and the only thing a lease exposes."""

SCHEMA_VERSION: Final[int] = 1
"""Bumped when the shape changes. An unknown version reads as unclaimed rather
than as a guess, because guessing here deletes somebody's download."""

_ENCODING: Final[str] = "utf-8"


def write(directory: Path, lease: WorkspaceLease) -> None:
    """Write ``lease``'s manifest into ``directory``, atomically and durably.

    Args:
        directory: The lease directory, which must already exist.
        lease: The lease to record.

    Raises:
        OSError: If the manifest cannot be written. A lease whose manifest
            failed is a lease nothing can recover, so this one is fatal.
    """
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "lease_id": str(lease.id),
        "label": lease.label,
        "state": lease.state.value,
        "owner": {"identity": lease.owner.identity, "process_id": lease.owner.process_id},
        "reserved_bytes": lease.reserved_bytes,
        "used_bytes": lease.used_bytes,
        "created_at": lease.created_at.isoformat(),
        "updated_at": lease.updated_at.isoformat(),
    }
    target = directory / MANIFEST_NAME
    staging = directory / STAGING_NAME
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode(_ENCODING)

    # Written through a descriptor rather than `write_text` so the bytes can be
    # forced to the platter before the rename makes them the truth. Without the
    # sync the rename is still atomic and the file is still empty.
    with staging.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    staging.replace(target)
    containment.fsync_directory(directory)


def clear_staging(directory: Path) -> bool:
    """Remove a manifest staging file left behind by an interrupted write.

    A crash between creating the staging file and renaming it leaves debris that
    nothing reads and nothing deletes - harmless individually, and a slow leak
    of inodes on a device that runs for years.

    Args:
        directory: The lease directory to tidy.

    Returns:
        Whether anything was removed.
    """
    staging = directory / STAGING_NAME
    try:
        staging.unlink()
    except OSError:
        return False
    return True


def read(directory: Path) -> WorkspaceLease | None:
    """Return the lease recorded in ``directory``, or ``None``.

    Args:
        directory: A lease directory.

    Returns:
        The reconstructed lease, or ``None`` when there is no manifest, it
        cannot be parsed, or it does not describe a lease this version
        understands.
    """
    path = directory / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding=_ENCODING))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
        return None
    try:
        return _to_lease(payload)
    except (DomainError, KeyError, TypeError, ValueError):
        logger.bind(manifest=str(path)).debug("Ignoring an unreadable lease manifest")
        return None


def _to_lease(payload: dict[str, Any]) -> WorkspaceLease:
    """Rebuild a lease from a parsed manifest, validating as the domain would."""
    owner = payload["owner"]
    return WorkspaceLease(
        lease_id=LeaseId(str(payload["lease_id"])),
        owner=LeaseOwner(
            identity=str(owner["identity"]),
            process_id=int(owner["process_id"]),
        ),
        label=str(payload["label"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
        reserved_bytes=int(payload["reserved_bytes"]),
        used_bytes=int(payload["used_bytes"]),
        state=LeaseState(str(payload["state"])),
    )
