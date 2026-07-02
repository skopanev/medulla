"""`medulla init` — bootstrap the runtime in the current project.

Lays down only what medulla needs to run in-place and inside docker:

  .medulla/
    medulla/           symlink → the installed medulla package
    scripts/           symlink → the installed package's scripts/
                       (docker.py, host-builder.sh, bridge-shim.sh, init-docker.sh)
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
    print("  # drop your workflows into .medulla/workflows/<name>/pipeline.yaml")
    print("  # then run:")
    print("  medulla --docker -w <workflow>\n")
    return 0
