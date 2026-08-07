"""`medulla` entrypoint — v2 engine plus direct container-runtime dispatch.

--docker selects Docker, --apple selects Apple Container, and no runtime flag
runs on the host. Every other flag passes through to the v2 CLI untouched.
v1 is gone: this file is a thin shim, the engine lives in medulla.v2.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def entry() -> int:
    argv = sys.argv[1:]

    if "--docker" in argv and "--apple" in argv:
        print("error: --docker and --apple are mutually exclusive", file=sys.stderr)
        return 1

    # No arguments is a question, not an error. argparse answers it with a usage line
    # that says which flags exist and nothing about which workflows do — and a wrong
    # -w path is the single most common way a run dies. `medulla help` prints the
    # resolvable ones, by name, for this machine.
    if not argv or argv[0] in ("help", "--help-run"):
        from .help import print_help
        return print_help()

    # documented subcommands (before any flag parsing)
    if argv and argv[0] == "refresh":
        from .init import bundled_templates, refresh_skill
        rest, pos, depth, dry = argv[1:], [], None, False
        if "--help" in rest:
            print("usage: medulla refresh <name> <root> [--depth N] [--dry-run]")
            print("  rescans <root>, refreshes every medulla-owned workflow and skill copy")
            return 0
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--dry-run":
                dry = True
            elif a == "--depth":
                if i + 1 >= len(rest) or not rest[i + 1].isdecimal() or int(rest[i + 1]) < 1:
                    print("error: --depth needs a positive integer, e.g. --depth 8", file=sys.stderr)
                    return 1
                depth = int(rest[i + 1]); i += 1
            elif a.startswith("-"):
                print(f"error: unknown flag: {a}", file=sys.stderr)
                return 1
            else:
                pos.append(a)
            i += 1
        if not pos:
            print("usage: medulla refresh <name> <root> [--depth N] [--dry-run]", file=sys.stderr)
            print("  rescans <root>, refreshes every medulla-owned copy of the skill", file=sys.stderr)
            print("  (.medulla/workflows/<name> + {.claude,.agents,.opencode}/skills/<name>) to the bundle", file=sys.stderr)
            print(f"  bundled: {', '.join(bundled_templates()) or 'none'}", file=sys.stderr)
            return 1
        name = pos[0]
        if len(pos) < 2:
            print("error: no search folder given — WHERE should I look?", file=sys.stderr)
            print(f"  usage: medulla refresh {name} <root> [--depth N]   e.g. medulla refresh {name} ~/Projects --depth 4",
                  file=sys.stderr)
            return 1
        kw = {"dry_run": dry} if depth is None else {"depth": depth, "dry_run": dry}
        return refresh_skill(name, pos[1], **kw)

    if argv and argv[0] == "init":
        from .init import bundled_templates, deploy_template, install_skill_md, run_init, scaffold_workflow
        rest, pos, want_skill, want_apple, local = argv[1:], [], False, False, False
        if "--help" in rest:
            print("usage: medulla init <name> [--skill [--apple]] [--local]")
            print("  deploy a bundled workflow or scaffold a new one")
            print("  --skill registers SKILL.md with claude-code, codex, and opencode")
            print("  --apple makes the installed skill use Apple Container")
            print("          bundled Docker skills stay on Docker by default")
            print("  --local installs into this project instead of machine-wide")
            return 0
        for arg in rest:
            if arg == "--skill":
                want_skill = True
            elif arg == "--apple":
                want_apple = True
            elif arg == "--local":
                local = True
            elif arg.startswith("-"):
                print(f"error: unknown flag: {arg}", file=sys.stderr)
                return 1
            else:
                pos.append(arg)
        if not pos:
            names = ", ".join(bundled_templates()) or "none bundled"
            print("usage: medulla init <name> [--skill [--apple]] [--local]", file=sys.stderr)
            print(f"  a bundled template name deploys that template ({names});",
                  file=sys.stderr)
            print("  any other name scaffolds a new workflow;", file=sys.stderr)
            print("  --skill also registers SKILL.md with the agent CLIs", file=sys.stderr)
            print("  --local installs into THIS project instead of machine-wide;",
                  file=sys.stderr)
            print("    a local copy always wins over the machine-wide one", file=sys.stderr)
            return 1
        if len(pos) != 1:
            print("error: init accepts exactly one workflow name", file=sys.stderr)
            return 1
        if want_apple and not want_skill:
            print("error: init --apple requires --skill", file=sys.stderr)
            return 1
        run_init()                          # project runtime (.medulla/), idempotent
        name = pos[0]
        # A template is the same everywhere, so it installs once per machine and every
        # repo resolves it; a scaffold is new work belonging to the repo you are in.
        bundled = name in bundled_templates()
        wdir = ((Path(".medulla") if local or not bundled else Path.home() / ".medulla")
                / "workflows" / name)
        if want_skill and (wdir / "workflow.yaml").is_file():
            print(f"using existing workflow '{name}' -> {wdir}/")
            rc = 0
        else:
            rc = (deploy_template(name, local=local) if bundled
                  else scaffold_workflow(name))
        if rc == 0 and want_skill:
            rc = install_skill_md(name, wdir, local=local, apple=want_apple)
        return rc
    if argv and argv[0] == "launch":
        from .launch import launch
        return launch(argv[1:])

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
        docker_py = _find_script("docker.py")
        if docker_py is None:
            print("error: scripts/docker.py not found (medulla init lays it down)",
                  file=sys.stderr)
            return 1
        return subprocess.call([sys.executable, str(docker_py), *argv])

    if "--apple" in argv:
        argv = [a for a in argv if a != "--apple"]
        if any(arg in ("-h", "--help") for arg in argv):
            from .v2.cli import main
            try:
                return main(["--help"])
            except SystemExit as exc:
                return int(exc.code or 0)
        runner = _find_script("apple_container.py")
        if runner is None:
            print("error: scripts/apple_container.py not found (run `medulla init`)",
                  file=sys.stderr)
            return 1
        return _exec_runner(runner, argv)

    from .v2.cli import main
    return main(argv)


def _find_docker_py() -> Path | None:
    return _find_script("docker.py")


def _exec_runner(runner: Path, argv: list[str]) -> int:
    """Replace the shim so one process owns Apple runtime signal cleanup."""
    os.execv(sys.executable, [sys.executable, str(runner), *argv])
    return 1


def _find_script(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".medulla" / "scripts" / name,
        here.parent / "scripts" / name,   # source: cli-agent/scripts
        here / "scripts" / name,          # installed: site-packages/medulla/scripts
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


if __name__ == "__main__":
    raise SystemExit(entry())
