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

from .v2.contract import engine_is_older_than, engine_version
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
            # Replace the NAME, not the contents. copy2 writes through to the same
            # inode, and bash reads a script as it executes it — so refreshing under
            # a running spar-run.sh fed it new bytes at an old offset and it died on
            # `syntax error near unexpected token '('` mid-round, losing the verdict
            # for a panel that had already finished its work. os.replace swaps the
            # directory entry atomically: anything already reading keeps the old
            # inode to the end, and the next start opens the new one.
            tmp = tgt_dir / f".{f}.medulla-tmp-{os.getpid()}"
            try:
                shutil.copy2(Path(base_dir) / f, tmp)
                os.replace(tmp, target)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise




def _replace_file(src: Path, target: Path) -> None:
    """Swap the NAME, never the contents of a file someone may be reading.

    The same rule that spar-run.sh taught us the hard way: copy2 writes through to
    the existing inode, so a reader already inside the file gets new bytes at an old
    offset. A SKILL.md is read whole by an agent CLI at load time — a refresh landing
    mid-read hands it a splice of two versions, and unlike a shell script it fails
    silently, as prose that makes slightly less sense than it should.
    """
    tmp = target.with_name(f".{target.name}.medulla-tmp-{os.getpid()}")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _declared_min_engine(path: Path) -> str | None:
    """Read min_engine without a YAML parse: this runs before any engine machinery,
    and a definition too new to parse is exactly the case being caught."""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("min_engine:"):
                continue
            value = line.split(":", 1)[1].strip().strip("\"'")
            return value or None
            
    except OSError:
        return None
    return None


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

    # Refuse BEFORE copying anything. This command legitimately pushes the bundled
    # definition into the machine-wide copy and then into every repository it finds,
    # so installing an unreleased build and refreshing is enough to hand a
    # newer-than-the-engine definition to projects that never asked for it. The
    # failure then lands twenty minutes later on other people's lanes, who cannot
    # fix it. It belongs here, on the caller, at the moment of the call.
    required = _declared_min_engine(bundle / "workflow.yaml")
    if required is not None and engine_is_older_than(required):
        print(f"error: '{name}' declares min_engine: {required}, but the installed "
              f"engine is {engine_version()}. Refusing to publish a definition newer "
              f"than the engine that would run it — upgrade medulla first.")
        return 1

    tag = " [dry-run]" if dry_run else ""
    print(f"scanning {root_p} for '{name}' (depth {depth}){tag} — "
          f"refreshing every medulla-owned copy to the current version "
          f"(deploys deeper than {depth} levels are skipped — raise with --depth)...")
    n_wf = n_sk = 0
    failures: list[str] = []

    # Machine-wide SKILL copies, which live in $HOME and so are never under the search
    # root either: ~/.claude/skills, ~/.agents/skills, ~/.config/opencode/skills, and
    # one per Claude Code profile. Only where the skill is ALREADY installed — refresh
    # updates what exists, it does not deploy.
    from .init import skill_dests_global
    if bundle_skill.is_file():
        for dest in skill_dests_global():
            target = dest / name / "SKILL.md"
            if not target.is_file() or target.is_symlink():
                continue
            if dry_run:
                print(f"  [dry-run] SKILL.md -> {dest / name} (machine-wide)")
            else:
                try:
                    _replace_file(bundle_skill, target)
                    print(f"  SKILL.md  -> {dest / name} (machine-wide)")
                except OSError as exc:
                    failures.append(f"{target}: {exc}")
            n_sk += 1

    base = len(root_p.parts)

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
                _replace_file(bundle_skill, target)
                print(f"  SKILL.md  -> {p}"); n_sk += 1
            except OSError as e:
                print(f"  FAILED    -> {target}: {e}"); failures.append(str(target))
    verb = "would refresh" if dry_run else "refreshed"
    print(f"{verb} {n_wf} workflow(s) + {n_sk} skill(s) under {root_p} (depth {depth})")
    if failures:
        print(f"  {len(failures)} failed mid-write (may be partial): " + ", ".join(failures[:5])
              + (" …" if len(failures) > 5 else ""))
    return 2 if failures else 0


