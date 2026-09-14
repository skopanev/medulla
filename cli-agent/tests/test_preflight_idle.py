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
