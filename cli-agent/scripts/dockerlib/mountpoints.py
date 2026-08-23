"""Making the mount points a nested --mount needs, and taking them back.

Split from docker.py under the project's 250-line rule ($MAX_LOC).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dockerlib.paths import _remove_made_mountpoints


def prepare(volumes: list, extra_mounts: list, cwd_ro: bool) -> tuple[list[Path], int]:
    """Returns (points we created, exit code) — a non-zero code means stop."""
    # Mount extra folders into /workspace/<name> (nested mount inside workspace)
    #
    # A nested mount needs its mount POINT to exist, and the daemon lays /workspace down
    # first: under --cwd-ro it is read-only by the time the nested mount is applied, so
    # the daemon cannot create /workspace/<name> and the run dies before it starts. That
    # hits the one case the flag exists for — a panel launched from an empty box that
    # brings every repository in with --mount. So the point is made HERE, on the host,
    # where the directory is still writable, and removed again below.
    workspace_root = Path(os.environ.get("PWD") or os.getcwd())
    made_mountpoints: list[Path] = []
    for mount_path, ro in extra_mounts:
        p = Path(mount_path).resolve()
        if not p.is_dir():
            print(f"[docker.py] mount path not found: {p}", file=sys.stderr)
            _remove_made_mountpoints(made_mountpoints)
            return made_mountpoints, 1
        if cwd_ro and not ro:
            # A writable mount of the reviewed tree (or any part of it) hands back the
            # write access --cwd-ro just took away, through a second door.
            try:
                p.relative_to(workspace_root)
                inside = True
            except ValueError:
                inside = workspace_root == p
            if inside:
                print(f"[docker.py] --mount-rw {p} is inside the read-only workspace — "
                      f"that would undo --cwd-ro", file=sys.stderr)
                _remove_made_mountpoints(made_mountpoints)
                return made_mountpoints, 1
        if cwd_ro:
            point = workspace_root / p.name
            if not point.exists():
                # Remember EVERY level created, not just the leaf: mkdir(parents=True)
                # can make several, and rmdir on the leaf alone leaves the rest behind
                # in a tree we promised not to touch.
                missing = [q for q in [point, *point.parents]
                           if not q.exists() and workspace_root in q.parents]
                try:
                    point.mkdir(parents=True)
                except OSError as exc:
                    print(f"[docker.py] cannot make mount point {point}: {exc}",
                          file=sys.stderr)
                    _remove_made_mountpoints(made_mountpoints)
                    return made_mountpoints, 1
                made_mountpoints.extend(missing)
        suffix = ":ro" if ro else ""
        volumes.extend(["-v", f"{p}:/workspace/{p.name}{suffix}"])
    return made_mountpoints, 0
