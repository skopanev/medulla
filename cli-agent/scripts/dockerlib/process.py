"""Starting the container, and stopping it properly.

Split out of docker.py under the project's 250-line rule ($MAX_LOC). Everything here is
about the PROCESS: assembling the docker run command, attaching to it, and taking the
whole tree down on interruption — the part where a mistake leaves a container running
after the user thinks they stopped it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid

from dockerlib import env as dockerenv
from dockerlib import paths as dockerpaths
from dockerlib.env import _unlink_env_file


def terminate_process_group(proc: subprocess.Popen, force: bool = False) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pgid, sig)
    except Exception:
        pass


def kill_container(container_name: str) -> None:
    try:
        subprocess.Popen(
            ["docker", "kill", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def interactive_stdio() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()



# env keys harnesses need — filters the HOST SHELL env only (a shell carries
# hundreds of unrelated vars; forwarding it whole would leak). The .env tiers
# are the user's zone and forward WHOLE via --env-file, no filter.
HARNESS_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "ZHIPU_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "VERTEX_LOCATION",
    "INTERCOM_TOKEN",
    "INTERCOM_ADMIN_ID",
    "MEDULLA_RUN_ID",
    "MEDULLA_PIPELINE_ID",
    "MEDULLA_BRIDGE",
)

def build_run_command(image, volumes, args, container_name: str,
                      run_dir_name: str | None = None,
                      runs_under: str | None = None) -> list[str]:
    cmd = ["docker", "run", "--init", "--rm", "--name", container_name]
    if sys.stdin.isatty():
        cmd.append("-i")
    if interactive_stdio():
        cmd.append("-t")

    for key in HARNESS_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            cmd.extend(["-e", f"{key}={val}"])

    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        cmd.extend(["-e", f"GEMINI_API_KEY={os.environ['GOOGLE_API_KEY']}"])

    # Forward the merged .env tiers via --env-file, not -e: values on the
    # command line leak into `ps` during start and `docker inspect` forever.
    if dockerenv.env_file_for_run:
        cmd.extend(["--env-file", dockerenv.env_file_for_run])

    cmd.extend(volumes)
    # shadow: an empty tmpfs mounted OVER a workspace subpath — the more
    # specific mount wins, the container sees the dir empty, the host keeps
    # the real content. Host existence is irrelevant: tmpfs never touches it.
    for p in dockerpaths.shadow_paths_for_run:
        cmd.extend(["--tmpfs", f"/workspace/{p}"])
    cmd.extend(["-w", "/workspace"])
    # inside the container the sandbox IS the isolation: adapters (agy trust
    # preflight) key off this
    cmd.extend(["-e", "MEDULLA_DOCKER=1"])
    if runs_under:
        cmd.extend(["-e", f"MEDULLA_RUNS_UNDER={runs_under}"])
    if run_dir_name:
        cmd.extend(["-e", f"MEDULLA_RUN_DIR_NAME={run_dir_name}"])
    cmd.extend([image, "medulla"])
    cmd.extend(args)
    return cmd


def run_docker(image, volumes, args, runs_under: str | None = None,
               run_dir_name: str | None = None, keep_session: bool = False):
    """Run medulla in a container. keep_session: the workflow named an agent session,
    so the container is reused across nested runs and removed at the end of the
    pipeline rather than by --rm — a conversation lives in the CLI's own state inside
    $HOME, and a fresh container is a fresh conversation whatever id we hand it."""
    if keep_session:
        # local: session_run imports build_run_command from here, and a top-level
        # import either way closes the cycle — importing session_run first (a test
        # does) then fails on a half-built module
        from dockerlib.session_run import _run_kept
        return _run_kept(image, volumes, args, runs_under, run_dir_name)
    container_name = f"medulla-{uuid.uuid4().hex[:8]}"
    cmd = build_run_command(image, volumes, args, container_name,
                            run_dir_name=run_dir_name,
                            runs_under=runs_under)

    # single subprocess path (no execvp): the temp env-file must outlive the
    # docker client's startup read; stdio inheritance keeps -it interactive
    stdin = None if sys.stdin.isatty() else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdin=stdin, start_new_session=True)
    interrupted = {"count": 0}

    def run_sigint(signum, frame):
        interrupted["count"] += 1
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = -1
        try:
            msg = (
                f"\n[medulla] SIGINT docker.py count={interrupted['count']} "
                f"pid={proc.pid} pgid={pgid} container={container_name}\n"
            )
            os.write(2, msg.encode("utf-8", errors="replace"))
        except Exception:
            pass
        if interrupted["count"] == 1:
            print(f"\n  stopping container {container_name}...", file=sys.stderr, flush=True)
            kill_container(container_name)
            terminate_process_group(proc, force=False)
            return
        print(f"\n  force stopping container {container_name}...", file=sys.stderr, flush=True)
        kill_container(container_name)
        terminate_process_group(proc, force=True)
        sys.exit(130)

    prev = signal.signal(signal.SIGINT, run_sigint)
    try:
        proc.wait()
        if interrupted["count"] > 0:
            return 130
        return proc.returncode
    finally:
        signal.signal(signal.SIGINT, prev)
        _unlink_env_file()
