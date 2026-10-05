"""Is the host idle enough to deploy? The check must answer with an exit code.

The line this replaces was typed by hand before a deploy and was wrong:
`docker ps --filter name=^medulla- | head; echo "(none)"` printed "(none)" unconditionally
— the echo never depended on the filter — so a host running two panels read as idle and
an install and a workflow refresh went out underneath them. Nothing broke that time.
The check being broken is the defect; the luck that followed is not a defence.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/preflight-idle.sh"


def _run(tmp_path, docker_body=None):
    """Run the check with a fake `docker` first on PATH (or none at all)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if docker_body is not None:
        fake = bin_dir / "docker"
        fake.write_text("#!/usr/bin/env bash\n" + docker_body)
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_busy_is_not_idle(tmp_path):
    res = _run(tmp_path, '''
case "$1" in
  info) exit 0 ;;
  ps) printf 'medulla-round-a\\tUp 6 minutes\\nmedulla-round-b\\tUp 51 minutes\\n' ;;
esac
''')
    assert res.returncode == 1
    assert "medulla-round-a" in res.stderr and "medulla-round-b" in res.stderr


def test_idle_is_idle(tmp_path):
    res = _run(tmp_path, 'case "$1" in info) exit 0 ;; ps) : ;; esac\n')
    assert res.returncode == 0, res.stderr


def test_a_dead_daemon_is_UNKNOWN_not_idle(tmp_path):
    """The razor: a check that cannot ask must not report a pass. "docker is absent" and
    "the daemon is not answering" are opposite facts and get opposite answers."""
    res = _run(tmp_path, 'case "$1" in info) exit 1 ;; esac\n')
    assert res.returncode == 2
    assert "cannot tell" in res.stderr


def test_no_docker_at_all_is_idle(tmp_path):
    """A native host has no container rounds to disturb. This is the one honest pass."""
    res = _run(tmp_path, None)
    assert res.returncode == 0, res.stderr


def test_a_failing_ps_is_UNKNOWN(tmp_path):
    res = _run(tmp_path, 'case "$1" in info) exit 0 ;; ps) exit 3 ;; esac\n')
    assert res.returncode == 2


def test_the_output_never_claims_idle_while_busy(tmp_path):
    """The exact shape of the original defect: a line that prints its verdict regardless
    of what it just measured."""
    res = _run(tmp_path, '''
case "$1" in
  info) exit 0 ;;
  ps) printf 'medulla-x\\tUp 1 minute\\n' ;;
esac
''')
    assert "idle" not in (res.stdout + res.stderr).lower().replace("idle enough", "")


# ── two questions, not one ──────────────────────────────────────────────────
#
# The first version answered only "are containers up" and refused an INSTALL on that
# basis, blocking a release the owner was waiting for — while the rounds it protected
# could not be reached by an install at all. So it grew a mode.
#
# Then the install mode got it wrong the OTHER way, twice, and both times toward saying
# yes. `pgrep -f medulla` matched a lane launcher's argv. Looking under
# $HOME/.medulla/engine/venv/bin matched nothing, because ~/.medulla is a symlink to
# another volume and the running processes are site-packages/…/docker.py — the same venv
# under two names, matched by neither. It answered "safe to install" with four live engine
# processes on the host. A guard that errs toward yes is worse than no guard.

