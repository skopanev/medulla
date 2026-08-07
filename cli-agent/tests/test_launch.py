"""`medulla launch <name>` must find a workflow's launcher from anywhere.

The failure it exists to stop, reported live: in a git worktree of finik-backend,
`.medulla/workflows/spar/scripts/spar-run.sh` is "no such file or directory", because
the aggregator's `.medulla` symlink does not follow into the worktree. The engine
already resolves `-w spar` by name from any directory; the launcher had no such rule
and depended on where the caller happened to stand.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _machine_wide(home: Path, name: str, script: str = None) -> Path:
    d = home / ".medulla" / "workflows" / name
    (d / "scripts").mkdir(parents=True)
    (d / "workflow.yaml").write_text(
        "version: '2'\nstart: n\nnodes:\n  n:\n    shell: 'true'\n    on_signal: {}\n")
    if script is not None:
        p = d / "scripts" / f"{name}-run.sh"
        p.write_text(script)
        p.chmod(0o755)
    return d


def _run(cwd: Path, home: Path, *args) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", "medulla", "launch", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_launcher_is_found_from_a_directory_that_has_no_medulla(tmp_path):
    """The worktree case: nothing local, and the call still works."""
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(home, "spar", "#!/bin/sh\necho \"launched in $PWD with: $*\"\n")
    elsewhere = tmp_path / "worktree"; elsewhere.mkdir()

    res = _run(elsewhere, home, "spar", "start", "q.md")
    assert res.returncode == 0, res.stderr
    assert "with: start q.md" in res.stdout


def test_the_launcher_runs_in_the_callers_directory(tmp_path):
    """A panel is about the tree you are standing in, not the one the workflow
    lives in — running it in the workflow's directory would review the wrong repo."""
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(home, "spar", "#!/bin/sh\necho \"$PWD\"\n")
    elsewhere = tmp_path / "worktree"; elsewhere.mkdir()

    res = _run(elsewhere, home, "spar")
    assert res.stdout.strip() == str(elsewhere.resolve())


def test_apple_runtime_flag_reaches_the_launcher(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(
        home, "spar",
        "#!/bin/sh\necho \"$MEDULLA_CONTAINER_RUNTIME:$*\"\n")

    res = _run(tmp_path, home, "spar", "--apple", "start", "q.md")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "apple:start q.md"


def test_a_local_copy_wins_exactly_as_for_the_workflow(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(home, "spar", "#!/bin/sh\necho machine-wide\n")
    repo = tmp_path / "repo"
    local = repo / ".medulla" / "workflows" / "spar" / "scripts"
    local.mkdir(parents=True)
    (local.parent / "workflow.yaml").write_text(
        "version: '2'\nstart: n\nnodes:\n  n:\n    shell: 'true'\n    on_signal: {}\n")
    p = local / "spar-run.sh"
    p.write_text("#!/bin/sh\necho local\n")
    p.chmod(0o755)

    assert _run(repo, home, "spar").stdout.strip() == "local"


def test_the_exit_code_is_the_launchers_own(tmp_path):
    """`wait` answers 2 for a failed panel and 3 for one that never finished —
    swallowing that would turn two different reactions into one."""
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(home, "spar", "#!/bin/sh\nexit 3\n")
    assert _run(tmp_path, home, "spar").returncode == 3


def test_an_unknown_workflow_says_where_to_look(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    res = _run(tmp_path, home, "nope")
    assert res.returncode == 1
    assert "no workflow 'nope'" in res.stderr and "medulla help" in res.stderr


def test_a_workflow_without_a_launcher_points_at_the_plain_run(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    _machine_wide(home, "plain")
    res = _run(tmp_path, home, "plain")
    assert res.returncode == 1
    assert "ships no launcher" in res.stderr
    assert "medulla -w plain" in res.stderr


def test_several_launchers_must_be_named(tmp_path):
    """Picking the alphabetically first would be a coin flip between
    'start the panel' and something else entirely."""
    home = tmp_path / "home"; home.mkdir()
    d = _machine_wide(home, "multi", "#!/bin/sh\necho first\n")
    second = d / "scripts" / "other.sh"
    second.write_text("#!/bin/sh\necho second\n")
    second.chmod(0o755)

    res = _run(tmp_path, home, "multi")
    assert res.returncode == 1 and "several launchers" in res.stderr
    assert _run(tmp_path, home, "multi", "other.sh").stdout.strip() == "second"


@pytest.mark.parametrize("args,expected", [([], "usage: medulla launch")])
def test_no_arguments_explains_itself(tmp_path, args, expected):
    home = tmp_path / "home"; home.mkdir()
    res = _run(tmp_path, home, *args)
    assert res.returncode == 1 and expected in res.stderr
