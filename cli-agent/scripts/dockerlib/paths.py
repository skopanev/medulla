"""Where a run may write, and where its history goes.

Split out of docker.py under the project's 250-line rule ($MAX_LOC). Each function here
answers a question about PLACE that has already been decided wrongly at least once, and
the comments record which way: a shared workflow's history landing at the repo root, a
runs folder the VM cannot see, a mount point left behind in a tree we promised not to
touch, a relative spelling classified as "outside the workspace".
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent
try:
    from medulla.v2.workflow_path import shared_name_for
except ImportError:                                   # running from a source checkout
    sys.path.insert(0, str(PROJECT_ROOT))
    from medulla.v2.workflow_path import shared_name_for

from dockerlib.image import _config_yaml


def read_shadow_paths(workflow: str | None) -> list[str]:
    """workflow.yaml `docker: {shadow: [...]}` — workspace-relative paths the
    container must see EMPTY (tmpfs mounted over them; host untouched). The
    block's law: a workflow may only SHRINK its container's exposure, never
    enlarge it. Standalone fail-fast: this runs before the engine validator."""
    if not workflow:
        return []
    yaml_path = _config_yaml(Path(workflow))
    if not yaml_path.is_file():
        return []
    try:
        import yaml
    except ImportError:
        raise SystemExit("error: pyyaml required (pip3 install pyyaml)")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    block = data.get("docker")
    if block is None:
        return []
    if not isinstance(block, dict) or not isinstance(block.get("shadow", []), list):
        raise SystemExit("error: docker: must be a mapping with a 'shadow' list")
    shadow = block.get("shadow") or []
    for p in shadow:
        parts = [s for s in str(p).split("/") if s not in ("", ".")]
        if not isinstance(p, str) or not parts or p.startswith("/") or ".." in parts:
            raise SystemExit(f"error: docker.shadow path escapes the workspace: {p!r}")
    return ["/".join([s for s in p.split("/") if s not in ("", ".")]) for p in shadow]


shadow_paths_for_run: list[str] = []


def runs_under_for(workflow: Path) -> Path:
    """Where this run's history goes, RELATIVE to the launch dir.

    Must match what the bare engine would pick (rundir.runs_root_for): a shared
    workflow's history is rooted at .medulla/workflows/<name> in the launching
    project. Handing the raw -w over instead made `-w spar --docker` write into
    spar/runs/ at the repo ROOT — same command, different place, and litter outside
    .medulla/. Relative on purpose: --print-run-dir hands this path to a caller who
    is on the host while the run happened inside the container.
    """
    name = shared_name_for(workflow)
    if name:
        return Path(".medulla") / "workflows" / name
    return workflow.parent if workflow.is_file() else workflow


def _remove_made_mountpoints(points: list[Path]) -> None:
    """Take back exactly what we made, deepest first.

    rmdir, never rmtree: it removes only what is still EMPTY, so a directory that turned
    out to hold something is left exactly where it is. The tree ends the run as it
    started it, which is the whole promise of --cwd-ro.
    """
    for point in sorted(points, key=lambda q: len(q.parts), reverse=True):
        try:
            point.rmdir()
        except OSError:
            pass


def definition_is_outside_workspace(resolved_yaml: Path) -> bool:
    """Does the definition need mounting in, or is it already under /workspace?

    ABSOLUTE on both sides: the resolver hands paths back as they were given, so a
    relative spelling of a repo-local definition used to compare false here and take
    the shared branch — building a bind mount whose SOURCE was a relative path, which
    Docker reads as a NAMED VOLUME and hands the container an empty directory instead
    of the workflow.
    """
    try:
        resolved_yaml.resolve().relative_to(Path.cwd().resolve())
        return False
    except ValueError:
        return True


# Which image, and making sure it exists — dockerlib/image.py.


def assert_runs_folder_reaches_the_container(runs_folder: Path, image: str) -> None:
    """Can the container actually WRITE here? Ask it, do not guess.

    A bind mount whose source the VM cannot see is not an error: the daemon creates an
    empty directory owned by root and mounts THAT, so the run dies later, deep inside
    mkdir, with a bare PermissionError naming neither the flag nor the reason. Under
    colima only the home directory is shared with the VM, Docker Desktop shares a
    different set, and a remote daemon shares none of it — so the honest test is a
    marker written here and looked for in there. Costs one container start, and only
    when --runs-folder was passed.
    """
    # Unique per probe: a fixed name would overwrite a file of that name that was
    # already there — and delete it afterwards — and two runs probing at once would
    # each remove the other's marker.
    marker = runs_folder / f".medulla-reachable-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        marker.write_text("probe\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: --runs-folder {runs_folder} is not writable here: {exc}")
    try:
        probe = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{runs_folder}:{runs_folder}",
             "--entrypoint", "sh", image, "-c",
             f"test -f '{marker}' && test -w '{runs_folder}'"],
            capture_output=True)
    finally:
        marker.unlink(missing_ok=True)
    if probe.returncode != 0:
        raise SystemExit(
            f"error: --runs-folder {runs_folder} is invisible to the container.\n"
            f"    The daemon mounted an empty directory over it, so the run would fail\n"
            f"    later with a bare 'Permission denied' from mkdir.\n"
            f"    Your VM only shares part of the filesystem: with colima that is your\n"
            f"    home directory. Put the folder under {Path.home()} — or add the path\n"
            f"    to the VM's mounts and restart it.")


# What the container can see — dockerlib/mounts.py.


def workspace_cwd() -> Path:
    """The directory to mount as /workspace.

    os.getcwd() is the TRUTH; $PWD is only a nicer spelling of it. Reading PWD first
    was wrong in a way that took an hour to find: subprocess.run(cwd=X) does not touch
    PWD, so a programmatic call inherited the CALLER's PWD — non-empty, so `or
    os.getcwd()` never fired — and mounted the caller's directory. The symptom was
    `E_VALIDATION: workflow not found`, with nothing about mounts in it.

    PWD is still preferred when it names the SAME directory, because it preserves the
    symlink form a user typed (/Users/... rather than /private/var/...), and that path
    has to be valid on the host side of a -v argument.
    """
    real = Path(os.getcwd())
    pwd = os.environ.get("PWD")
    if pwd:
        try:
            if os.path.samefile(pwd, real):
                return Path(pwd)
        except OSError:
            pass
    return real
