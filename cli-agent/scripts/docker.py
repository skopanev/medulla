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
import subprocess
import sys
import termios
import uuid
from pathlib import Path

DEFAULT_IMAGE = "medulla:latest"
SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
# When installed: .medulla/scripts/docker.py → context is .medulla/
# When running from source: cli-agent/scripts/docker.py → context is cli-agent/
PROJECT_ROOT = SCRIPT_DIR.parent


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



def build_run_command(image, volumes, args, container_name: str) -> list[str]:
    cmd = ["docker", "run", "--init", "--rm", "--name", container_name]
    if sys.stdin.isatty():
        cmd.append("-i")
    if interactive_stdio():
        cmd.append("-t")

    for key in (
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
        "MEDULLA_BRIDGE",
    ):
        val = os.environ.get(key)
        if val:
            cmd.extend(["-e", f"{key}={val}"])

    if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        cmd.extend(["-e", f"GEMINI_API_KEY={os.environ['GOOGLE_API_KEY']}"])

    cmd.extend(volumes)
    cmd.extend(["-w", "/workspace"])
    # inside the container the sandbox IS the isolation: adapters (agy trust
    # preflight) key off this
    cmd.extend(["-e", "MEDULLA_DOCKER=1"])
    cmd.extend([image, "medulla"])
    cmd.extend(args)
    return cmd


def exec_docker_foreground(cmd: list[str], container_name: str) -> int:
    os.execvp("docker", cmd)
    return 1


def resolve_dockerfile(workflow: str | None, cli_vars: dict) -> Path:
    """Read vars.DOCKERFILE from <workflow>/pipeline.yaml, resolve relative
    to the pipeline's dir. CLI --var DOCKERFILE=... overrides.
    No default — must be declared."""
    if not workflow:
        raise SystemExit("error: -w/--workflow required to resolve Dockerfile via pipeline vars")
    workflow_dir = Path(workflow)

    cli_df = cli_vars.get("DOCKERFILE")
    if cli_df:
        p = Path(cli_df)
        return p if p.is_absolute() else (workflow_dir / p)

    pipeline_yaml = workflow_dir / "pipeline.yaml"
    if not pipeline_yaml.is_file():
        raise SystemExit(f"error: pipeline.yaml not found: {pipeline_yaml}")
    try:
        import yaml
    except ImportError:
        raise SystemExit("error: pyyaml required (pip3 install pyyaml)")
    data = yaml.safe_load(pipeline_yaml.read_text(encoding="utf-8")) or {}
    vars_map = data.get("vars") or {}
    df = vars_map.get("DOCKERFILE")
    if not df:
        raise SystemExit(
            f"error: vars.DOCKERFILE not set in {pipeline_yaml} "
            f"(and no --var DOCKERFILE=... passed)"
        )
    p = Path(df)
    return p if p.is_absolute() else (workflow_dir / p)


def ensure_image(image, build, workflow, cli_vars):
    if not build:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, check=False,
        )
        if result.returncode == 0:
            return 0
        print(f"image '{image}' not found, building...", file=sys.stderr)

    dockerfile = resolve_dockerfile(workflow, cli_vars)
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


def build_volumes(claude_home):
    home = Path.home()
    pwd_env = os.environ.get("PWD")
    cwd = Path(pwd_env) if pwd_env else Path(os.getcwd())
    vols = []

    def add(src, dst, ro=False):
        suffix = ":ro" if ro else ""
        vols.extend(["-v", f"{src}:{dst}{suffix}"])

    if claude_home.is_dir():
        add(claude_home.resolve(), "/mnt/claude", ro=True)
        # settings.json may be a symlink outside the mounted dir — resolve and
        # mount the real file so init-docker.sh can copy it into the container
        for fname in ("settings.json", "settings.local.json"):
            link = claude_home / fname
            if link.is_symlink():
                resolved = link.resolve()
                if resolved.is_file():
                    add(resolved, f"/mnt/claude/{fname}", ro=True)

    add(cwd, "/workspace")

    codex_dir = home / ".codex"
    if codex_dir.is_dir():
        add(codex_dir.resolve(), "/mnt/codex", ro=True)

    gemini_dir = home / ".gemini"
    if gemini_dir.is_dir():
        add(gemini_dir.resolve(), "/mnt/gemini", ro=True)

    opencode_dir = home / ".config" / "opencode"
    if opencode_dir.is_dir():
        add(opencode_dir.resolve(), "/home/medulla/.config/opencode", ro=True)

    ntk_dir = home / ".config" / "ntk"
    if ntk_dir.is_dir():
        add(ntk_dir.resolve(), "/home/medulla/.config/ntk", ro=True)

    opencode_auth = home / ".local" / "share" / "opencode" / "auth.json"
    if opencode_auth.exists():
        add(opencode_auth.resolve(), "/mnt/opencode-auth.json", ro=True)

    gitconfig = home / ".gitconfig"
    if gitconfig.exists():
        add(gitconfig.resolve(), "/home/medulla/.gitconfig", ro=True)

    # init-docker.sh from package → /mnt/init-docker.sh (outside /workspace, virtiofs-safe)
    _mount_init_docker(vols)

    # agy (Antigravity CLI) keys — extract from macOS Keychain and mount as temp files
    _mount_agy_keys(vols)

    # host-builder bridge for macOS native builds. Per-run bridge dir so
    # parallel runs don't share/clobber one bridge.
    bridge = Path(os.environ.get("MEDULLA_BRIDGE",
                                 Path(os.environ.get("TMPDIR", "/tmp")) / "medulla-bridge"))
    if bridge.is_dir():
        add(bridge, str(bridge))

    return vols


