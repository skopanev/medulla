"""Finding the bundled copy of a workflow, and refreshing every deploy of it.

Split from init.py under the project's 250-line rule ($MAX_LOC). init.py CREATES things;
this file updates what already exists on disk — a different job, with a different way of
going wrong: it walks somebody's home directory, so every guard here is about not
touching what medulla does not own.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .v2.workflow_path import shared_workflows

DEFAULT_REFRESH_DEPTH = 4
_REFRESH_PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv", "runs"}


def _bundle_dir(name: str) -> Path | None:
    """The bundled template dir (source of truth) for `name`, installed or source."""
    from importlib import resources
    try:
        p = Path(str(resources.files("medulla") / "workflows" / name))
        if (p / "workflow.yaml").is_file():
            return p
    except Exception:
        pass
    p = Path(__file__).resolve().parent.parent / "workflows" / name
    return p if (p / "workflow.yaml").is_file() else None


# dirs never worth descending into when scanning for deployed copies
_REFRESH_PRUNE = {".git", "node_modules", ".venv", "venv", "__pycache__",
                  "runs", ".next", "dist", "build", ".cache", ".turbo",
                  "target", "vendor", "Pods", "DerivedData", ".gradle", ".m2",
                  ".tox", ".terraform", ".pytest_cache", ".idea", ".svelte-kit",
                  ".dart_tool", "coverage", "out"}
# the agent-CLI dirs whose skills/<name>/ we own (project-local)
_SKILL_PARENTS = {".claude", ".agents", ".opencode"}
# bounded default so `refresh <name> ~/Projects` can't hang a full-home walk;
# a repo's deploy sits at rel-depth ~4-5 (repo/.medulla/workflows/<name>), so 8
# covers nested layouts with headroom. Raise with --depth for deeper trees.
DEFAULT_REFRESH_DEPTH = 8


def _copy_bundle_over(src: Path, dst: Path) -> None:
    """Copy every bundle file into `dst`, skipping runs/ — and NEVER writing
    through a symlink (a symlinked dest file OR subdir is left untouched, so a
    booby-trapped deploy can't clobber a file outside itself: CWE-59)."""
    for base_dir, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in ("runs", "__pycache__")]
        tgt_dir = dst / Path(base_dir).relative_to(src)
        if tgt_dir.is_symlink():            # symlinked subdir → don't descend/write
            print(f"  skip symlink dir {tgt_dir} (left as-is; deploy partial there)")
            dirnames[:] = []
            continue
        tgt_dir.mkdir(parents=True, exist_ok=True)
        for f in filenames:
            if f.endswith(".pyc"):
                continue
            target = tgt_dir / f
            if target.is_symlink():         # never write through a file symlink
                print(f"  skip symlink {target} (left as-is)")
                continue
            shutil.copy2(Path(base_dir) / f, target)


def refresh_skill(name: str, root: str, depth: int = DEFAULT_REFRESH_DEPTH, dry_run: bool = False) -> int:
    """Walk `root` (up to `depth` levels) and refresh every medulla-OWNED deploy
    of `name` from the current bundle. Owned = `.medulla/workflows/<name>/` (a
    workflow, runs/ preserved) or `{.claude,.agents,.opencode}/skills/<name>/`
    (a SKILL.md). The grandparent gate is the safety: it never touches a
    same-named dir belonging to another tool, and never the bundle itself."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        print(f"error: not a directory: {root_p}")
        return 1
    from .init import bundled_templates  # local: init.py imports this module
    bundle = _bundle_dir(name)
    if bundle is None:
        print(f"error: no bundled '{name}' (bundled: {', '.join(bundled_templates()) or 'none'})")
        return 1
    bundle, bundle_skill = bundle.resolve(), (bundle / "SKILL.md").resolve()
    tag = " [dry-run]" if dry_run else ""
    print(f"scanning {root_p} for '{name}' (depth {depth}){tag} — "
          f"refreshing every medulla-owned copy to the current version "
          f"(deploys deeper than {depth} levels are skipped — raise with --depth)...")
    base = len(root_p.parts)
    n_wf = n_sk = 0
    failures: list[str] = []

    # The machine-wide copy FIRST, and whether or not it sits under `root`. It is the
    # one every bare name resolves to and the one repo-local symlinks point at, so
    # leaving it stale leaves everything stale — and it lives in $HOME, which is not
    # the folder anyone passes here. Seen live: `refresh spar ~/Projects` reported
    # "refreshed 1 workflow" while the definition every one of those repos actually
    # runs stayed two versions behind.
    shared = shared_workflows() / name
    if (shared / "workflow.yaml").is_file() and shared.resolve() != bundle:
        if dry_run:
            print(f"  [dry-run] workflow -> {shared} (machine-wide)")
        else:
            try:
                _copy_bundle_over(bundle, shared)
                print(f"  workflow  -> {shared} (machine-wide)")
            except OSError as exc:
                failures.append(f"{shared}: {exc}")
        n_wf += 1
    for dirpath, dirnames, _ in os.walk(root_p):     # followlinks=False: no escape/cycles
        p = Path(dirpath)
        if len(p.parts) - base >= depth:
            dirnames[:] = []                         # at the depth limit — don't descend
        dirnames[:] = [d for d in dirnames if d not in _REFRESH_PRUNE]
        if p.name != name:
            continue
        gp = p.parent.parent.name
        if (p.parent.name == "workflows" and gp == ".medulla"
                and (p / "workflow.yaml").is_file() and p.resolve() != bundle):
            if dry_run:
                print(f"  [dry-run] workflow -> {p}"); n_wf += 1; continue
            try:                                     # one bad deploy must not abort the rest
                _copy_bundle_over(bundle, p)
                print(f"  workflow  -> {p}"); n_wf += 1
            except OSError as e:
                print(f"  FAILED    -> {p}: {e}"); failures.append(str(p))
        elif (p.parent.name == "skills" and gp in _SKILL_PARENTS
                and (p / "SKILL.md").is_file() and bundle_skill.is_file()):
            target = p / "SKILL.md"
            if target.is_symlink():                  # never write through a symlink
                print(f"  skip symlink {target}"); continue
            if dry_run:
                print(f"  [dry-run] SKILL.md -> {p}"); n_sk += 1; continue
            try:
                shutil.copy2(bundle_skill, target)
                print(f"  SKILL.md  -> {p}"); n_sk += 1
            except OSError as e:
                print(f"  FAILED    -> {target}: {e}"); failures.append(str(target))
    verb = "would refresh" if dry_run else "refreshed"
    print(f"{verb} {n_wf} workflow(s) + {n_sk} skill(s) under {root_p} (depth {depth})")
    if failures:
        print(f"  {len(failures)} failed mid-write (may be partial): " + ", ".join(failures[:5])
              + (" …" if len(failures) > 5 else ""))
    return 2 if failures else 0


