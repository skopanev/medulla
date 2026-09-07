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
