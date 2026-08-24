"""Single files the container needs: the entrypoint, and agy's Keychain-extracted keys.

Split from mounts.py under the project's 250-line rule ($MAX_LOC). These are mounts
that carry a FILE rather than a view of the workspace — the entrypoint script, and
credentials that exist only in the macOS Keychain until a run needs them.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent.parent


def _mount_init_docker(vols: list) -> None:
    # SCRIPT_DIR, not __file__.parent: this module lives one level down in dockerlib/
    # now, and init-docker.sh stayed beside docker.py. Getting it wrong mounts nothing
    # and the container dies on `exec /mnt/init-docker.sh: No such file or directory`
    # — with the file plainly present on the host, which reads as anything but a
    # missing mount.
    src = SCRIPT_DIR / "init-docker.sh"
    if src.is_file():
        vols.extend(["-v", f"{src}:/mnt/init-docker.sh:ro"])


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

    _mount(_keychain_get("gemini", "antigravity"), "/mnt/agy-token")
    _mount(_keychain_get("Antigravity Safe Storage", "Antigravity Key"), "/mnt/agy-safe-key")


