"""Which image a workflow runs in, and making sure it exists.

Split out of docker.py under the project's 250-line rule ($MAX_LOC). Everything here
answers one question — WHICH image — from the workflow's own declaration: the
Dockerfile it names, the content-addressed tag that follows, the real $HOME inside, and
building or pulling when the tag is absent.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent
try:
    from medulla.v2.workflow_path import resolve_workflow_yaml, workflow_dir_for
except ImportError:                                   # running from a source checkout
    sys.path.insert(0, str(PROJECT_ROOT))
    from medulla.v2.workflow_path import resolve_workflow_yaml, workflow_dir_for

DEFAULT_IMAGE = "medulla:latest"       # unchanged from before the split


def _config_yaml(d: Path) -> Path:
    """Which yaml `-w` means. Delegates to the engine's resolver so both processes
    cannot drift; kept as a name because the rest of this file reads better with it."""
    return resolve_workflow_yaml(d)


def read_workflow_vars(workflow: str | None) -> dict:
    if not workflow:
        return {}
    workflow_yaml = _config_yaml(Path(workflow))
    if not workflow_yaml.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        raise SystemExit("error: pyyaml required (pip3 install pyyaml)")
    data = yaml.safe_load(workflow_yaml.read_text(encoding="utf-8")) or {}
    return data.get("vars") or {}


def resolve_dockerfile(workflow: str | None, cli_vars: dict) -> Path:
    """Read vars.DOCKERFILE from <workflow>/workflow.yaml, resolve relative
    to the workflow's dir. CLI --var DOCKERFILE=... overrides. Absent ->
    the packaged default (one shared image for workflows that don't care)."""
    if not workflow:
        raise SystemExit("error: -w/--workflow required to resolve Dockerfile via workflow vars")
    # -w may name the yaml itself (see _config_yaml). A relative DOCKERFILE is relative to
    # the workflow's DIRECTORY either way — without this, `-w brain/resolve.yaml` resolved
    # to "brain/resolve.yaml/Dockerfile" and the build died on a path that cannot exist.
    _w = Path(workflow)
    workflow_dir = workflow_dir_for(_w)

    cli_df = cli_vars.get("DOCKERFILE")
    if cli_df:
        p = Path(cli_df)
        return p if p.is_absolute() else (workflow_dir / p)

    workflow_yaml = _config_yaml(_w)          # _w, not workflow_dir: in file mode the
    if not workflow_yaml.is_file():           # yaml IS the path; the dir has no workflow.yaml
        raise SystemExit(f"error: workflow.yaml not found: {workflow_yaml}")
    try:
        import yaml
    except ImportError:
        raise SystemExit("error: pyyaml required (pip3 install pyyaml)")
    data = yaml.safe_load(workflow_yaml.read_text(encoding="utf-8")) or {}
    vars_map = data.get("vars") or {}
    df = vars_map.get("DOCKERFILE")
    if not df:
        # no vars.DOCKERFILE = the packaged default image (all four harnesses):
        # workflows that don't care share one build; declare the var to diverge
        return SCRIPT_DIR / "Dockerfile.default"
    p = Path(df)
    return p if p.is_absolute() else (workflow_dir / p)


def image_tag_for(workflow: str, dockerfile: Path) -> str:
    """Per-workflow, content-addressed image tag: medulla-<name>:<sha of Dockerfile>.

    Workflows with different Dockerfiles must never share a tag (the first
    builder would silently win), and editing a Dockerfile must trigger a
    rebuild without a manual --build (a new hash is an absent image)."""
    import hashlib
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    # A yaml path drops its extension (`-w brain/resolve.yaml` must not tag the image
    # "medulla-resolve.yaml:<sha>"); a DIRECTORY keeps its full name, or a dir called
    # "my.workflows" would silently tag as "my".
    resolved = resolve_workflow_yaml(Path(workflow))
    # From the RESOLVED definition, not the raw -w: `-w .medulla/workflows/spar/workflow.yaml`
    # tagged the image "medulla-workflow.yaml". A generic file name means the workflow is
    # its DIRECTORY; any other name is a workflow living as one file among several.
    wf_name = resolved.parent.name if resolved.stem in ("workflow", "pipeline") else resolved.stem
    name = "default" if dockerfile == SCRIPT_DIR / "Dockerfile.default" else wf_name
    return f"medulla-{name}:{digest}"


def image_home(image, fallback):
    """The non-root user's $HOME inside the resolved image, read from the image
    itself. The two images in play run as different users (hltm vs medulla), so a
    hardcode is wrong for one of them and silently drops every $HOME-based cred —
    most visibly the broker config cx needs, failing the gpt panelist. Docker sets
    HOME from the image's USER at run time, so a one-shot probe is authoritative;
    fall back to the default only if the probe cannot answer."""
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", image,
             "-c", 'printf %s "$HOME"'],
            capture_output=True, text=True, timeout=30)
        home = out.stdout.strip()
        if out.returncode == 0 and home.startswith("/") and home != "/":
            return home
    except Exception:
        pass
    return fallback


def ensure_image(image, build, workflow, cli_vars, dockerfile=None, ready_image=False):
    if not build:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, check=False,
        )
        if result.returncode == 0:
            return 0
        if ready_image:
            # IMAGE is a ready tag: never build it from an unrelated Dockerfile
            print(f"image '{image}' not found locally, pulling...", file=sys.stderr)
            return subprocess.run(["docker", "pull", image], check=False).returncode
        print(f"image '{image}' not found, building...", file=sys.stderr)

    dockerfile = dockerfile or resolve_dockerfile(workflow, cli_vars)
    if not dockerfile.is_file():
        raise SystemExit(f"error: Dockerfile not found: {dockerfile}")
    context = Path.cwd()
    print(f"building image '{image}' from {context} (Dockerfile: {dockerfile})...", file=sys.stderr)
    cmd = ["docker", "build",
           "--build-arg", f"USER_UID={os.getuid()}",
           "-f", str(dockerfile),
           "-t", image]
    if build:
        cmd.append("--no-cache")
    cmd.append(str(context))
    proc = subprocess.Popen(cmd, start_new_session=True)
    interrupted = {"count": 0}

    # Imported HERE, not at module scope: process imports paths, and paths imports this
    # module — a cycle at import time. The build only needs them when interrupted.
    from dockerlib.process import terminate_process_group

    def build_sigint(signum, frame):
        interrupted["count"] += 1
        try:
            os.write(
                2,
                (
                    f"\n[medulla] SIGINT docker-build count={interrupted['count']} pid={proc.pid} pgid={os.getpgid(proc.pid)}\n"
                ).encode("utf-8", errors="replace"),
            )
        except Exception:
            pass
        if interrupted["count"] == 1:
            print("\n  ⏹ build interrupted (stopping)", file=sys.stderr, flush=True)
            terminate_process_group(proc, force=False)
            return
        print("\n  ⏹ build force stop", file=sys.stderr, flush=True)
        terminate_process_group(proc, force=True)
        subprocess.run(["docker", "buildx", "stop"], capture_output=True, check=False)
        sys.exit(130)

    prev = signal.signal(signal.SIGINT, build_sigint)
    proc.wait()
    signal.signal(signal.SIGINT, prev)
    if interrupted["count"] > 0:
        subprocess.run(["docker", "buildx", "stop"], capture_output=True, check=False)
    if proc.returncode != 0:
        print("docker build failed", file=sys.stderr)
    return proc.returncode


