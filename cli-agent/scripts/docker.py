#!/usr/bin/env python3
"""docker.py — run medulla wrapper in Docker with credential forwarding.

Authentication is env/settings-based. This runner does not access
macOS Keychain.

Usage:
    docker.py -e claude-code:opus -p dev.md -n 20
    docker.py --build -e claude-code:opus -p dev.md -n 20
"""

import os
import signal
import sys
from pathlib import Path

# Home directory of the NON-ROOT user INSIDE the container. Credentials that a
# CLI reads from $HOME (broker config, opencode/ntk config, .gitconfig, the
# container overlay's home/ tree) must be mounted here — NOT at a guessed path.
# The images in play disagree on the user: the packaged default image runs as
# `medulla` (HOME=/home/medulla), the pbl/docker image (Dockerfile.workflows) as
# `hltm` (HOME=/home/hltm). A single hardcode drops every home-cred outside where
# the CLI looks for whichever image it is wrong about (e.g. cx reads
# $HOME/.config/hltm-broker/config.json → broker "not configured", gpt panelist
# fails). So this is only the FALLBACK: the real home is read from the resolved
# image at runtime (image_home) and assigned to CONTAINER_HOME before the mounts
# are built.
SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
# When installed: .medulla/scripts/docker.py → context is .medulla/
# When running from source: cli-agent/scripts/docker.py → context is cli-agent/
PROJECT_ROOT = SCRIPT_DIR.parent

# ONE resolver, shared with the engine (v2/workflow_path.py). This process answers
# "which yaml does -w mean?" BEFORE the engine starts, and its answer picks the image
# and the tmpfs isolation policy — so a second hand-written copy meant the right
# workflow could run under another one's image, or without the isolation it declared.
# Import works in both layouts: installed, docker.py runs on the venv interpreter that
# already has medulla importable; from source, PROJECT_ROOT is cli-agent/.
try:
    from medulla.v2.workflow_path import resolve_workflow_yaml  # noqa: F401
except ImportError:                                   # running from a source checkout
    sys.path.insert(0, str(PROJECT_ROOT))


# The pieces below sit in dockerlib/, beside this file. That directory is on sys.path
# when this runs as a script but NOT when it is loaded by path — which is exactly how
# the tests load it. Say so once, here, rather than in every module.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Secrets: the three .env tiers, the Claude token fallback, the transient 0600 env-file.
from dockerlib import cliargs  # noqa: E402
from dockerlib import env as dockerenv  # noqa: E402

# What the container can see — dockerlib/mounts.py.
from dockerlib import mounts as dockermounts  # noqa: E402

# Where a run may write, and where its history goes — dockerlib/paths.py.
from dockerlib import paths as dockerpaths  # noqa: E402
from dockerlib.env import (  # noqa: E402
    _add_claude_token_fallback,
    _collect_dotenv,
    _unlink_env_file,
)

# Which image, and making sure it exists — dockerlib/image.py.
from dockerlib.image import (  # noqa: E402
    DEFAULT_IMAGE,
    _config_yaml,
    ensure_image,
    image_home,
    image_tag_for,
    read_workflow_vars,
    resolve_dockerfile,
)
from dockerlib.mounts import build_volumes, workflow_uses_agy  # noqa: E402
from dockerlib.paths import (  # noqa: E402
    _remove_made_mountpoints,
    assert_runs_folder_reaches_the_container,
    definition_is_outside_workspace,
    read_shadow_paths,
    runs_under_for,
)

# Starting the container and stopping it properly — dockerlib/process.py.
from dockerlib.process import (  # noqa: E402,F401
    build_run_command,  # re-exported: the suite drives them through this module
    interactive_stdio,
    kill_container,
    run_docker,
    terminate_process_group,
)


