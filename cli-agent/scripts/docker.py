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
from dockerlib import (
    announce,  # noqa: E402
    cliargs,  # noqa: E402
    mountpoints,  # noqa: E402
)
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

# Which image, and making sure it exists — dockerlib/image.py.
from dockerlib.probe import workflow_names_a_session  # noqa: E402

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
                # EVERY directory the workflow ships, not a hardcoded list. It was
                # ("prompts",) — so scripts/ never reached the container, and the
                # synthesize node's `$MEDULLA_WORKFLOW_DIR/scripts/spar-run.sh` did not
                # exist there. The panel then wrote no verdict.md at all while the node
                # still reported success. A workflow that ships a directory ships it
                # because a node needs it; runs/ is the one exception, being history
                # rather than definition.
                for src in sorted(d for d in resolved.parent.iterdir() if d.is_dir()):
                    if src.name in ("runs", "__pycache__"):
                        continue
                    volumes.extend(["-v", f"{src}:{mnt}/{src.name}:ro"])
                args = [f"{mnt}/workflow.yaml" if a == str(workflow) else a for a in args]
                # RELATIVE, not /workspace/...: --print-run-dir hands this path back to
                # the caller, who is on the HOST while the run happened inside the
                # container. An absolute container path does not exist for them (found
                # live: the panel printed a run dir that could not be listed).
                shared_runs_under = str(dest)

    # --var-file sources: mounted at their own absolute path, so the argument the engine
    # receives is valid on both sides of the boundary.
    for src in var_files:
        volumes.extend(["-v", f"{src}:{src}:ro"])

    made_mountpoints, rc = mountpoints.prepare(volumes, extra_mounts, cwd_ro)
    if rc:
        return rc


    args, run_dir_name = announce.announce(args, workflow, runs_folder, image)

    try:
        # A workflow that NAMES a session keeps its container: the conversation lives
        # in the CLI's own state inside $HOME, so a fresh container would be a fresh
        # conversation whatever id we resumed with. Blunt on purpose — no check for
        # whether the session is actually used, because that check is one more thing
        # to get wrong, and an unused name costs one idle container per pipeline.
        return run_docker(image, volumes, args, runs_under=shared_runs_under,
                          run_dir_name=run_dir_name,
                          keep_session=workflow_names_a_session(workflow))
    finally:
        _remove_made_mountpoints(made_mountpoints)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
