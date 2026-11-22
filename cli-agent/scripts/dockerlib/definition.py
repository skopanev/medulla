"""Where the workflow definition comes from, when it is not in the workspace.

Carved out of docker.py under the project's 250-line rule ($MAX_LOC). A shared
definition lives in ~/.medulla/workflows/<name>, outside the mounted workspace, so it
has to be brought in — and the HOW has been wrong twice in ways that cost a day each.
Both reasons are recorded below, because the fix reads like an arbitrary choice without
them.
"""
from __future__ import annotations

from pathlib import Path

from dockerlib import image as dockerimage
from dockerlib import paths as dockerpaths


def mount_shared_definition(volumes: list, args: list[str],
                            workflow) -> tuple[list[str], str | None]:
    """Mount a shared definition and point the engine at it.

    Returns the engine arguments, with -w rewritten to the container path, and the
    place the run history belongs — or None when the definition is already inside the
    workspace and needs no mount.

    Mount it OUTSIDE /workspace, never into it. A mount target inside a bind mount is
    created by the daemon when it is missing, and for a file target that means an EMPTY
    FILE appears in the user's repository. That debris then outranked the real shared
    definition and broke every run, and the symptom read as panelists that failed to
    deliver — a day went into provider quotas. Whether the daemon creates the target
    changes with the storage driver, which is exactly why the workspace must not be the
    target at all. The run history still lands in the project: MEDULLA_RUNS_UNDER tells
    the engine where, because the yaml now sits on a read-only path.
    """
    if not workflow:
        return args, None
    resolved = dockerimage._config_yaml(Path(workflow))
    if not dockerpaths.definition_is_outside_workspace(resolved):
        return args, None
    if not resolved.is_file():
        return args, None
    dest = dockerpaths.runs_under_for(Path(workflow))
    name = resolved.parent.name
    mnt = f"/mnt/medulla-workflows/{name}"
    # MOUNT THE DIRECTORY, NOT THE FILE. A bind mount of a FILE is bound to its inode,
    # and `medulla refresh` replaces definitions atomically — write a temporary file,
    # os.replace it into place — which is the correct way to publish a file that
    # somebody may be reading. But the new name points at a NEW inode, and every
    # container that already mounts the old one loses it outright: measured here, `cat`
    # inside the container goes from the file's contents to "No such file or directory"
    # the instant the host replaces it.
    #
    # That is the FileNotFoundError on medulla's own workflow.yaml that killed rounds
    # all day. It looked environmental because it only hits rounds that start inside
    # the second or two a refresh takes, and it presents as a healthy start: the
    # launcher prints the run directory and the pid, then nothing is ever created. Two
    # lanes three seconds apart, with different flags, died identically.
    #
    # A directory mount survives it: the directory keeps its inode while the files
    # inside it are replaced, and the container sees the new content. Verified both
    # ways before this changed.
    volumes.extend(["-v", f"{resolved.parent}:{mnt}:ro"])
    # EVERY directory the workflow ships, not a hardcoded list. It was ("prompts",) —
    # so scripts/ never reached the container, and the synthesize node's
    # `$MEDULLA_WORKFLOW_DIR/scripts/spar-run.sh` did not exist there. The panel then
    # wrote no verdict.md at all while the node still reported success. A workflow that
    # ships a directory ships it because a node needs it. runs/ is the one exception,
    # being history rather than definition: it comes too, read-only and unread, because
    # shadowing it would mean a second mount inside this one, and a nested mount is
    # what the file mount already cost us.
    args = [f"{mnt}/workflow.yaml" if a == str(workflow) else a for a in args]
    # RELATIVE, not /workspace/...: --print-run-dir hands this path back to the caller,
    # who stands on the HOST while the run happened inside the container. An absolute
    # container path does not exist for them — found live, when a panel printed a run
    # directory that could not be listed.
    return args, str(dest)
