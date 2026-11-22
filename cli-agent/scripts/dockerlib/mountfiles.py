"""Single files the container needs: ours, and the ones a caller names.

Split from mounts.py under the project's 250-line rule ($MAX_LOC). These are mounts
that carry a FILE rather than a view of the workspace — the entrypoint script,
credentials that exist only in the macOS Keychain until a run needs them, --var-file
sources, and the files --mount-file names.

ONE WARNING COVERS EVERY MOUNT IN THIS FILE. A bind mount of a FILE is bound to its
INODE, not to its name. Replace the host file while a container holds it — write a
temporary file and rename it over the original, which is the correct way to publish a
file somebody may be reading — and that container loses the file outright for the rest
of the run. `cat` inside goes from the contents to "No such file or directory" the
instant the host renames. So a caller writes such a file ONCE before the run and
deletes it after the run, and never rewrites it in place.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent.parent

# Container paths medulla binds for its own purposes. Named once, used by the mounts
# below AND by the --mount-file guard, so the guard cannot drift from the real mount.
# A caller target that lands on one of these replaces something the run needs, and the
# failure then arrives far from the flag: a container that dies on a missing entrypoint,
# or an agy seat without its key.
INIT_DOCKER_TARGET = "/mnt/init-docker.sh"
AGY_TOKEN_TARGET = "/mnt/agy-token"
AGY_SAFE_KEY_TARGET = "/mnt/agy-safe-key"
DEFINITION_TARGET_ROOT = "/mnt/medulla-workflows"

RESERVED_TARGETS = (
    INIT_DOCKER_TARGET,
    AGY_TOKEN_TARGET,
    AGY_SAFE_KEY_TARGET,
    DEFINITION_TARGET_ROOT,
)


def _mount_init_docker(vols: list) -> None:
    # SCRIPT_DIR, not __file__.parent: this module lives one level down in dockerlib/
    # now, and init-docker.sh stayed beside docker.py. Getting it wrong mounts nothing
    # and the container dies on `exec /mnt/init-docker.sh: No such file or directory`
    # — with the file plainly present on the host, which reads as anything but a
    # missing mount.
    src = SCRIPT_DIR / "init-docker.sh"
    if src.is_file():
        vols.extend(["-v", f"{src}:{INIT_DOCKER_TARGET}:ro"])


def _mount_agy_keys(vols: list) -> None:
    import atexit
    import platform
    import tempfile
    if platform.system() != "Darwin":
        return

    def _keychain_get(service: str, account: str) -> str:
        try:
            return subprocess.check_output(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                stderr=subprocess.DEVNULL,
            ).strip().decode()
        except Exception:
            return ""

    def _mount(value: str, dst: str) -> None:
        if not value:
            return
        tmp = tempfile.NamedTemporaryFile(prefix="agy-", delete=False, mode="w", suffix=".txt")
        tmp.write(value)
        tmp.flush()
        tmp.close()
        atexit.register(lambda p=tmp.name: __import__("os").unlink(p) if __import__("os").path.exists(p) else None)
        vols.extend(["-v", f"{tmp.name}:{dst}:ro"])

    _mount(_keychain_get("gemini", "antigravity"), AGY_TOKEN_TARGET)
    _mount(_keychain_get("Antigravity Safe Storage", "Antigravity Key"),
           AGY_SAFE_KEY_TARGET)




def mount_var_files(vols: list, var_files: list) -> None:
    """--var-file sources, each at its own absolute path.

    The engine opens the path it was given, so the same string has to be valid on both
    sides of the boundary. Mounting the file where it already lives is what makes that
    true without rewriting the argument.
    """
    for src in var_files:
        vols.extend(["-v", f"{src}:{src}:ro"])


def _target_fault(target: str, taken: dict) -> str:
    """Why this container path cannot be used — empty when it can."""
    if not target.startswith("/"):
        if "/" not in target:
            # A docker habit: -v takes a trailing :ro. This flag does not, and the LAST
            # colon separates — so `file:/run/x:ro` reads the mode as the container
            # path. Say that, rather than leave an operator to re-read the flag.
            return ("the container path must be absolute, and the flag takes no mode "
                    "— it is read-only always")
        return "the container path must be absolute"
    parts = target.split("/")[1:]
    if not any(parts) or "" in parts or "." in parts or ".." in parts:
        return "the container path must be absolute and normalised"
    if target == "/workspace" or target.startswith("/workspace/"):
        # The daemon creates a missing target inside a bind mount, and for a file that
        # leaves an EMPTY FILE in the user's repository. Under --cwd-ro the workspace is
        # read-only by the time the nested mount applies, so the run dies before it
        # starts. Both were measured here, which is why this is refused rather than
        # repaired.
        return "the container path must not be inside /workspace"
    for reserved in RESERVED_TARGETS:
        if target == reserved or target.startswith(reserved + "/"):
            return f"the container path collides with medulla's own mount {reserved}"
    if target in taken:
        return f"the container path is already mounted from {taken[target]}"
    return ""


def parse_mount_file(value: str, taken: dict) -> tuple[Path, str]:
    """Read one --mount-file HOST:CONTAINER, or stop with the reason.

    The LAST colon separates, which is docker's own rule for -v. Neither side may
    contain a colon, and docker does not support one either.

    Every fault stops the run HERE, on the host, before an image is built or a
    container starts. A bad mount that reaches the daemon fails much later, and the
    message then names neither the flag nor the file: a bind mount whose source the
    daemon cannot use becomes an empty directory owned by root, and the run dies deep
    inside whatever tried to read it.
    """
    host_raw, sep, target = value.rpartition(":")
    if not sep or not host_raw or not target:
        raise SystemExit(f"error: --mount-file wants HOST:CONTAINER, got {value!r}")
    # THE TARGET FIRST. It is pure syntax, while the host side depends on what is on
    # the disk — and with a trailing :ro the last field is the mode, so the host side
    # then reads as a file that does not exist. "no such file" for a path the operator
    # can see is the least useful answer available.
    fault = _target_fault(target, taken)
    if fault:
        raise SystemExit(f"error: --mount-file {value}: {fault}")
    src = Path(host_raw).expanduser()
    if src.is_dir():
        raise SystemExit(f"error: --mount-file {host_raw}: this is a directory. "
                         f"--mount carries a directory.")
    if not src.is_file():
        raise SystemExit(f"error: --mount-file {host_raw}: no such file")
    return src.resolve(), target


def mount_secret_files(vols: list, mount_files: list) -> None:
    """--mount-file sources: one host file each, read-only, at the named path.

    READ-ONLY ALWAYS, and the flag takes no mode. A caller that wants the container to
    write a file wants a directory, which --mount-rw already carries. One flag with one
    meaning cannot be the door that hands back write access somebody took away.

    The host path rides in docker's argv, so `ps` shows it. The content stays private.
    A secret therefore belongs in the file, never in the file's NAME.
    """
    for src, target in mount_files:
        vols.extend(["-v", f"{src}:{target}:ro"])