def main():
    args = sys.argv[1:]

    # Outliving the shell that started it is the whole point of `medulla ... &`: a panel
    # runs for 10-20 minutes and nobody sits on it. SIGHUP arrives when that shell goes
    # away — and unhandled, it kills us mid-run, which looked like medulla dying on its
    # own: a panel that had already written 105KB of one answer vanished without a
    # manifest. Deliberate interruption still works; SIGINT and SIGTERM are untouched.
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass                    # no SIGHUP (Windows) or not the main thread

    # Our flags out, the engine's flags on. dockerlib/cliargs.py
    args, opts = cliargs.parse(args)
    build = opts["build"]
    cwd_ro = opts["cwd_ro"]
    runs_folder = opts["runs_folder"]
    extra_mounts = opts["extra_mounts"]
    var_files = opts["var_files"]
    cli_vars = opts["cli_vars"]
    workflow = opts["workflow"]

    # Image resolution, two distinct concepts with symmetric surfaces:
    #   IMAGE      = a ready tag, run as-is (never built here)
    #   DOCKERFILE = a recipe, built into a content-addressed tag
    # Precedence: MEDULLA_IMAGE env > --var IMAGE > vars.IMAGE >
    #             (--var DOCKERFILE > vars.DOCKERFILE > packaged default) build
    dockerfile = None
    workflow_vars = read_workflow_vars(workflow)
    image = (os.environ.get("MEDULLA_IMAGE")
             or cli_vars.get("IMAGE")
             or workflow_vars.get("IMAGE"))
    if image is None:
        if workflow:
            dockerfile = resolve_dockerfile(workflow, cli_vars)
            if not dockerfile.is_file():
                raise SystemExit(f"error: Dockerfile not found: {dockerfile}")
            image = image_tag_for(workflow, dockerfile)
        else:
            image = DEFAULT_IMAGE

    rc = ensure_image(image, build, workflow, cli_vars, dockerfile=dockerfile,
                      ready_image=dockerfile is None and workflow is not None)
    if rc != 0:
        return rc

    # The image exists now — read its real $HOME so home-based creds (broker
    # config, opencode/ntk, .gitconfig, the overlay home/ tree) mount where the
    # container's user actually looks. hltm and medulla images differ here.
    # The probe's answer belongs where the mounts are built, not here.
    dockermounts.CONTAINER_HOME = image_home(image, dockermounts.CONTAINER_HOME)

    # Before anything is mounted: a runs folder the container cannot see fails much
    # later and says nothing useful about why.
    if runs_folder is not None:
        assert_runs_folder_reaches_the_container(runs_folder, image)

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = Path(claude_config).expanduser().resolve() if claude_config else Path.home() / ".claude"

    dockerpaths.shadow_paths_for_run = read_shadow_paths(workflow)

    dotenv = _collect_dotenv(workflow)
    _add_claude_token_fallback(dotenv)
    if dotenv:
        import atexit
        import tempfile
        fd, dockerenv.env_file_for_run = tempfile.mkstemp(prefix="medulla-env-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in dotenv.items():
                f.write(f"{k}={v}\n")
        # belt for exits that never reach run_docker (bad mount → return 1)
        atexit.register(_unlink_env_file)
    volumes = build_volumes(claude_home, mount_agy=workflow_uses_agy(workflow),
                            cwd_ro=cwd_ro, runs_folder=runs_folder)

    # A SHARED definition lives outside the workspace (~/.medulla/workflows/<name>), and
    # only cwd is mounted — so the container would not find it. Mount it OUTSIDE
    # /workspace, never into it (fback-yimerxmy0y): a mount target inside a bind mount
    # is created by the daemon when missing, and for a file that means an EMPTY FILE
    # appearing in the user's repo. That debris then outranked the shared definition and
    # broke every run, and the symptom read as panelists failing to deliver — a day went
    # into chasing provider quotas. Whether the daemon creates it varies by driver
    # (colima did not here, Docker Desktop does), which is exactly why the workspace must
    # not be the target at all. runs/ still land in the project: MEDULLA_RUNS_UNDER tells
    # the engine where, since the yaml now sits on a read-only path.
    shared_runs_under = None
    if workflow:
        resolved = _config_yaml(Path(workflow))
        if definition_is_outside_workspace(resolved):
            if resolved.is_file():
                dest = runs_under_for(Path(workflow))
                name = resolved.parent.name
                mnt = f"/mnt/medulla-workflows/{name}"
                volumes.extend(["-v", f"{resolved}:{mnt}/workflow.yaml:ro"])
                for extra in ("prompts",):
                    src = resolved.parent / extra
                    if src.is_dir():
                        volumes.extend(["-v", f"{src}:{mnt}/{extra}:ro"])
                args = [f"{mnt}/workflow.yaml" if a == str(workflow) else a for a in args]
                # RELATIVE, not /workspace/...: --print-run-dir hands this path back to
                # the caller, who is on the HOST while the run happened inside the
                # container. An absolute container path does not exist for them (found
                # live: the panel printed a run dir that could not be listed).
                shared_runs_under = str(dest)

    # Mount extra folders into /workspace/<name> (nested mount inside workspace)
    #
    # A nested mount needs its mount POINT to exist, and the daemon lays /workspace down
    # first: under --cwd-ro it is read-only by the time the nested mount is applied, so
    # the daemon cannot create /workspace/<name> and the run dies before it starts. That
    # hits the one case the flag exists for — a panel launched from an empty box that
    # brings every repository in with --mount. So the point is made HERE, on the host,
    # where the directory is still writable, and removed again below.
    for src in var_files:
        volumes.extend(["-v", f"{src}:{src}:ro"])

    workspace_root = Path(os.environ.get("PWD") or os.getcwd())
    made_mountpoints: list[Path] = []
    for mount_path, ro in extra_mounts:
        p = Path(mount_path).resolve()
        if not p.is_dir():
            print(f"[docker.py] mount path not found: {p}", file=sys.stderr)
            _remove_made_mountpoints(made_mountpoints)
            return 1
        if cwd_ro and not ro:
            # A writable mount of the reviewed tree (or any part of it) hands back the
            # write access --cwd-ro just took away, through a second door.
            try:
                p.relative_to(workspace_root)
                inside = True
            except ValueError:
                inside = workspace_root == p
            if inside:
                print(f"[docker.py] --mount-rw {p} is inside the read-only workspace — "
                      f"that would undo --cwd-ro", file=sys.stderr)
                _remove_made_mountpoints(made_mountpoints)
                return 1
        if cwd_ro:
            point = workspace_root / p.name
            if not point.exists():
                # Remember EVERY level created, not just the leaf: mkdir(parents=True)
                # can make several, and rmdir on the leaf alone leaves the rest behind
                # in a tree we promised not to touch.
                missing = [q for q in [point, *point.parents]
                           if not q.exists() and workspace_root in q.parents]
                try:
                    point.mkdir(parents=True)
                except OSError as exc:
                    print(f"[docker.py] cannot make mount point {point}: {exc}",
                          file=sys.stderr)
                    _remove_made_mountpoints(made_mountpoints)
                    return 1
                made_mountpoints.extend(missing)
        suffix = ":ro" if ro else ""
        volumes.extend(["-v", f"{p}:/workspace/{p.name}{suffix}"])

    # --print-run-dir, answered NOW. The engine used to print it, which meant waiting
    # out the container bootstrap and its medulla upgrade — ~20s during which an
    # orchestrator has nothing to attach to, and every caller grew the same polling
    # loop. The name is decided here instead and handed in, so the path is known before
    # the container exists. It also fixes whose clock names the run: the container's
    # differs from the host's (a run started at 11:08 was named 09:08).
    # A RESUMED run already has a directory, and it is not ours to name: the engine
    # finds it from the journal. Printing a freshly invented path here would answer the
    # caller with somewhere that will never exist, and the engine would print the real
    # one seconds later — two lines, the wrong one first. So when resuming, step aside
    # and let the engine print, exactly as before.
    resuming = "--resume" in args or "--run" in args
    run_dir_name = None
    if not resuming and ("--print-run-dir" in args or "--print-run-json" in args):
        import datetime
        import uuid
        run_dir_name = (datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        + "-" + uuid.uuid4().hex[:8])
        base = runs_folder or (runs_under_for(Path(workflow)) / "runs" if workflow
                               else Path("runs"))
        host_run_dir = Path(base) / run_dir_name
        if "--print-run-json" in args:
            import json as _json
            args = [a for a in args if a != "--print-run-json"]
            print(_json.dumps(
                {"run_dir": str(host_run_dir), "runs_folder": str(base), "image": image,
                 "started_at": datetime.datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False), flush=True)
        if "--print-run-dir" in args:
            args = [a for a in args if a != "--print-run-dir"]
            print(host_run_dir, flush=True)

    try:
        return run_docker(image, volumes, args, runs_under=shared_runs_under,
                          run_dir_name=run_dir_name)
    finally:
        _remove_made_mountpoints(made_mountpoints)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
