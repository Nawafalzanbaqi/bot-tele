"""The gateway's process umask must keep the Bot API hand-off readable.

Regression test for 2026-09-28..10-01: a umask of 0o027 made every engine file
0640 owned by the app user, the local Bot API server (a different user, in a
different container, reading the workspace volume by path) could no longer
``stat`` the file, and every delivery failed with a 400 after a successful
download. The value is pinned here with the reason, so the next tightening is
made knowingly.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from mediahub.presentation.telegram.__main__ import HANDOFF_UMASK


class TestHandoffUmask:
    def test_a_file_created_under_the_umask_is_readable_by_other(self, tmp_path: Path) -> None:
        """What the Bot API server needs: read on the file, search on the directory."""
        previous = os.umask(HANDOFF_UMASK)
        try:
            lease = tmp_path / "lease"
            lease.mkdir()
            artifact = lease / "clip.mp4"
            artifact.write_bytes(b"\x00")
        finally:
            os.umask(previous)

        file_mode = stat.S_IMODE(artifact.stat().st_mode)
        dir_mode = stat.S_IMODE(lease.stat().st_mode)
        assert file_mode & stat.S_IROTH, f"engine files must stay other-readable: {oct(file_mode)}"
        assert dir_mode & stat.S_IXOTH, f"lease dirs must stay other-searchable: {oct(dir_mode)}"

    def test_the_umask_never_grants_write_to_group_or_other(self) -> None:
        assert HANDOFF_UMASK & 0o022 == 0o022
