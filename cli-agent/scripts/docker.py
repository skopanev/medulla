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
# Imports work in both layouts: installed has medulla on sys.path; in a source checkout
# PROJECT_ROOT is cli-agent/.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from medulla.v2.secret_policy import (  # noqa: E402,I001
    SecretPolicyError,
    prepare_run_secrets,
)


# The pieces below sit in dockerlib/, beside this file. That directory is on sys.path
# when this runs as a script but NOT when it is loaded by path — which is exactly how
# the tests load it. Say so once, here, rather than in every module.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Secrets: the three .env tiers and the Claude token fallback.
from dockerlib import (
    announce,  # noqa: E402
    cliargs,  # noqa: E402
    definition,  # noqa: E402
    mountfiles,  # noqa: E402
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
)
from dockerlib.image import (  # noqa: E402,F401
    DEFAULT_IMAGE,
    _config_yaml,  # re-exported: the suite drives the resolver through this module
    ensure_image,
    image_home,
    image_tag_for,
    read_workflow_vars,
    resolve_dockerfile,
)
from dockerlib.mounts import build_volumes, workflow_uses_agy  # noqa: E402,F401
from dockerlib.paths import (  # noqa: E402,F401
    _remove_made_mountpoints,
    assert_runs_folder_reaches_the_container,
    definition_is_outside_workspace,  # re-exported, with runs_under_for, for the suite
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
    mount_files = opts["mount_files"]
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
    # No fallback passed on purpose. The old call handed CONTAINER_HOME over as a
    # default, so a probe that timed out under load mounted credentials at /home/hltm
    # for an image whose home is /home/medulla — and the lane reported a missing key
    # instead of a wrong path. Let it fail loudly here, where the image is still named.
    dockermounts.CONTAINER_HOME = image_home(image)

    # Before anything is mounted: a runs folder the container cannot see fails much
    # later and says nothing useful about why.
    if runs_folder is not None:
        assert_runs_folder_reaches_the_container(runs_folder, image)

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = Path(claude_config).expanduser().resolve() if claude_config else Path.home() / ".claude"

    dockerpaths.shadow_paths_for_run = read_shadow_paths(workflow)

    try:
        secret_policy, selected_env = prepare_run_secrets(
            workflow, _collect_dotenv, _add_claude_token_fallback, cli_vars,
        )
    except SecretPolicyError as exc:
        raise SystemExit(f"error: {exc}") from exc
    dockerenv.env_values_for_run = selected_env
    volumes = build_volumes(
        claude_home,
        cwd_ro=cwd_ro,
        runs_folder=runs_folder,
        credential_bundles=secret_policy["bundles"],
        credential_env=selected_env,
    )

    # A shared definition lives outside the mounted workspace and has to be brought
    # in — dockerlib/definition.py holds the two reasons it is a DIRECTORY mount
    # outside /workspace, both of which cost a day before they were understood.
    args, shared_runs_under = definition.mount_shared_definition(volumes, args, workflow)

    # Single host files, read-only: --var-file sources at their own path, and whatever
    # --mount-file names. dockerlib/mountfiles.py carries the inode warning that
    # applies to all of them.
    mountfiles.mount_var_files(volumes, var_files)
    mountfiles.mount_secret_files(volumes, mount_files)

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
                          run_dir_name=run_dir_name, workflow=workflow,
                          keep_session=workflow_names_a_session(workflow))
    finally:
        _remove_made_mountpoints(made_mountpoints)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
