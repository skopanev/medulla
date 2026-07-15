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

Workflows (``.medulla/pipelines/``) are NOT provisioned by this command —
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


PIPELINE_YAML = """\
# <NAME> — describe what this pipeline does in one line.
#
# Run:      medulla -w .medulla/pipelines/<NAME>
# Explore:  medulla -w .medulla/pipelines/<NAME> --dry-run
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

PIPELINE_README = """\
# <NAME>

A medulla pipeline. Edit pipeline.yaml; keep prompts in prompts/.

- run:        medulla -w .medulla/pipelines/<NAME>
- dry run:    medulla -w .medulla/pipelines/<NAME> --dry-run
- resume:     medulla -w .medulla/pipelines/<NAME> --resume
- reference:  medulla --help  (all MEDULLA_* env vars and signal syntax)
- history:    runs/<ts>-<id>/  (journal, per-step logs, outcome.json)
- secrets:    put KEY=VALUE into .env here — children see them as env,
              templates and run history never do
"""

GITIGNORE = ".env\nruns/\n"


def scaffold_pipeline(name: str) -> int:
    import re
    if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", name):
        print(f"error: pipeline name '{name}' must match [A-Za-z][A-Za-z0-9_-]*")
        return 1
    dest = Path(".medulla") / "pipelines" / name
    if (dest / "pipeline.yaml").exists():
        print(f"error: {dest}/pipeline.yaml already exists")
        return 1
    (dest / "prompts").mkdir(parents=True, exist_ok=True)
    (dest / "pipeline.yaml").write_text(
        PIPELINE_YAML.replace("<NAME>", name), encoding="utf-8")
    (dest / "README.md").write_text(
        PIPELINE_README.replace("<NAME>", name), encoding="utf-8")
    (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    print(f"created {dest}/ (pipeline.yaml, README.md, .gitignore, prompts/)")
    print(f"  edit:  {dest}/pipeline.yaml")
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
    print("  # drop your pipelines into .medulla/pipelines/<name>/pipeline.yaml")
    print("  # then run:")
    print("  medulla --docker -w <workflow>\n")
    return 0
