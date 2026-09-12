"""Is MY round alive — asked of a container that may not carry my round's name.

A running panel was called `medulla-ab862829` while its run directory was
`2026-09-10_21-43-55-510c31cd`: the directory name arrived from outside and was used,
the container name fell back to random hex. The observer asked only about the name,
found nothing, and reported the round dead at fourteen minutes old — with a
recommendation to remove a zombie. Naming by run directory arrived in 4.72.0 and the
labels in 4.75.0, and two installations of medulla coexist on this machine (4.76.4 in
PATH, 4.56.2 under pipx), so the old form is not a thing of the past here.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/spar-run.sh"

FAKE_DOCKER = """#!/usr/bin/env python3
# A stand-in for docker, driven by three files: which containers are up (by the
# run-directory name they are NAMED after), which carry the run_dir_name label, and
# what each one's environment says.
import os, sys

argv = sys.argv[1:]
names = [l for l in open(os.environ["FAKE_NAMES"]).read().split() if l]
labels = [l for l in open(os.environ["FAKE_LABELS"]).read().split() if l]
env_rows = [l.split() for l in open(os.environ["FAKE_ENV"]).read().splitlines() if l.strip()]

if not argv or argv[0] == "info":
    sys.exit(0)

if argv[0] == "ps":
    filters = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter" and i + 1 < len(argv)]
    for f in filters:
        if f.startswith("name=^medulla-") and f.endswith("$"):
            want = f[len("name=^medulla-"):-1]
            if want in names:
                print("id-" + want)
            sys.exit(0)
        if f.startswith("label=medulla.run_dir_name="):
            if f.split("=", 2)[2] in labels:
                print("id-label")
            sys.exit(0)
    # bare listing of every medulla container
    for n in names:
        print("id-" + n)
    sys.exit(0)

if argv[0] == "inspect":
    cid = argv[1][3:]              # strip the "id-" prefix
    for row in env_rows:
        if row[0] == cid:
            print("MEDULLA_RUN_DIR_NAME=" + row[1])
    sys.exit(0)
sys.exit(0)
"""


def alive_count(tmp_path, run_dir, names=(), labels=(), envs=()):
    """Run the REAL alive_count from spar-run.sh against a scripted docker."""
    fn = subprocess.run(
        ["sed", "-n", "/^alive_count()/,/^}/p", str(LAUNCHER)],
        capture_output=True, text=True, check=True).stdout
    assert "docker ps" in fn, "did not extract the function"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    for name, rows in (("names", names), ("labels", labels), ("env", envs)):
        (tmp_path / name).write_text("\n".join(rows) + ("\n" if rows else ""))
    script = fn + f'\nalive_count "{run_dir}"\n'
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                         env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin",
                              "FAKE_NAMES": str(tmp_path / "names"),
                              "FAKE_LABELS": str(tmp_path / "labels"),
                              "FAKE_ENV": str(tmp_path / "env")})
    return res.stdout.strip().splitlines()[-1] if res.stdout.strip() else res.stderr


RUN = "/box/2026-09-10_21-43-55-510c31cd"
NAME = "2026-09-10_21-43-55-510c31cd"


def test_a_container_named_after_the_run_is_found(tmp_path):
    assert alive_count(tmp_path, RUN, names=[NAME]) == "1"


def test_a_container_with_the_LABEL_is_found_though_its_name_differs(tmp_path):
    """4.75.0 put the run directory in a label precisely so the name stops mattering."""
    assert alive_count(tmp_path, RUN, names=["ab862829"], labels=[NAME]) == "1"


def test_a_container_that_only_its_ENVIRONMENT_identifies_is_found(tmp_path):
    """The p6B case: no matching name, no labels — an older host side launched it.
    The environment still names the run directory, and the round was alive."""
    assert alive_count(tmp_path, RUN, names=["ab862829"], envs=[f"ab862829 {NAME}"]) == "1"


def test_a_dead_round_is_still_dead(tmp_path):
    """The whole point of the check survives: nothing matching means nothing running,
    and a genuinely dead round must not be kept alive by a broader search."""
    assert alive_count(tmp_path, RUN, names=["someone-elses-round"],
                       envs=["someone-elses-round other-run-dir"]) == "0"


def test_a_SIBLING_round_in_the_same_box_is_not_mine(tmp_path):
    """Why the last resort reads the environment and not the mount: the BOX is what
    gets mounted, one box holds many rounds, and matching on it would report this
    round alive because a neighbour is. That is the failure the name-based lookup was
    introduced to fix, and it must not come back through the new door."""
    assert alive_count(tmp_path, RUN, names=["2026-09-10_22-00-00-other"],
                       envs=["2026-09-10_22-00-00-other 2026-09-10_22-00-00-other"]) == "0"


# ── a failure to observe is not a death ─────────────────────────────────────────

BROKEN_DOCKER = """#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ["info"]:
    sys.exit(0)                       # the daemon answers hello...
