"""medulla CLI entry point.

Resolves the pipeline path, parses arguments, and dispatches to the runner.
This module is imported by:
  - the repo-root ``./medulla`` launcher (source mode)
  - ``python3 -m medulla`` against ``.medulla/`` (init mode, used by docker.py)
  - the pip-installed ``medulla`` console script (pip mode)

The ``_find_script`` helper handles all three modes when locating
``docker.py`` / ``host-builder.sh``:
  1. Inside the package (pip mode — scripts shipped as package data)
  2. Parent directory (source mode ``cli-agent/scripts/`` or init mode ``.medulla/scripts/``)
  3. ``.medulla/scripts/`` relative to CWD (pip user in an init'd workspace)
"""

import argparse
import atexit
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from .output import eprint, log, init_log_file, close_log_target, set_verbose
from .pipeline import load_pipeline, validate_pipeline
from .executor import runtime_diagnostics, confirm_non_docker_max_permissions
from .runner import run_pipeline


def _resolve_task_id(ns) -> str:
    """Resolve a stable per-run id so parallel runs in one workdir don't
    collide on shared state (vars file, log dir, bridge dir, pid files).

    Priority: explicit --var MEDULLA_TASK_ID=... > env MEDULLA_TASK_ID
    (set by parent docker.py / resume) > generated uuid hex.
    """
    for v in (ns.var or []):
        if "=" in v:
            k, val = v.split("=", 1)
            if k == "MEDULLA_TASK_ID" and val:
                return val
    existing = os.environ.get("MEDULLA_TASK_ID", "").strip()
    if existing:
        return existing
    return uuid.uuid4().hex[:8]


def _resolve_bridge_path(task_id: str) -> str:
    """Per-run host-builder bridge dir so cleanup of one run doesn't wipe
    another's bridge. Defaults to the legacy shared path when no task_id."""
    base = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    if task_id:
        return f"{base}/medulla-bridge-{task_id}"
    return f"{base}/medulla-bridge"


def _find_script(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "scripts" / name,           # pip mode (scripts inside package)
        here.parent / "scripts" / name,    # source/init mode (scripts sibling)
        Path(".medulla") / "scripts" / name,  # pip user with .medulla/ in cwd
    ):
        if candidate.is_file():
            return candidate
    return None


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="medulla", add_help=True)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("-w", "--workflow")
    parser.add_argument("--var", action="append", metavar="KEY=VALUE",
                        help="override pipeline variable (repeatable)")
    parser.add_argument("--mount", action="append", metavar="PATH",
                        help="mount extra folder in Docker, read-only (repeatable)")
    parser.add_argument("--mount-rw", action="append", metavar="PATH",
                        help="mount extra folder in Docker, read-write (repeatable)")
    parser.add_argument("--host", action="store_true",
                        help="auto-start host-builder.sh for native builds")
    parser.add_argument("--stage", metavar="STAGE",
                        help="skip to this stage (bypass earlier rounds)")
    parser.add_argument("command", nargs="?")
    parser.add_argument("pipeline", nargs="?")
    return parser.parse_known_args(argv)


def run_docker_passthrough(rest: list[str]) -> int:
    docker_py = _find_script("docker.py")
    if docker_py is None:
        eprint("error: docker.py not found")
        return 1
    os.execvp("python3", ["python3", str(docker_py), *rest])
    raise RuntimeError("unreachable")


def resolve_pipeline_path(workflow: str | None, command: str | None, pipeline: str | None) -> tuple[str, Path]:
    if workflow:
        # -w expects a path relative to cwd: <workflow>/pipeline.yaml
        return "run", Path(workflow) / "pipeline.yaml"
    if command in ("run", "validate", "graph") and pipeline:
        return command, Path(pipeline)
    raise ValueError("invalid command")


