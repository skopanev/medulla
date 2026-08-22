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
import uuid
from pathlib import Path

DEFAULT_IMAGE = "medulla:latest"
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
CONTAINER_HOME = "/home/hltm"
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
    from medulla.v2.workflow_path import resolve_workflow_yaml, shared_name_for, workflow_dir_for
except ImportError:                                   # running from a source checkout
    sys.path.insert(0, str(PROJECT_ROOT))
    from medulla.v2.workflow_path import resolve_workflow_yaml, shared_name_for, workflow_dir_for


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
    "MEDULLA_BRIDGE",
)
CLAUDE_TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"


def _parse_env_file(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and not k.startswith("MEDULLA_"):
            out[k] = v.strip().strip('"').strip("'")
    return out


def _collect_dotenv(workflow: str | None) -> dict:
    """All three .env tiers forward WHOLE (owner decision: the user's zone,
    the engine is not a nanny). Merge order explicit and fixed:
    global < project < workflow — THE NEAREST TIER WINS on key conflict."""
    merged = _parse_env_file(Path.home() / ".medulla" / ".env")
    if workflow:
        # The PROJECT tier belongs to the repo you launch from — which for a shared
        # definition is not an ancestor of the yaml at all (it lives under $HOME).
        launch = Path.cwd().resolve()
        wdir = workflow_dir_for(Path(workflow)).resolve()
        seen = set()
        for base in list(reversed(launch.parents)) + [launch] + list(reversed(wdir.parents)):
            candidate = base / ".medulla" / ".env"
            if candidate in seen or candidate == Path.home() / ".medulla" / ".env":
                continue                                  # already the global tier
            seen.add(candidate)
            merged.update(_parse_env_file(candidate))
        # The WORKFLOW tier belongs to the definition, wherever it resolved to. Taking
        # it from the raw -w argument meant the flagship shared layout (no local file
        # at all) read this documented tier from a path that does not exist, and the
        # real ~/.medulla/workflows/<name>/.env was silently never forwarded.
        merged.update(_parse_env_file(wdir / ".env"))
    return merged


def _add_claude_token_fallback(env: dict) -> None:
    """Use the standard Claude profile token when no explicit token exists."""
    if os.environ.get(CLAUDE_TOKEN_KEY) or env.get(CLAUDE_TOKEN_KEY):
        return
    token_path = Path.home() / ".claude" / "token-home"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SystemExit(f"error: cannot read Claude OAuth token: {token_path}: {exc}") from exc
    if not token:
        return
    if "\n" in token or "\r" in token:
        raise SystemExit(f"error: Claude OAuth token must be one line: {token_path}")
    env[CLAUDE_TOKEN_KEY] = token


env_file_for_run = None


def _unlink_env_file() -> None:
    """The env-file holds merged provider tokens (0600 in $TMPDIR). Docker's
    client reads --env-file at startup, so the file is only needed until the
    run ends — remove it on EVERY exit path (finally + atexit belt): a timer
    thread dies with the process and leaks secrets on Ctrl-C / early return."""
    global env_file_for_run
    if env_file_for_run:
        try:
            os.unlink(env_file_for_run)
        except OSError:
            pass
        env_file_for_run = None


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


def build_run_command(image, volumes, args, container_name: str,
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
    if env_file_for_run:
        cmd.extend(["--env-file", env_file_for_run])

    cmd.extend(volumes)
    # shadow: an empty tmpfs mounted OVER a workspace subpath — the more
    # specific mount wins, the container sees the dir empty, the host keeps
    # the real content. Host existence is irrelevant: tmpfs never touches it.
    for p in shadow_paths_for_run:
        cmd.extend(["--tmpfs", f"/workspace/{p}"])
    cmd.extend(["-w", "/workspace"])
    # inside the container the sandbox IS the isolation: adapters (agy trust
    # preflight) key off this
    cmd.extend(["-e", "MEDULLA_DOCKER=1"])
    if runs_under:
        cmd.extend(["-e", f"MEDULLA_RUNS_UNDER={runs_under}"])
    cmd.extend([image, "medulla"])
    cmd.extend(args)
    return cmd


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
    marker = runs_folder / ".medulla-reachable"
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


def workflow_uses_agy(workflow: str | None) -> bool:
    """Does this workflow actually use the agy harness?

    Only then are the Keychain-extracted agy keys mounted: a Keychain prompt on every
    --docker run for workflows that never touch agy is noise, and scary noise.

    Reads the HARNESS FIELDS, not the file text. The previous check was
    `"agy" in yaml_path.read_text()` — a substring match, so the word appearing in a
    prompt, a comment or a model name sent the runner into the macOS Keychain for
    credentials the run never needed. Anything unreadable or unparseable keeps the old
    permissive answer: missing credentials fail confusingly, a spurious prompt is merely
    annoying.
    """
    if not workflow:
        return True
    yaml_path = _config_yaml(Path(workflow))
    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return True

    def mentions_agy(node) -> bool:
        if isinstance(node, dict):
            agent = node.get("agent")
            if isinstance(agent, str) and agent.strip() == "agy":
                return True
            if isinstance(agent, dict) and str(agent.get("harness", "")).strip() == "agy":
                return True
            # pool inputs carry the harness as data: {slug: gemini, harness: agy, ...}
            if str(node.get("harness", "")).strip() == "agy":
                return True
            return any(mentions_agy(v) for v in node.values())
        if isinstance(node, list):
            return any(mentions_agy(v) for v in node)
        return False

    return mentions_agy(data)


def build_volumes(claude_home, mount_agy=True, *,
                  cwd_ro: bool = False, runs_folder: Path | None = None):
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

    # The reviewed tree. Read-only when asked: a panel must be able to read a repo
    # without leaving anything in it — two rounds left a dangling symlink and a staged
    # file behind, and at review time neither is distinguishable from real work. It can
    # only be read-only because the run's history goes elsewhere (--runs-folder).
    add(cwd, "/workspace", ro=cwd_ro)
    if runs_folder is not None:
        # At its OWN host path, so the run dir printed inside is openable outside.
        add(runs_folder, str(runs_folder), ro=False)

    # A git WORKTREE keeps its metadata in ANOTHER repository: .git here is a file
    # reading `gitdir: <main>/.git/worktrees/<name>`, a host path the container does
    # not have. Every git command then fails inside — `git rev-parse` first — and the
    # agents spend their turn working around it instead of reviewing. Mount the common
    # .git at its own host path so the pointer resolves; read-only exactly when the
    # workspace is, since git writes its index and reflog in there.
    git_pointer = cwd / ".git"
    if git_pointer.is_file():
        try:
            line = git_pointer.read_text(encoding="utf-8").strip()
        except OSError:
            line = ""
        if line.startswith("gitdir:"):
            gitdir = Path(line.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (cwd / gitdir).resolve()
            # the worktree's own dir holds no objects or refs — the COMMON .git does
            common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
            if common.is_dir():
                add(common, str(common), ro=cwd_ro)

    # Shared workflow definitions: a symlink under .medulla/workflows/ points OUT of the
    # workspace (e.g. ~/.medulla/workflows/spar/workflow.yaml, one copy per machine), and
    # only cwd is mounted — so inside the container the link dangles and the workflow is
    # "not found". Mount each target read-only at the SAME path it occupies in /workspace.
    # Only the linked file/dir travels: the directory around it stays repo-local, so runs/
    # and artifacts are still written per-worktree. A real file always beats a link, so a
    # project that wants its own version just replaces it — local overrides shared.
    for link in sorted((cwd / ".medulla" / "workflows").rglob("*")):
        if not link.is_symlink():
            continue
        try:
            target = link.resolve(strict=True)
            rel = link.relative_to(cwd)
        except (OSError, ValueError):
            continue                       # broken link or outside cwd: leave it be
        if cwd in target.parents or target == cwd:
            continue                       # points back inside the workspace: already there
        add(target, f"/workspace/{rel}", ro=True)

    codex_dir = home / ".codex"
    if codex_dir.is_dir():
        add(codex_dir.resolve(), "/mnt/codex", ro=True)

    gemini_dir = home / ".gemini"
    if gemini_dir.is_dir():
        add(gemini_dir.resolve(), "/mnt/gemini", ro=True)

    opencode_dir = home / ".config" / "opencode"
    if opencode_dir.is_dir():
        add(opencode_dir.resolve(), f"{CONTAINER_HOME}/.config/opencode", ro=True)

    ntk_dir = home / ".config" / "ntk"
    if ntk_dir.is_dir():
        add(ntk_dir.resolve(), f"{CONTAINER_HOME}/.config/ntk", ro=True)

    # Container overlay — the escape hatch for anything the IMAGE cannot carry:
    # private tooling, a site-specific wrapper, credentials medulla knows nothing
    # about. Whatever sits in ~/.medulla/container/ is mounted read-only at the
    # matching place inside, and medulla neither reads it nor cares what it is.
    #   ~/.medulla/container/bin/<name>   -> /usr/local/bin/<name>     (on PATH)
    #   ~/.medulla/container/home/<path>  -> {CONTAINER_HOME}/<path>   (container $HOME)
    # Real directories are walked and mounted ONE FILE AT A TIME, never as a directory
    # over /usr/local/bin: covering that would hide the CLIs the image installs. A
    # SYMLINK to a directory is the exception — it travels whole, to its own nested
    # path. Without that a wrapper could be placed on PATH but the package it imports
    # could not follow it in, and the tool died inside the container on an import it
    # resolved fine on the host.
    overlay = home / ".medulla" / "container"
    for sub, dest_root in (("bin", "/usr/local/bin"), ("home", CONTAINER_HOME)):
        root = overlay / sub
        if not root.is_dir():
            continue
        for entry in sorted(root.rglob("*")):
            if entry.is_dir() and not entry.is_symlink():
                continue                  # walked into; its files are mounted below
            try:
                rel = entry.relative_to(root)
                target = entry.resolve(strict=True)
            except (OSError, ValueError):
                continue                  # broken link: skip, never fail the run
            add(target, f"{dest_root}/{rel}", ro=True)

    opencode_auth = home / ".local" / "share" / "opencode" / "auth.json"
    if opencode_auth.exists():
        add(opencode_auth.resolve(), "/mnt/opencode-auth.json", ro=True)

    gitconfig = home / ".gitconfig"
    if gitconfig.exists():
        add(gitconfig.resolve(), f"{CONTAINER_HOME}/.gitconfig", ro=True)

    # init-docker.sh from package → /mnt/init-docker.sh (outside /workspace, virtiofs-safe)
    _mount_init_docker(vols)

    # agy (Antigravity CLI) keys — extract from macOS Keychain and mount as temp
    # files, but ONLY for workflows that use agy (Keychain prompts are not free)
    if mount_agy:
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
    import atexit
    import platform
    import subprocess
    import tempfile
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


def run_docker(image, volumes, args, runs_under: str | None = None):
    container_name = f"medulla-{uuid.uuid4().hex[:8]}"
    cmd = build_run_command(image, volumes, args, container_name,
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

    build = "--build" in args
    if build:
        args = [a for a in args if a != "--build"]

    # --cwd-ro is OURS: the engine has no container to mount anything into, so it is
    # consumed here. --runs-folder is NOT — it travels on, because the engine is what
    # actually roots the history there.
    cwd_ro = "--cwd-ro" in args
    if cwd_ro:
        args = [a for a in args if a != "--cwd-ro"]
    runs_folder = None
    for j, a in enumerate(args):
        if a == "--runs-folder" and j + 1 < len(args):
            runs_folder = Path(args[j + 1]).expanduser().resolve()
            # Rewrite it to the ABSOLUTE path, deliberately, and mount the folder at
            # that same path inside (below). --print-run-dir hands its answer to a
            # caller standing on the HOST while the run happened in the container, so
            # a container-only path would be unusable there — this file already carries
            # scars from exactly that. One absolute string, valid in both namespaces.
            args[j + 1] = str(runs_folder)
            break
    if cwd_ro and runs_folder is None:
        print("error: --cwd-ro requires --runs-folder (a read-only workspace leaves the "
              "run nowhere to write)", file=sys.stderr)
        return 1
    if runs_folder is not None:
        runs_folder.mkdir(parents=True, exist_ok=True)

    # Extract --mount / --mount-rw; also peek --var for Dockerfile resolution
    extra_mounts = []  # list of (path, ro:bool)
    var_files: list[Path] = []          # --var-file sources, mounted at their own paths
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
        elif args[i] == "--var-file" and i + 1 < len(args):
            # The file has to exist INSIDE too, and its host path is what the engine
            # will open. Mount it at that same absolute path (the trick --runs-folder
            # already uses) and hand the absolute form on, so a relative one — or a
            # file outside the workspace — still resolves in there.
            key, _, raw = args[i + 1].partition("=")
            src = Path(raw).expanduser()
            if raw and not src.is_file():
                print(f"[docker.py] --var-file {key}: no such file: {src}", file=sys.stderr)
                return 1
            src = src.resolve()
            var_files.append(src)
            clean_args.append(args[i])
            clean_args.append(f"{key}={src}")
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
        if a in ("-w", "--workflow", "--workflow") and j + 1 < len(args):
            workflow = args[j + 1]
            break

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
    global CONTAINER_HOME
    CONTAINER_HOME = image_home(image, CONTAINER_HOME)

    # Before anything is mounted: a runs folder the container cannot see fails much
    # later and says nothing useful about why.
    if runs_folder is not None:
        assert_runs_folder_reaches_the_container(runs_folder, image)

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_home = Path(claude_config).expanduser().resolve() if claude_config else Path.home() / ".claude"

    global shadow_paths_for_run
    shadow_paths_for_run = read_shadow_paths(workflow)

    global env_file_for_run
    dotenv = _collect_dotenv(workflow)
    _add_claude_token_fallback(dotenv)
    if dotenv:
        import atexit
        import tempfile
        fd, env_file_for_run = tempfile.mkstemp(prefix="medulla-env-")
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
            return 1
        if cwd_ro:
            point = workspace_root / p.name
            if not point.exists():
                try:
                    point.mkdir(parents=True)
                except OSError as exc:
                    print(f"[docker.py] cannot make mount point {point}: {exc}",
                          file=sys.stderr)
                    return 1
                made_mountpoints.append(point)
        suffix = ":ro" if ro else ""
        volumes.extend(["-v", f"{p}:/workspace/{p.name}{suffix}"])

    try:
        return run_docker(image, volumes, args, runs_under=shared_runs_under)
    finally:
        # rmdir, never rmtree: it removes only what is still EMPTY, so a directory that
        # turned out to hold something is left exactly where it is. The tree ends the
        # run as it started it, which is the whole promise of --cwd-ro.
        for point in reversed(made_mountpoints):
            try:
                point.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