def _run_mode(tmp_path, mode, processes="", link_target=None):
    """Run the check in a mode against a fake process table.

    The fake pgrep FILTERS by the pattern it is given, exactly as the real one does. The
    first version of this fixture printed its lines unconditionally, so every test passed
    whatever pattern the script searched for — including a pattern that matched nothing on
    the real host. A fixture that ignores the argument under test proves nothing, and it
    hid the very defect these tests exist for.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    table = bin_dir / "process-table.txt"
    table.write_text(processes)
    fake = bin_dir / "pgrep"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "pat=\"${@: -1}\"\n"                      # pgrep -fl <pattern>
        f'out=$(grep -F -- "$pat" "{table}" || true)\n'
        'test -n "$out" || exit 1\n'
        'printf "%s\\n" "$out"\n')
    fake.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    target = link_target or (tmp_path / "engine" / "venv" / "bin" / "medulla")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "medulla").symlink_to(target)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)}
    return subprocess.run(["bash", str(SCRIPT), mode],
                          capture_output=True, text=True, env=env)


def test_install_refuses_while_an_engine_runs_on_the_host(tmp_path):
    """The case that nearly cost four live lane rounds their code mid-run."""
    venv = tmp_path / "engine" / "venv"
    res = _run_mode(tmp_path, "install", processes=(
        f"14798 /usr/bin/python {venv}/lib/python3.14/site-packages/"
        f"medulla/scripts/docker.py --cwd-ro -w lane --var x=1\n"))
    assert res.returncode == 1, res.stdout + res.stderr
    assert "14798" in res.stderr and "docker.py" in res.stderr


def test_install_is_allowed_when_no_engine_runs(tmp_path):
    res = _run_mode(tmp_path, "install", processes="")
    assert res.returncode == 0, res.stderr
    assert "safe to install" in res.stdout


def test_install_ignores_processes_that_merely_mention_medulla(tmp_path):
    """A lane launcher carries --var EQUILL_BRIDGE=/tmp/medulla-bridge/… in its argv.
    Matching the word reported engines that did not exist."""
    res = _run_mode(tmp_path, "install", processes=(
        "501 node worker.mjs --var EQUILL_BRIDGE=/tmp/medulla-bridge/equill\n"))
    assert res.returncode == 0, res.stderr


def test_install_resolves_a_symlinked_engine_home(tmp_path):
    """~/.medulla is a symlink to another volume on this host, so the entry point and the
    running processes spell the same venv differently. Matching one spelling found
    nothing and reported safe."""
    real = tmp_path / "elsewhere" / "engine" / "venv" / "bin" / "medulla"
    res = _run_mode(tmp_path, "install", link_target=real, processes=(
        f"900 /usr/bin/python {real.parent.parent}/lib/python3.14/"
        f"site-packages/medulla/scripts/docker.py -w lane\n"))
    assert res.returncode == 1, "a resolved path must still match"


def test_refresh_mode_is_unchanged_and_still_asks_about_containers(tmp_path):
    """A refresh rewrites the definition containers have MOUNTED, so a live round reads
    the new scripts mid-run. That question is the strict one and must stay strict."""
    res = _run_mode(tmp_path, "refresh", processes="")
    # docker fake exits 0 for info; ps output empty -> idle
    assert res.returncode in (0, 2), res.stderr


def test_no_argument_defaults_to_the_strict_question(tmp_path):
    """An operator who types the bare name gets the cautious answer, not the permissive
    one. Defaulting to `install` would make the dangerous mode the easy one to reach."""
    body = (SCRIPT).read_text()
    assert 'MODE="${1:-refresh}"' in body


def test_the_narrowing_happens_twice_and_the_second_one_is_the_real_filter():
    """Two gates, and only one direction of error matters.

    pgrep narrows by the resolved venv path; then sed keeps only lines that are actually
    site-packages/medulla/<script>. Widening the pgrep pattern is harmless because sed
    still drops a lane launcher's /tmp/medulla-bridge argv — verified by mutation: a
    pattern of bare "medulla" changes no test. NARROWING it is what kills: a pattern that
    misses the live processes reports "safe to install", which is the failure that nearly
    replaced the venv under four running lanes.

    So the tests defend the dangerous direction and stay quiet about the safe one. This
    records that asymmetry rather than leaving the next reader to rediscover it.
    """
    body = SCRIPT.read_text()
    assert "site-packages/medulla/" in body, "the structural filter must stay"
    assert 'pgrep -fl "$engine_root"' in body, "and it must be fed the resolved path"
