"""Running a workflow inside a container that outlives it.

Split from process.py under the project's 250-line rule ($MAX_LOC). The ordinary path
is one `docker run --rm` per run; this one keeps the container so an agent conversation
survives host steps in between, which makes readiness — not just startup — something
the caller has to wait for.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid

from dockerlib import keep
from dockerlib import paths as dockerpaths
from dockerlib.process import (
    build_run_command,
    docker_client_env,
    forwarded_env_values,
    interactive_stdio,
)


def _run_kept(image, volumes, args, runs_under, run_dir_name):
    """The session path: an IDLE container, entered once the entrypoint has finished.

    The first version started the container with the workflow as its command and then
    `docker exec`-ed the same workflow into it. Two runs, and the exec bypassed the
    entrypoint — so it ran whatever medulla the image was built with while the
    entrypoint was still upgrading. Seen live: 4.27.4 answering inside a container
    mid-upgrade to 4.39.1, and 78 seconds of two workflows racing each other.

    So: start it idle, WAIT for the readiness marker the entrypoint writes after the
    upgrade, then exec exactly once.

    Ownership decides who removes it. MEDULLA_PIPELINE_ID is set by the outermost run
    and inherited by every nested one, so a pipeline is one container however many runs
    it starts; that run removes it on the way out. Without an id above us we are the
    top and clean up here.
    """
    keep.sweep_stale()                     # belt: a pipeline that never came back
    owner = keep.owner_id() or f"top-{uuid.uuid4().hex[:8]}"
    mine = keep.owner_id() == ""           # nobody above us will clean up
    # The spec joins the name: a pipeline that runs two workflows with different
    # images, mounts or --cwd-ro gets a container each, instead of the second silently
    # inheriting the first one's world.
    spec = keep.spec_digest(image, volumes, dockerpaths.shadow_paths_for_run)
    name = keep.container_name(owner, spec)

    if not keep.is_running(name):
        # `sleep infinity` as the command: the entrypoint still runs (tini execs it),
        # upgrades medulla, writes the marker, and only then hands over to the idle
        # process. No caller credentials or workflow code run here.
        cmd = build_run_command(
            image, volumes, ["--version"], name, forward_env=False,
        )
        cmd = [c for c in cmd if c not in ("--rm", "-i", "-t")]
        at = cmd.index("--name")
        cmd[at:at] = ["-d", "--label", f"{keep.LABEL}={owner}",
                      "--label", f"{keep.SPEC_LABEL}={spec}"]
        tail = cmd.index(image)
        cmd[tail:] = [image, "sleep", "infinity"]
        started = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=docker_client_env(),
        )
        if started.returncode != 0:
            # Losing the election is not an error. Two first callers can both see the
            # container absent and both try to create it; the loser gets "name already
            # in use" and should JOIN rather than fail the pipeline.
            if not keep.is_running(name):
                print(started.stderr.strip(), file=sys.stderr)
                return started.returncode
            print(f"[medulla] joining session container {name}", file=sys.stderr)
        else:
            print(f"[medulla] session container {name} starting", file=sys.stderr)
    else:
        print(f"[medulla] reusing session container {name}", file=sys.stderr)

    # EVERY caller waits, not just the one that created it. Waiting only in the create
    # branch left the second caller free to exec while the entrypoint was still
    # upgrading — the exact race the idle-start rewrite existed to remove, reintroduced
    # for anyone who arrives second. A warm container answers on the first probe, so
    # this costs one `docker exec test`.
    if not _wait_ready(name):
        print(f"[medulla] {name} never became ready — removing it", file=sys.stderr)
        keep.remove(name)
        return 1

    cmd = ["docker", "exec"]
    if sys.stdin.isatty():
        cmd.append("-i")
    if interactive_stdio():
        cmd.append("-t")
    cmd += ["-w", "/workspace"]
    # THIS call supplies only names; docker reads values from the child environment.
    # `docker exec --env-file` exists, but no temp file is needed. Creation-time
    # values would remain clear in Config.Env, so the idle container gets none.
    for key in forwarded_env_values():
        cmd += ["-e", key]
    if runs_under:
        cmd += ["-e", "MEDULLA_RUNS_UNDER"]
    if run_dir_name:
        cmd += ["-e", "MEDULLA_RUN_DIR_NAME"]
    cmd += [name, "medulla", *args]

    stdin = None if sys.stdin.isatty() else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        stdin=stdin,
        start_new_session=True,
        env=docker_client_env(runs_under, run_dir_name),
    )
    try:
        proc.wait()
        return proc.returncode
    finally:
        if mine:
            keep.remove(name)


READY_TIMEOUT_S = 300      # the upgrade downloads and builds; a slow link is not a fault


def _wait_ready(name: str, timeout: float = READY_TIMEOUT_S) -> bool:
    """Block until the entrypoint says it is done, or the container dies trying.

    Polls for the marker rather than sleeping a fixed amount: the upgrade takes eight
    seconds on a warm cache and minutes on a cold one, and guessing either way is how
    the race came back.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(["docker", "exec", name, "test", "-f", "/tmp/.medulla-ready"],
                               capture_output=True, check=False)
        if probe.returncode == 0:
            return True
        if not subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"],
                              capture_output=True, text=True, check=False).stdout.strip():
            return False                   # it exited: nothing to wait for
        time.sleep(0.5)
    return False