def _mount_init_docker(vols: list) -> None:
    src = Path(__file__).parent / "init-docker.sh"
    if src.is_file():
        vols.extend(["-v", f"{src}:/mnt/init-docker.sh:ro"])


def _mount_agy_keys(vols: list) -> None:
    import platform, subprocess, tempfile, atexit
    if platform.system() != "Darwin":
        return

    def _keychain_get(service: str, account: str) -> str:
        try:
            return subprocess.check_output(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                stderr=subprocess.DEVNULL,
            ).strip().decode()
        except Exception:
            return ""

    def _mount(value: str, dst: str) -> None:
        if not value:
            return
        tmp = tempfile.NamedTemporaryFile(prefix="agy-", delete=False, mode="w", suffix=".txt")
        tmp.write(value)
        tmp.flush()
        tmp.close()
        atexit.register(lambda p=tmp.name: __import__("os").unlink(p) if __import__("os").path.exists(p) else None)
        vols.extend(["-v", f"{tmp.name}:{dst}:ro"])

    _mount(_keychain_get("gemini", "antigravity"), "/mnt/agy-token")
    _mount(_keychain_get("Antigravity Safe Storage", "Antigravity Key"), "/mnt/agy-safe-key")


def run_docker(image, volumes, args):
    container_name = f"medulla-{uuid.uuid4().hex[:8]}"
    cmd = build_run_command(image, volumes, args, container_name)

    if interactive_stdio():
        return exec_docker_foreground(cmd, container_name)

    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
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


def main():
    image = os.environ.get("MEDULLA_IMAGE", DEFAULT_IMAGE)
    args = sys.argv[1:]

    build = "--build" in args
    if build:
        args = [a for a in args if a != "--build"]

    # Extract --mount / --mount-rw; also peek --var for Dockerfile resolution
    extra_mounts = []  # list of (path, ro:bool)
    cli_vars: dict[str, str] = {}
    clean_args = []
    i = 0
    while i < len(args):
        if args[i] == "--mount" and i + 1 < len(args):
            extra_mounts.append((args[i + 1], True))
            i += 2
        elif args[i] == "--mount-rw" and i + 1 < len(args):
            extra_mounts.append((args[i + 1], False))
            i += 2
        elif args[i] == "--var" and i + 1 < len(args):
            kv = args[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                cli_vars[k] = v
            clean_args.append(args[i])
            clean_args.append(args[i + 1])
            i += 2
        else:
            clean_args.append(args[i])
            i += 1
    args = clean_args

    # Extract workflow for Dockerfile resolution
    workflow = None
    for j, a in enumerate(args):
        if a in ("-w", "--workflow", "--pipeline") and j + 1 < len(args):
            workflow = args[j + 1]
            break

    rc = ensure_image(image, build, workflow, cli_vars)
    if rc != 0:
        return rc

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = Path(claude_config).expanduser().resolve() if claude_config else Path.home() / ".claude"

    volumes = build_volumes(claude_home)

    # Mount extra folders into /workspace/<name> (nested mount inside workspace)
    for mount_path, ro in extra_mounts:
        p = Path(mount_path).resolve()
        if not p.is_dir():
            print(f"[docker.py] mount path not found: {p}", file=sys.stderr)
            return 1
        suffix = ":ro" if ro else ""
        volumes.extend(["-v", f"{p}:/workspace/{p.name}{suffix}"])

    return run_docker(image, volumes, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
