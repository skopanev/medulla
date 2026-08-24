"""`medulla init` — bootstrap the runtime in the current project.

Lays down only what medulla needs to run in-place and inside docker:

  .medulla/
    medulla/           symlink → the installed medulla package
    scripts/           symlink → the installed package's scripts/
                       (docker.py, host-builder.sh, init-docker.sh)
    snapshot/          empty state dir for per-round artifacts

The runtime is SYMLINKED to the active (global) install rather than copied,
so it never goes stale: `medulla upgrade` is reflected everywhere with no
re-init. docker.py resolves the link via os.path.realpath when mounting
init-docker.sh, so the bind-mount source is the real package file.

Workflows (``.medulla/workflows/``) are NOT provisioned by this command —
they're project content. Use ``install-skill`` for bundled ones.
"""

from __future__ import annotations

from pathlib import Path


def _ensure_gitignore(patterns: list[str]) -> None:
    gitignore = Path(".gitignore")
    existing: set[str] = set()
    if gitignore.is_file():
        existing = set(gitignore.read_text(encoding="utf-8").splitlines())
    missing = [p for p in patterns if p not in existing]
    if not missing:
        return
    with gitignore.open("a", encoding="utf-8") as f:
        for p in missing:
            f.write(p + "\n")


def _symlink(link: Path, target: Path) -> None:
    """Point `link` at `target`, replacing any existing file/dir/symlink."""
    if link.is_symlink() or link.exists():
        if link.is_dir() and not link.is_symlink():
            import shutil
            shutil.rmtree(link)
        else:
            link.unlink()
    link.symlink_to(target, target_is_directory=target.is_dir())


# The text init writes — medulla/templates.py.
from .templates import GITIGNORE, SKILL_MD, WORKFLOW_README, WORKFLOW_YAML  # noqa: E402

SKILL_DESTS = (          # every agent CLI that reads skills (main@dca7dbf)
    Path(".claude") / "skills",      # claude-code
    Path(".agents") / "skills",      # codex
    Path(".opencode") / "skills",    # opencode
)

def skill_dests_global() -> tuple[Path, ...]:
    """The same CLIs, machine-wide: a skill here works in EVERY repo, which is what a
    machine-wide workflow wants — one copy to update, no install step per checkout.

    Resolved on CALL, never at import: Path.home() captured at import time ignores a
    later HOME change, which silently wrote into the real home during tests.
    opencode reads ~/.config/opencode — the per-user layout differs from per-project.
    """
    home = Path.home()
    dests = [home / ".claude" / "skills",
             home / ".agents" / "skills",
             home / ".config" / "opencode" / "skills"]
    # Claude Code profiles: CLAUDE_CONFIG_DIR points at ~/.claude-<name>, and a user
    # with several of them (work, personal, a client) has several skill directories.
    # Only EXISTING ones — this must not conjure a profile nobody uses. Found by a
    # refresh that reported success while five machine-wide copies stayed stale.
    dests += sorted(p / "skills" for p in home.glob(".claude-*")
                    if p.is_dir() and (p / "skills").is_dir())
    return tuple(dests)


def install_skill_md(name: str, workflow_dir: Path, local: bool = False) -> int:
    """Register the workflow's SKILL.md with every agent CLI's skill dir.

    Sourced the same way the workflow itself is: local copy first, then the
    machine-wide one in ~/.medulla/workflows/<name>. A shared definition should not
    force every repo to keep its own duplicate of the skill text — refresh already
    reads it from the bundle, and this made `init --skill` the only command that
    still demanded a local file.
    """
    import shutil
    src = workflow_dir / "SKILL.md"
    if not src.is_file():
        shared = Path.home() / ".medulla" / "workflows" / name / "SKILL.md"
        if shared.is_file():
            src = shared
    if not src.is_file():                      # scaffolds get a starter
        src = workflow_dir / "SKILL.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(SKILL_MD.replace("<NAME>", name), encoding="utf-8")
        print(f"  created starter {src} — edit the description")
    for root in (SKILL_DESTS if local else skill_dests_global()):
        dest = root / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "SKILL.md")
        print(f"  skill installed -> {dest}/SKILL.md")
    return 0


