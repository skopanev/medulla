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

    # documented subcommands (before any flag parsing)
    if argv and argv[0] == "init":
        from .init import (bundled_templates, deploy_template, install_skill_md,
                           run_init, scaffold_pipeline)
        args = [a for a in argv[1:] if not a.startswith("-")]
        want_skill = "--skill" in argv
        if not args:
            names = ", ".join(bundled_templates()) or "none bundled"
            print("usage: medulla init <name> [--skill]", file=sys.stderr)
            print(f"  a bundled template name deploys that template ({names});",
                  file=sys.stderr)
            print("  any other name scaffolds a new pipeline;", file=sys.stderr)
            print("  --skill also registers SKILL.md with Claude Code", file=sys.stderr)
            return 1
        run_init()                          # project runtime (.medulla/), idempotent
        name = args[0]
        rc = deploy_template(name) if name in bundled_templates()             else scaffold_pipeline(name)
        if rc == 0 and want_skill:
            from pathlib import Path as _P
            rc = install_skill_md(name, _P(".medulla") / "pipelines" / name)
        return rc
    if argv and argv[0] == "upgrade":
        # two install methods exist: install.sh (venv at ~/.medulla/engine;
        # pre-4.0.4 installs used ~/.medulla-engine — the installer migrates)
        # and pipx. `pipx upgrade` on a venv install either errors or touches
        # a different copy — match the method.
        home = Path.home()
        installer_venvs = (home / ".medulla" / "engine" / "venv" / "bin" / "medulla",
                           home / ".medulla-engine" / "venv" / "bin" / "medulla")
        if any(p.exists() for p in installer_venvs):
            return subprocess.call(
                ["bash", "-c",
                 "curl -sSL https://raw.githubusercontent.com/skopanev/medulla/main/install.sh | bash"])
        return subprocess.call(["pipx", "upgrade", "medulla"])

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
