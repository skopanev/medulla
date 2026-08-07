"""Apple Container preflight and top-level run orchestration."""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from medulla.scripts import docker as shared
from medulla.scripts.apple_build import ensure_image, image_home

DEFAULT_IMAGE = "medulla:latest"


def container_service_running() -> bool:
    status = subprocess.run(
        ["container", "system", "status", "--format", "json"],
        capture_output=True, check=False, text=True)
    if status.returncode == 0:
        try:
            return json.loads(status.stdout)["status"] == "running"
        except (KeyError, TypeError, json.JSONDecodeError):
            return plain_status_is_running(status.stdout)
    status = subprocess.run(
        ["container", "system", "status"], capture_output=True,
        check=False, text=True)
    return status.returncode == 0 and plain_status_is_running(status.stdout)


def plain_status_is_running(output: str) -> bool:
    for line in output.lower().splitlines():
        fields = line.replace(":", " ").split()
        if fields and fields[0] == "status":
            return len(fields) > 1 and fields[1] == "running"
    lowered = output.lower()
    return "is running" in lowered and "not running" not in lowered


def assert_runs_folder_reaches_container(runs_folder: Path, image: str) -> None:
    """Fail before a run when Apple's VM cannot see the requested host folder."""
    marker = runs_folder / f".medulla-mount-probe-{uuid.uuid4().hex}"
    marker.write_text("ok", encoding="utf-8")
    result = None
    try:
        try:
            result = subprocess.run(
                ["container", "run", "--rm", "-v", f"{runs_folder}:{runs_folder}:rw",
                 "--entrypoint", "sh", image, "-c",
                 f"test -f {shlex.quote(str(marker))} && "
                 f"test -w {shlex.quote(str(runs_folder))}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            result = None
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    if result is None or result.returncode != 0:
        detail = result.stderr.strip() if result is not None else "probe failed"
        raise SystemExit(
            f"error: --runs-folder is not visible to Apple Container: {runs_folder}"
            + (f" ({detail})" if detail else ""))


def main(run_apple: Callable[..., int]) -> int:
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass
    if sys.platform != "darwin":
        print("error: Apple container runtime requires macOS", file=sys.stderr)
        return 1
    if platform.machine() != "arm64":
        print("error: Apple container runtime requires Apple silicon", file=sys.stderr)
        return 1
    mac_version = platform.mac_ver()[0]
    try:
        mac_major = int(mac_version.split(".", 1)[0])
    except (AttributeError, ValueError):
        mac_major = 0
    if mac_major < 26:
        print("error: Apple container runtime requires macOS 26 or newer", file=sys.stderr)
        return 1
    if shutil.which("container") is None:
        print("error: Apple `container` CLI not found; install it from github.com/apple/container",
              file=sys.stderr)
        return 1
    if not container_service_running():
        print("error: Apple container service is not running; run `container system start`",
              file=sys.stderr)
        return 1

    parsed = shared.cliargs.parse(sys.argv[1:])
    if parsed == 1:
        return 1
    args, opts = parsed
    build, cwd_ro = opts["build"], opts["cwd_ro"]
    runs_folder, extra_mounts = opts["runs_folder"], opts["extra_mounts"]
    var_files, cli_vars, workflow = opts["var_files"], opts["cli_vars"], opts["workflow"]

    dockerfile = None
    workflow_vars = shared.read_workflow_vars(workflow)
    image = (os.environ.get("MEDULLA_IMAGE") or cli_vars.get("IMAGE")
             or workflow_vars.get("IMAGE"))
    if image is None:
        if workflow:
            dockerfile = shared.resolve_dockerfile(workflow, cli_vars)
            if not dockerfile.is_file():
                raise SystemExit(f"error: Dockerfile not found: {dockerfile}")
            image = shared.image_tag_for(workflow, dockerfile)
        else:
            image = DEFAULT_IMAGE
    rc = ensure_image(image, build, workflow, cli_vars, dockerfile=dockerfile,
                      ready_image=dockerfile is None and workflow is not None)
    if rc != 0:
        return rc

    shared.dockermounts.CONTAINER_HOME = image_home(image)
    if runs_folder is not None:
        assert_runs_folder_reaches_container(runs_folder, image)
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = (Path(claude_config).expanduser().resolve() if claude_config
                   else Path.home() / ".claude")
    shared.dockerpaths.shadow_paths_for_run = shared.read_shadow_paths(workflow)
    dotenv = shared._collect_dotenv(workflow)
    shared._add_claude_token_fallback(dotenv)
    if dotenv:
        import atexit
        import tempfile
        fd, shared.dockerenv.env_file_for_run = tempfile.mkstemp(prefix="medulla-env-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            for key, value in dotenv.items():
                handle.write(f"{key}={value}\n")
        atexit.register(shared._unlink_env_file)

    volumes = shared.build_volumes(
        claude_home, mount_agy=shared.workflow_uses_agy(workflow),
        cwd_ro=cwd_ro, runs_folder=runs_folder)
    shared_runs_under = None
    if workflow:
        resolved = shared._config_yaml(Path(workflow))
        if shared.definition_is_outside_workspace(resolved) and resolved.is_file():
            dest, name = shared.runs_under_for(Path(workflow)), resolved.parent.name
            mount_root = f"/mnt/medulla-workflows/{name}"
            volumes.extend(["-v", f"{resolved}:{mount_root}/workflow.yaml:ro"])
            for source in sorted(path for path in resolved.parent.iterdir() if path.is_dir()):
                if source.name not in ("runs", "__pycache__"):
                    volumes.extend(["-v", f"{source}:{mount_root}/{source.name}:ro"])
            args = [f"{mount_root}/workflow.yaml" if arg == str(workflow) else arg
                    for arg in args]
            shared_runs_under = str(dest)
    for source in var_files:
        volumes.extend(["-v", f"{source}:{source}:ro"])
    made_mountpoints, rc = shared.mountpoints.prepare(volumes, extra_mounts, cwd_ro)
    if rc:
        return rc
    args, run_dir_name = shared.announce.announce(args, workflow, runs_folder, image)
    try:
        return run_apple(image, volumes, args, runs_under=shared_runs_under,
                         run_dir_name=run_dir_name)
    finally:
        shared._remove_made_mountpoints(made_mountpoints)
