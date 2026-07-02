"""Self-upgrade command for medulla.

Reinstalls the latest version straight from the GitHub repository via pip/pipx.
There is no package registry — the GitHub main branch is the source of truth.
"""

import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version

PACKAGE = "medulla"
GIT_URL = "git+https://github.com/skopanev/medulla.git"


def _current_version() -> str:
    try:
        return pkg_version(PACKAGE)
    except PackageNotFoundError:
        return "dev"


def run_upgrade() -> int:
    current = _current_version()
    print(f"current: {current}")

    if current == "dev":
        print("running from source (not a pip install) — use `git pull` instead")
        return 0

    print(f"reinstalling latest from {GIT_URL} ...")

    # prefer pipx (host installs); fall back to pip (docker / venv installs)
    import shutil
    if shutil.which("pipx"):
        result = subprocess.run(["pipx", "install", "--force", GIT_URL])
    else:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", GIT_URL],
        )
    if result.returncode != 0:
        print("upgrade failed", file=sys.stderr)
        return result.returncode

    print(f"теперь я финалка ({_current_version()})")

    # Re-run init to sync the runtime into the project — but NOT inside the
    # container: there /workspace/.medulla is the mounted host project, and
    # run_init would point its symlinks at container-only paths, corrupting the
    # host. The container runs the pipx-installed medulla directly.
    import os
    if not os.environ.get("MEDULLA_DOCKER"):
        from .init import run_init
        run_init()

    return 0
