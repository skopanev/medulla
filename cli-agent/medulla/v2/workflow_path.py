"""Which yaml does `-w` mean? ONE answer, for every process that asks.

The engine and the container wrapper (scripts/docker.py) both have to answer this,
and they used to answer it separately. The wrapper picks the image and the tmpfs
isolation policy from its answer; the engine runs the graph from its own. When the
two drifted the result was a chimera — the right workflow under another one's image,
or without the isolation it declared. So this module is the single answer, imported
by both; docker.py runs on the same interpreter and needs no packaging change.

stdlib only, deliberately: docker.py must be able to import this before it has
established that pyyaml is installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SHARED_DIR = (".medulla", "workflows")


def shared_workflows() -> Path:
    """Machine-wide workflow definitions: one copy per machine, every repo uses it.

    Resolved on CALL: a module-level Path.home() freezes at import and ignores a
    later HOME — the same trap that made the suite write into a real home directory.
    """
    return Path.home() / ".medulla" / "workflows"


def _usable(p: Path) -> bool:
    """A ZERO-BYTE yaml is debris, not a definition — an interrupted write, a stray
    shell redirect, or a bind-mount target the Docker daemon created (fback-yimerxmy0y:
    one such file outranked the machine-wide definition and broke every run for a day,
    while the symptom read as 'the panelists did not deliver'). Empty carries no
    intent, so it does not count as a file at all. A file with content still wins,
    however broken — that IS someone's intent, and ignoring it would be worse.
    """
    return p.is_file() and p.stat().st_size > 0


def config_yaml(d: Path) -> Path:
    """Read-side config lookup inside one directory: workflow.yaml, else the pre-4.1
    name pipeline.yaml — old projects and old run dirs keep working untouched (10+
    such workflows are still live). Writes always use the new name.

    Returns the workflow.yaml path even when nothing is there, so callers can report
    the name a user would expect to see.
    """
    w = d / "workflow.yaml"
    if _usable(w):
        return w
    legacy = d / "pipeline.yaml"
    return legacy if _usable(legacy) else w


def shared_name_for(w: Path) -> str | None:
    """The workflow NAME this path is a canonical reference to, or None.

    Canonical means a bare name (`-w spar`) or a path ending in
    `.medulla/workflows/<name>`, optionally naming the yaml inside it. Anything else
    that does not exist on disk is a typo, and a typo must not select a workflow:
    `-w typo/spar/missing.yaml` used to silently run the shared `spar`, because the
    fallback keyed off the parent directory's name whatever the rest of the path said.

    The shape is what counts, not where the path lives: `-w /another/repo/.medulla/
    workflows/<name>` resolves to the machine-wide copy too, which is the point — any
    repo may reference it. Only a path that exists is ever preferred over it.
    """
    parts = w.parts
    if len(parts) == 1 and not w.suffix:
        return w.name
    # Match the SHAPE of the path, never guess "file or directory?" from a suffix:
    # a workflow directory may legitimately carry a dot (`my.workflows`), and treating
    # it as a file chopped off the real name and refused to resolve it at all.
    if len(parts) >= 3 and parts[-3:-1] == _SHARED_DIR:
        return parts[-1]                      # .../.medulla/workflows/<name>
    if len(parts) >= 4 and parts[-4:-2] == _SHARED_DIR:
        return parts[-2]                      # .../.medulla/workflows/<name>/<file>.yaml
    return None


def _shared_yaml(name: str) -> Path | None:
    d = shared_workflows() / name
    y = config_yaml(d)
    return y if y.is_file() else None


def resolve_workflow_yaml(w: Path) -> Path:
    """LOCAL ALWAYS WINS, then the machine-wide copy.

    A definition can live once in ~/.medulla/workflows/<name>/ and serve every repo,
    so fixing it fixes all of them; a project that needs its own version just puts a
    real workflow.yaml in .medulla/workflows/<name>/ and nothing else changes.

    The path comes back AS GIVEN, never absolutised: --print-run-dir hands it to a
    caller who is on the host while the run happened inside the container, and an
    absolute container path does not exist for them.

    A path that exists is the answer. A path that does not is a fallback ONLY in the
    canonical shared form (see shared_name_for) — otherwise it is returned unchanged
    so the caller fails loudly on the path the user actually typed.
    """
    w = Path(w)
    if w.is_file():
        if _usable(w):
            return w
        _warn_debris(w)
        beside = config_yaml(w.parent)         # debris must not hide a working
        if _usable(beside):                    # definition next to it either
            return beside
        return _shared_yaml(w.parent.name) or w
    if w.is_dir():
        local = config_yaml(w)
        if _usable(local):
            return local
        if (w / "workflow.yaml").exists():     # exists but empty: say so once
            _warn_debris(w / "workflow.yaml")
        return _shared_yaml(w.name) or local
    name = shared_name_for(w)
    if name:
        return _shared_yaml(name) or w
    return w


def _warn_debris(p: Path) -> None:
    print(f"warning: ignoring empty {p} — it carries no definition; delete the file "
          f"to silence this", file=sys.stderr)


def workflow_dir_for(w: Path) -> Path:
    """The directory a workflow's own assets live in — prompts/, .env, a relative
    vars.DOCKERFILE. It is the RESOLVED yaml's directory, never the raw -w argument:
    for the flagship shared layout the raw argument is a repo-local path that does
    not exist, and deriving assets from it silently emptied the workflow .env tier
    and pointed relative Dockerfiles at a path that cannot exist.
    """
    return resolve_workflow_yaml(w).parent