def main() -> int:
    ns, extra = parse_args(sys.argv[1:])

    # Resolve + publish the per-run id BEFORE anything reads shared state.
    # All isolation (vars file, log dir, bridge dir) keys off this env var.
    task_id = _resolve_task_id(ns)
    os.environ["MEDULLA_TASK_ID"] = task_id
    if not os.environ.get("MEDULLA_BRIDGE"):
        os.environ["MEDULLA_BRIDGE"] = _resolve_bridge_path(task_id)

    eprint(
        f"[medulla] startup pid={os.getpid()} ppid={os.getppid()} "
        f"docker={int(ns.docker)} cwd={Path.cwd()} task_id={task_id}"
    )

    set_verbose("--verbose" in extra)

    log(f"args: workflow={ns.workflow} command={ns.command} pipeline={ns.pipeline} extra={extra} docker={ns.docker}")

    if ns.command == "upgrade":
        from .upgrade import run_upgrade
        return run_upgrade()

    if ns.command == "init":
        from .init import run_init
        return run_init()

    if ns.command == "install-skill":
        from .install_skill import run_install_skill
        argv = ([ns.pipeline] if ns.pipeline else []) + extra
        return run_install_skill(argv)

    # Auto-start host-builder if --host
    host_proc = None
    if ns.host:
        host_script = _find_script("host-builder.sh")
        if host_script is not None:
            eprint(f"[medulla] starting host-builder: {host_script}")
            host_proc = subprocess.Popen(
                ["bash", str(host_script)],
                stdout=sys.stderr, stderr=sys.stderr,
            )
            # save PID so cleanup.py can kill it
            pid_file = Path(".medulla") / "host-builder.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(host_proc.pid))
            atexit.register(lambda: (host_proc.terminate(), host_proc.wait()))
            import time; time.sleep(1)  # let bridge dir be created
        else:
            eprint("warning: --host specified but host-builder.sh not found")

    if ns.docker:
        if not ns.workflow:
            eprint("error: --docker requires -w <workflow> (Dockerfile is resolved from pipeline vars)")
            eprint("usage: medulla --docker -w <workflow> [--stage <stage>] [--var K=V ...]")
            return 1
        rest = ["-w", ns.workflow]
        if ns.stage:
            rest += ["--stage", ns.stage]
        for v in (ns.var or []):
            rest += ["--var", v]
        for m in (ns.mount or []):
            rest += ["--mount", m]
        for m in (ns.mount_rw or []):
            rest += ["--mount-rw", m]
        if ns.command:
            rest.append(ns.command)
        if ns.pipeline:
            rest.append(ns.pipeline)
        rest += extra
        log(f"docker passthrough: {rest}")
        return run_docker_passthrough(rest)

    init_log_file()

    try:
        cmd, path = resolve_pipeline_path(ns.workflow, ns.command, ns.pipeline)
        log(f"resolved: cmd={cmd} path={path}")
    except ValueError:
        eprint(
            "usage:\n"
            "  medulla init\n"
            "  medulla upgrade\n"
            "  medulla install-skill <workflow> [--claude] [--cursor] [--project]\n"
            "  medulla --docker -w <workflow> [--stage S] [--var K=V]\n"
            "  medulla -w <workflow>\n"
            "  medulla run <pipeline.yaml>\n"
            "  medulla validate <pipeline.yaml>\n"
            "  medulla graph <pipeline.yaml>"
        )
        return 1

    try:
        log(f"loading pipeline: {path}")
        data = load_pipeline(path)
        log(f"loaded pipeline with {len(data.get('stages', {}))} stages")
        validate_pipeline(data)
        log("pipeline validation OK")
    except Exception as exc:
        eprint(f"error: {exc}")
        return 1

    if cmd == "validate":
        print(f"Valid: {len(data['stages'])} stages")
        return 0

    if cmd == "graph":
        from .graph import build_graph, open_file
        output_path = build_graph(path)
        print(f"Generated: {output_path}")
        open_file(output_path)
        return 0

    dry_run = "--dry-run" in extra
    verbose = "--verbose" in extra

    if os.environ.get("MEDULLA_DOCKER") != "1":
        confirm_rc = confirm_non_docker_max_permissions()
        if confirm_rc != 0:
            return confirm_rc

    cli_vars = {}
    for v in (ns.var or []):
        if "=" in v:
            k, val = v.split("=", 1)
            cli_vars[k] = val

    log(f"run_pipeline: dry_run={dry_run} verbose={verbose} cli_vars={cli_vars} stage={ns.stage}")
    runtime_diagnostics()
    return run_pipeline(path, dry_run=dry_run, verbose=verbose, cli_vars=cli_vars, start_stage=ns.stage)


def entry() -> int:
    """Entry point wrapper that handles KeyboardInterrupt and log cleanup."""
    try:
        return main()
    except KeyboardInterrupt:
        eprint(
            f"[medulla] top-level KeyboardInterrupt pid={os.getpid()} "
            f"pgid={os.getpgrp()} sigint_handler={signal.getsignal(signal.SIGINT)}"
        )
        eprint("cancelled by user")
        return 130
    finally:
        close_log_target()


if __name__ == "__main__":
    raise SystemExit(entry())