# Finding a bundle and refreshing deploys — medulla/refresh.py.
from .refresh import (  # noqa: E402,F401
    DEFAULT_REFRESH_DEPTH,
    _bundle_dir,
    _copy_bundle_over,
    refresh_skill,
)


def bundled_templates() -> list[str]:
    try:
        from importlib import resources
        root = resources.files("medulla") / "workflows"
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and (d / "workflow.yaml").is_file())
    except Exception:
        src = Path(__file__).resolve().parent.parent / "workflows"
        if src.is_dir():
            return sorted(d.name for d in src.iterdir()
                          if (d / "workflow.yaml").is_file())
        return []


def deploy_template(name: str, local: bool = False) -> int:
    """Install a bundled workflow — machine-wide by default, or into this project.

    Machine-wide (~/.medulla/workflows/<name>) is the default because a template is
    the same everywhere: one copy to update, and every repo resolves it (see
    v2/cli.py::_resolve_workflow_yaml). runs/ still land in the project that started
    them, so history never pools. --local writes into .medulla/workflows/<name>
    instead, and a local copy always wins over the machine-wide one.
    """
    dest = (Path(".medulla") if local else Path.home() / ".medulla") / "workflows" / name
    existed = (dest / "workflow.yaml").exists()   # overwrite by default: re-deploy
                                                  # refreshes template files; runs/ is
                                                  # preserved (ignored from source below)
    from importlib import resources
    src_path = None
    try:
        src_path = Path(str(resources.files("medulla") / "workflows" / name))
    except Exception:
        pass
    if src_path is None or not src_path.is_dir():      # source-mode layout
        src_path = Path(__file__).resolve().parent.parent / "workflows" / name
    if not src_path.is_dir():
        print(f"error: bundled template '{name}' not found")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    _copy_bundle_over(src_path, dest)          # symlink-safe (no CWE-59 write-through), runs/ kept
    verb = "re-deployed (overwrote)" if existed else "deployed"
    print(f"{verb} template '{name}' -> {dest}/")
    print(f"  run:   medulla -w {dest}")
    return 0


def scaffold_workflow(name: str) -> int:
    import re
    if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", name):
        print(f"error: workflow name '{name}' must match [A-Za-z][A-Za-z0-9_-]*")
        return 1
    dest = Path(".medulla") / "workflows" / name
    if (dest / "workflow.yaml").exists():
        print(f"error: {dest}/workflow.yaml already exists")
        return 1
    (dest / "prompts").mkdir(parents=True, exist_ok=True)
    (dest / "workflow.yaml").write_text(
        WORKFLOW_YAML.replace("<NAME>", name), encoding="utf-8")
    (dest / "README.md").write_text(
        WORKFLOW_README.replace("<NAME>", name), encoding="utf-8")
    (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    print(f"created {dest}/ (workflow.yaml, README.md, .gitignore, prompts/)")
    print(f"  edit:  {dest}/workflow.yaml")
    print(f"  run:   medulla -w {dest}")
    return 0


def run_init() -> int:
    # The active install: this module lives inside the installed package, so its
    # parent IS the package dir we want to link against (global pipx, venv, …).
    pkg = Path(__file__).resolve().parent
    dest = Path(".medulla")

    print(f"setting up medulla runtime in {dest}/ ...")
    dest.mkdir(parents=True, exist_ok=True)

    # Symlink the package + scripts to the live install — no stale copies.
    _symlink(dest / "medulla", pkg)
    scripts_src = pkg / "scripts"
    if scripts_src.is_dir():
        _symlink(dest / "scripts", scripts_src)

    (dest / "snapshot").mkdir(parents=True, exist_ok=True)

    _ensure_gitignore([".medulla/logs", ".medulla/human.md", ".medulla/medulla", ".medulla/scripts"])

    print(f"  linked .medulla/medulla → {pkg}")
    print("\ndone.\n")
    print("  # drop your workflows into .medulla/workflows/<name>/workflow.yaml")
    print("  # then run:")
    print("  medulla --docker -w <workflow>\n")
    return 0
