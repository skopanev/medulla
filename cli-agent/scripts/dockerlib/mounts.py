"""Everything the container can see: what is mounted where, and why.

Split out of docker.py under the project's 250-line rule ($MAX_LOC). This is the file
that decides a container's whole view of the world — the workspace, credentials, the
overlay, a worktree's real git directory — so it is also the file to read when
something inside cannot find something that plainly exists outside.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))   # source checkout: medulla lives one level up

from dockerlib.image import _config_yaml
from dockerlib.mountfiles import _mount_agy_keys, _mount_init_docker
from dockerlib.paths import workspace_cwd

# Home of the NON-ROOT user INSIDE the container. Only the FALLBACK: docker.py probes
# the resolved image (image_home) and assigns the real one here before mounts are
# built, because the two images in play run as different users and a hardcode silently
# drops every $HOME-based credential for one of them.
CONTAINER_HOME = "/home/hltm"


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
    cwd = workspace_cwd()
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
    #
    # Checked for cwd AND for its immediate children: a pack that hands an agent an
    # isolated copy usually creates the worktree INSIDE the project it is working on,
    # and cwd is then the project, not the worktree. That case used to fall through
    # this branch entirely and every git command inside answered "fatal: not a git
    # repository: (null)" — reported from a develop pack, where it makes the whole
    # arrangement unusable. One level only: deeper is a tree walk on every run, for a
    # layout nobody has needed yet.
    for holder in [cwd, *(d for d in cwd.iterdir() if d.is_dir())] if cwd.is_dir() else [cwd]:
        _mount_worktree_gitdir(holder, add, cwd_ro)

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


def _mount_worktree_gitdir(holder: Path, add, cwd_ro: bool) -> None:
    """If `holder` is a git worktree, mount the repository its .git file points into.

    The pointer is an absolute HOST path, so mounting the common .git at that same
    path is what makes it resolve on both sides of the boundary.
    """
    pointer = holder / ".git"
    if not pointer.is_file():
        return
    try:
        line = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not line.startswith("gitdir:"):
        return
    gitdir = Path(line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (holder / gitdir).resolve()
    # the worktree's own dir holds no objects or refs — the COMMON .git does
    common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
    if common.is_dir():
        add(common, str(common), ro=cwd_ro)
