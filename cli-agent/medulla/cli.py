"""`medulla` console entrypoint — the v2 engine plus the --docker exec boundary.

--docker re-invokes medulla inside the pipeline's image (scripts/docker.py owns
mounts/credentials); every other flag passes through to the v2 CLI untouched.
v1 is gone: this file is a thin shim, the engine lives in medulla.v2.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def entry() -> int:
    argv = sys.argv[1:]
    if "--docker" in argv:
        argv = [a for a in argv if a != "--docker"]
        docker_py = _find_docker_py()
        if docker_py is None:
            print("error: scripts/docker.py not found (medulla init lays it down)",
                  file=sys.stderr)
            return 1
        return subprocess.call([sys.executable, str(docker_py), *argv])

    from .v2.cli import main
    return main(argv)


def _find_docker_py() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".medulla" / "scripts" / "docker.py",
        here.parent / "scripts" / "docker.py",   # source: cli-agent/scripts
        here / "scripts" / "docker.py",          # installed: site-packages/medulla/scripts
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


if __name__ == "__main__":
    raise SystemExit(entry())