sys.stderr.write("Cannot connect to the Docker daemon\\n")
sys.exit(1)                           # ...and then dies under the actual question
"""


def _run_alive_count(tmp_path, docker_body, run_dir=RUN):
    fn = subprocess.run(["sed", "-n", "/^alive_count()/,/^}/p", str(LAUNCHER)],
                        capture_output=True, text=True, check=True).stdout
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(docker_body)
    (bin_dir / "docker").chmod(0o755)
    for name in ("names", "labels", "env"):
        (tmp_path / name).write_text("")
    res = subprocess.run(["bash", "-c", fn + f'\nalive_count "{run_dir}"; echo "rc=$?"'],
                         capture_output=True, text=True, check=False,
                         env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin",
                              "FAKE_NAMES": str(tmp_path / "names"),
                              "FAKE_LABELS": str(tmp_path / "labels"),
                              "FAKE_ENV": str(tmp_path / "env")})
    return res.stdout


def test_a_docker_that_cannot_answer_does_not_report_absence(tmp_path):
    """Every query here can fail — a daemon that went away, a socket that timed out —
    and a failed query returns no rows, which counts as zero, which reads as "dead".
    That is the fail-open shape this repository has already paid for twice. Not
    knowing must be its own answer: rc 2, never a confident zero."""
    out = _run_alive_count(tmp_path, BROKEN_DOCKER)
    assert "rc=2" in out, out
    assert "\n0" not in out, "a broken query produced a confident zero"


def test_a_confirmed_absence_is_still_zero(tmp_path):
    """The other half, or the fix would make every dead round immortal: docker that
    answers normally with no matches must still say zero, with rc 0."""
    out = _run_alive_count(tmp_path, FAKE_DOCKER)
    assert "rc=0" in out and out.splitlines()[0].strip() == "0", out


def test_wait_keeps_waiting_when_it_cannot_ask(tmp_path):
    """End to end: the launcher must not declare a zombie because docker blinked."""
    run = tmp_path / "run"
    (run / "artifacts").mkdir(parents=True)
    (run / "journal.jsonl").write_text('{"node": "panel", "signal": null, "next": "panel"}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(BROKEN_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "20"],
                         capture_output=True, text=True, timeout=120, check=False,
                         env={**os.environ, "PATH": f"{bin_dir}:/bin:/usr/bin",
                              "SPAR_STARTUP_GRACE_S": "0"})
    assert "no medulla container is running" not in res.stderr, res.stderr
    assert "could not ask docker" in res.stderr, res.stderr


UNREACHABLE_DAEMON = """#!/usr/bin/env python3
import sys
sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\\n")
sys.exit(1)                           # even `info` fails: installed, not answering
"""


def test_an_unreachable_daemon_is_unknown_not_native(tmp_path):
    """`docker info` failing means one of two different things: docker is not
    installed (this round is native, and counting host processes is right), or the
    daemon is not answering (we do not know, and the container may be running). The
    first version of this fix folded both into the native branch, so an unreachable
    daemon became a process count of zero — the same fail-open shape, one level up
    from where I had just fixed it."""
    out = _run_alive_count(tmp_path, UNREACHABLE_DAEMON)
    assert "rc=2" in out, out
    assert out.splitlines()[0].strip() != "0", "an unreachable daemon produced a count"


def test_a_machine_without_docker_still_counts_processes(tmp_path):
    """And the native path must survive: where docker is genuinely absent, the answer
    comes from host processes, with rc 0 — not 'unknown' forever."""
    fn = subprocess.run(["sed", "-n", "/^alive_count()/,/^}/p", str(LAUNCHER)],
                        capture_output=True, text=True, check=True).stdout
    bin_dir = tmp_path / "bin"          # a PATH with no docker in it at all
    bin_dir.mkdir(exist_ok=True)
    res = subprocess.run(["bash", "-c", fn + f'\nalive_count "{RUN}"; echo "rc=$?"'],
                         capture_output=True, text=True, check=False,
                         env={"PATH": f"{bin_dir}:/bin:/usr/bin"})
    assert "rc=0" in res.stdout, res.stdout + res.stderr
