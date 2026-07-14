"""`medulla install-skill` — install a bundled workflow into the project + Claude / Cursor.

Usage:
    medulla install-skill <name>           # workflow → .medulla/ + skill → claude + cursor
    medulla install-skill <name> --claude  # skill → Claude Code only
    medulla install-skill <name> --cursor  # skill → Cursor only

Workflow files are bundled inside the medulla package under medulla/pipelines/<name>/.
They are copied to .medulla/pipelines/<name>/ in the current project.
SKILL.md is also installed into Claude Code and/or Cursor.
"""

from __future__ import annotations

import re
import shutil
from importlib import resources
from pathlib import Path


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _find_local_skill_md(name: str) -> tuple[Path, Path] | None:
    """Return (skill_md, workflow_dir) from local filesystem, or None.
    Resolves symlinks so the source dir stays valid after dest cleanup."""
    # source-tree location: cli-agent/pipelines/ (when running from repo)
    pkg_parent = Path(__file__).resolve().parent.parent
    candidates = [
        Path(name) / "SKILL.md",
        pkg_parent / "pipelines" / name / "SKILL.md",
        Path(".medulla") / "pipelines" / name / "SKILL.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            wf = candidate.parent.resolve()
            return wf / "SKILL.md", wf
    return None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        meta = {}
    return meta, text[m.end():]


def _to_cursor_mdc(text: str, name: str) -> str:
    meta, body = _parse_frontmatter(text)
    raw = meta.get("description", name) or name
    description = str(raw).strip().splitlines()[0]
    return f"---\ndescription: {description}\nglobs:\nalwaysApply: false\n---\n{body}"


def _install_from_dir(name: str, workflow_dir: Path, skill_md: Path,
                      *, claude: bool, cursor: bool) -> int:
    text = skill_md.read_text(encoding="utf-8")

    dest_wf = Path(".medulla") / "pipelines" / name
    if dest_wf.is_symlink():
        dest_wf.unlink()
    elif dest_wf.exists():
        shutil.rmtree(dest_wf)
    shutil.copytree(workflow_dir, dest_wf,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__init__.py"))
    print(f"workflow: {dest_wf}/")

    if claude:
        dest_dir = Path(".claude/commands")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.md"
        shutil.copy2(skill_md, dest)
        print(f"claude: {dest}")

    if cursor:
        dest_dir = Path(".cursor/rules")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.mdc"
        dest.write_text(_to_cursor_mdc(text, name), encoding="utf-8")
        print(f"cursor: {dest}")

    return 0


def install_skill(name: str, *, claude: bool, cursor: bool) -> int:
    # try bundled package first — do everything inside the as_file context
    # so the temp dir stays alive for the duration of the copy
    try:
        pkg = resources.files("medulla") / "pipelines" / name
        with resources.as_file(pkg) as wf_path:
            if wf_path.is_dir():
                skill_md = wf_path / "SKILL.md"
                if skill_md.is_file():
                    return _install_from_dir(name, wf_path, skill_md,
                                             claude=claude, cursor=cursor)
    except Exception:
        pass

    # fallback: local path
    local = _find_local_skill_md(name)
    if local:
        skill_md, workflow_dir = local
        return _install_from_dir(name, workflow_dir, skill_md,
                                 claude=claude, cursor=cursor)

    print(f"error: workflow '{name}' not found in medulla package or locally")
    return 1


def run_install_skill(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="medulla install-skill")
    parser.add_argument("name", help="bundled workflow name (e.g. spar)")
    parser.add_argument("--claude", action="store_true", help="install skill for Claude Code only")
    parser.add_argument("--cursor", action="store_true", help="install skill for Cursor only")
    ns = parser.parse_args(argv)

    if not ns.claude and not ns.cursor:
        ns.claude = True
        ns.cursor = True

    rc = install_skill(ns.name, claude=ns.claude, cursor=ns.cursor)
    if rc == 0:
        # Provision the runtime too, sourced from the (global) install, so a
        # single `install-skill` yields a docker-runnable setup — no separate
        # `init` step. Idempotent; never touches .medulla/pipelines/.
        from .init import run_init
        run_init()
    return rc
