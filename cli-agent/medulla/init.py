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


WORKFLOW_YAML = """\
# <NAME> — describe what this workflow does in one line.
#
# Run:      medulla -w .medulla/workflows/<NAME>
# Explore:  medulla -w .medulla/workflows/<NAME> --dry-run
# Full API: medulla --help   (env vars, signals, docker)
version: "2"
start: hello

# vars: {BRANCH: main}          # {{var:BRANCH}} in prompts/commands
# defaults:
#   timeout: 1800
#   fallback: {agent: {harness: claude-code, model: opus}}

nodes:

  hello:
    shell: |
      echo "hello from <NAME>"
      echo "<signal:ok>it works</signal:ok>"
    timeout: 60
    on_signal: {ok: __exit_ok__}

  # An agent node (delete hello above, rename this to your liking):
  # work:
  #   agent: {harness: claude-code, model: sonnet}
  #   prompt: |
  #     {{file:prompts/task.md}}
  #     Do the thing, then emit the signal named done.
  #   post: 'test -s artifact.md'         # the truth channel: verify, don't trust
  #   max_attempts: 2
  #   on_signal: {done: __exit_ok__, __failed__: __exit_fail__}

  # A pool (fan-out over inputs; the join routes, bodies write to the manifest):
  # sweep:
  #   inputs: {shell: "ls *.md"}          # or a YAML list, or JSON/JSONL output
  #   max_parallel: 4
  #   min_success: 1
  #   shell: 'echo "processing $MEDULLA_INPUT"'
  #   on_signal: {__done__: __exit_ok__, __empty__: __exit_ok__}
"""

WORKFLOW_README = """\
# <NAME>

A medulla workflow. Edit workflow.yaml; keep prompts in prompts/.

- run:        medulla -w .medulla/workflows/<NAME>
- dry run:    medulla -w .medulla/workflows/<NAME> --dry-run
- resume:     medulla -w .medulla/workflows/<NAME> --resume
- reference:  medulla --help  (all MEDULLA_* env vars and signal syntax)
- history:    runs/<ts>-<id>/  (journal, per-step logs, outcome.json)
- secrets:    put KEY=VALUE into .env here — children see them as env,
              templates and run history never do
"""

GITIGNORE = ".env\nruns/\n"

SKILL_MD = """\
---
name: <NAME>
description: |
  One paragraph: when should an agent reach for this workflow?
  Trigger phrases, use cases, what it returns.
---

Run the workflow and read its result:

    medulla -w .medulla/workflows/<NAME> --var "KEY=VALUE" >&2
    # outputs land in the newest runs/<ts>-<id>/ directory
"""


SKILL_DESTS = (          # every agent CLI that reads skills (main@dca7dbf)
    Path(".claude") / "skills",      # claude-code
    Path(".agents") / "skills",      # codex
    Path(".opencode") / "skills",    # opencode
)


def install_skill_md(name: str, workflow_dir: Path) -> int:
    """Register the workflow's SKILL.md with every agent CLI's skill dir."""
    import shutil
    src = workflow_dir / "SKILL.md"
    if not src.is_file():                      # scaffolds get a starter
        src.write_text(SKILL_MD.replace("<NAME>", name), encoding="utf-8")
        print(f"  created starter {src} — edit the description")
    for root in SKILL_DESTS:
        dest = root / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "SKILL.md")
        print(f"  skill installed -> {dest}/SKILL.md")
    return 0


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
    import os
    import shutil
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
    import os
    import shutil
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        print(f"error: not a directory: {root_p}")
        return 1
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


def deploy_template(name: str) -> int:
    """Copy a bundled workflow (template) into the project."""
    import shutil
    dest = Path(".medulla") / "workflows" / name
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
