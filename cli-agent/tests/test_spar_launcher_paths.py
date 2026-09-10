"""Launch-path checks with stub executables: no Docker, models, or real runs."""
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/spar-run.sh"


def _launch(tmp_path, spelling, unrelated=False):
    project = tmp_path / "project"
    (project / ".medulla/workflows/spar").mkdir(parents=True)
    question = tmp_path / "question.md"
    question.write_text("Review this fixture.\n")
    storage = tmp_path / "physical storage"
    storage.mkdir()
    linked = tmp_path / "linked storage"
    linked.symlink_to(storage, target_is_directory=True)
    box = (linked if spelling == "symlink" else storage) / "panel runs/new box"
    argument = os.path.relpath(box, project) if spelling == "relative" else str(box)
    recorded = tmp_path / "runs-folder-argument"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    scripts = {
        "docker": "#!/bin/sh\nexit 0\n",
        "medulla": '''#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
    if [ "$1" = --runs-folder ]; then
        box=$2
        shift 2
    else
        shift
    fi
done
printf '%s\\n' "$box" > "$SPAR_TEST_ARGUMENT"
physical=$(cd "$box" && pwd -P)
printf 'warning before the run path\\n'
printf '%s/fixture-run\\n' "${SPAR_TEST_OUTPUT_ROOT:-$physical}"
''',
    }
    for name, body in scripts.items():
        executable = binaries / name
        executable.write_text(body)
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binaries}:/bin:/usr/bin",
        "MEDULLA_PANEL_RUNS": argument,
        "SPAR_TEST_ARGUMENT": str(recorded),
        "SPAR_TEST_OUTPUT_ROOT": str(tmp_path / "unrelated") if unrelated else "",
    }
    result = subprocess.run(
        ["/bin/bash", str(LAUNCHER), "start", str(question)],
        cwd=project, env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    return result, box.resolve(), recorded.read_text().strip()


@pytest.mark.parametrize("spelling", ["symlink", "relative", "physical"])
def test_start_accepts_physical_run_path_before_directory_exists(tmp_path, spelling):
    """The engine resolves its run root; a linked ancestor must not cause exit 2."""
    result, physical_box, passed_box = _launch(tmp_path, spelling)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(physical_box / "fixture-run")
    assert passed_box == str(physical_box)
    assert not (physical_box / "fixture-run").exists()
    assert len(list(physical_box.glob("question.*.md"))) == 1


def test_start_still_rejects_a_run_path_outside_its_box(tmp_path):
    result, _, _ = _launch(tmp_path, "symlink", unrelated=True)
    assert result.returncode == 2
    assert "no usable run directory" in result.stderr
    assert result.stdout == ""


def test_the_lane_is_named_after_the_box(tmp_path):
    """A panel round is a LANE, and the box already says which worktree or ticket it
    belongs to. Without passing that on, the run directory — and the container named
    after it — is a timestamp plus eight random hex, so `docker ps` answers "how many
    lanes are up and on what" with neither. Measured before this: three panels running,
    three names of the form medulla-2026-09-10_10-12-34-094c5165."""
    project = tmp_path / "project"
    (project / ".medulla/workflows/spar").mkdir(parents=True)
    question = tmp_path / "q.md"
    question.write_text("Review this fixture.\n")
    box = tmp_path / "panel-runs" / "wt-ticket-9d3b-bd726e15"
    recorded = tmp_path / "seen-run-id"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text("#!/bin/sh\nexit 0\n")
    (binaries / "medulla").write_text(
        '#!/bin/sh\nprintf %s "${MEDULLA_RUN_ID-}" > "$SPAR_TEST_RUN_ID"\n'
        'printf "%s/fixture-run\\n" "$PWD"\n')
    for name in ("docker", "medulla"):
        (binaries / name).chmod(0o755)
    subprocess.run(["/bin/bash", str(LAUNCHER), "start", str(question)],
                   cwd=project, capture_output=True, text=True, timeout=10, check=False,
                   env={**os.environ, "PATH": f"{binaries}:/bin:/usr/bin",
                        "MEDULLA_PANEL_RUNS": str(box),
                        "SPAR_TEST_RUN_ID": str(recorded)})
    seen = recorded.read_text().strip()
    assert "wt-ticket-9d3b" in seen, f"the lane is unnamed: {seen!r}"


def test_a_callers_own_lane_id_is_not_overwritten(tmp_path):
    """A default, not a policy: an orchestrator that already correlates by its own id
    must keep it, or correlation breaks exactly where it was asked for."""
    project = tmp_path / "project"
    (project / ".medulla/workflows/spar").mkdir(parents=True)
    question = tmp_path / "q.md"
    question.write_text("Review this fixture.\n")
    recorded = tmp_path / "seen-run-id"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text("#!/bin/sh\nexit 0\n")
    (binaries / "medulla").write_text(
        '#!/bin/sh\nprintf %s "${MEDULLA_RUN_ID-}" > "$SPAR_TEST_RUN_ID"\n'
        'printf "%s/fixture-run\\n" "$PWD"\n')
    for name in ("docker", "medulla"):
        (binaries / name).chmod(0o755)
    subprocess.run(["/bin/bash", str(LAUNCHER), "start", str(question)],
                   cwd=project, capture_output=True, text=True, timeout=10, check=False,
                   env={**os.environ, "PATH": f"{binaries}:/bin:/usr/bin",
                        "MEDULLA_PANEL_RUNS": str(tmp_path / "panel-runs" / "box"),
                        "MEDULLA_RUN_ID": "orchestrator-lane-7",
                        "SPAR_TEST_RUN_ID": str(recorded)})
    assert recorded.read_text().strip() == "orchestrator-lane-7"


# ── the run directory's own name must not be able to lose the outcome ───────────

def test_a_lane_named_run_still_knows_when_it_started(tmp_path):
    """`started_at` parsed the directory name with rsplit("-", 1), which assumed the
    suffix was one dash-free token. That held while it was eight hex characters. Since
    lane names arrived — `wt-x4lu-db-fence-8527c8f7-30130` — rsplit handed strptime a
    timestamp with half the lane glued on, and it raised.

    It raised from _normalize_outcome, which runs on the way OUT: the round had
    finished, the verdict was on disk, and outcome.json was never written. Measured
    before the fix: 22 rounds named that way in one day, 20 with no outcome.json —
    the exact "finished but unmarked" symptom that was being chased in the observer.
    """
    import datetime
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from medulla.v2.rundir import RunStore

    for name in ("2026-09-10_22-36-20-c465bdd7",                       # the old shape
                 "2026-09-10_23-08-49-work-0c8fe162-30130",            # a lane name
                 "2026-09-10_23-08-49-wt-x4lu-db-fence-8527c8f7-30130"):
        store = RunStore.__new__(RunStore)
        store.dir = tmp_path / name
        assert store.started_at == datetime.datetime(2026, 9, 10,
                                                     *([22, 36, 20] if "c465" in name
                                                       else [23, 8, 49])), name


def test_an_unparseable_run_name_does_not_cost_the_outcome(tmp_path):
    """A duration is worth less than the record that the run finished. If the name
    carries no timestamp at all, fall back rather than raise on the way out."""
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from medulla.v2.rundir import RunStore

    store = RunStore.__new__(RunStore)
    store.dir = tmp_path / "not-a-timestamp-at-all"
    store.dir.mkdir()
    store.started_at          # must not raise
