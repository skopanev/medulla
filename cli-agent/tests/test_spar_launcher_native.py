"""The panel must survive a dead container runtime.

A colima content-store fault took Docker down and spar went with it — not because
the panel needs a container, but because the launcher demanded one and the prepare
guard checked a path that only exists inside one. Every harness runs on the host.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml as pyyaml

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "workflows/spar/scripts/spar-run.sh"
WORKFLOW = ROOT / "workflows/spar/workflow.yaml"


def _prepare(tmp_path, question="q"):
    """Run the prepare node's shell body in tmp_path, as medulla would."""
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env={**os.environ, "QUESTION": question},
                         check=False)
    return res.returncode, res.stdout + res.stderr


def test_prepare_accepts_a_non_empty_tree_outside_a_container(tmp_path):
    """The guard used to check /workspace, which exists only inside the container.
    Natively it failed on its own check and the run died before any panelist."""
    (tmp_path / "somefile.py").write_text("x = 1\n")
    rc, out = _prepare(tmp_path)
    assert rc == 0, out
    assert "ready" in out


def test_prepare_still_refuses_an_empty_tree(tmp_path):
    """The guard exists because a failed --mount once produced a verdict from five
    agents reasoning about nothing. Re-pointing it must not disarm it."""
    rc, out = _prepare(tmp_path)
    assert rc != 0
    assert "empty" in out


def test_prepare_ignores_bookkeeping_directories(tmp_path):
    """`.medulla`, `.git` and `box` are ours, not the tree under review."""
    for d in (".medulla", ".git", "box"):
        (tmp_path / d).mkdir()
    rc, out = _prepare(tmp_path)
    assert rc != 0, "a tree holding only our own directories is still empty"


def test_prepare_requires_a_question(tmp_path):
    (tmp_path / "somefile.py").write_text("x = 1\n")
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    env = {k: v for k, v in os.environ.items() if k != "QUESTION"}
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env=env, check=False)
    assert res.returncode != 0
    assert "question" in (res.stdout + res.stderr)


def test_launcher_does_not_hard_require_docker():
    """`command -v docker || die` made a container-runtime outage fatal to a panel
    that does not need a container."""
    src = LAUNCHER.read_text()
    assert 'die "docker is not installed' not in src
    assert 'die "the docker daemon is not responding' not in src
    assert "DOCKER_OK" in src, "the launcher must decide a mode, not assume one"


def test_launcher_still_prefers_docker_when_healthy():
    src = LAUNCHER.read_text()
    assert 'if [ "$DOCKER_OK" = yes ]; then' in src
    assert "--docker --cwd-ro" in src, "the container path keeps the read-only tree"


def test_launcher_announces_the_reduced_native_panel():
    """A thinner panel is a fact the caller must be told, not discover."""
    src = LAUNCHER.read_text()
    assert "3 of 5" in src
    assert "NOT mounted read-only" in src


def test_launcher_liveness_check_works_without_docker():
    """The wait watchdog counted containers; natively that reports every healthy
    panel as dead sixty seconds in."""
    src = LAUNCHER.read_text()
    assert "alive_count()" in src
    assert "docker ps -q --filter 'name=^medulla-'" not in src.split("alive_count() {")[0]


def test_the_workflow_ships_exactly_one_launcher():
    """`medulla launch spar start ...` is the owner's command, and medulla picks the
    launcher by the executable bit — several executables and it refuses to guess. A
    helper script added to scripts/ with a stray chmod +x took that command out: it
    started answering "workflow 'spar' ships several launchers — name one". The helpers
    beside it (collect_verdict.py, quota_precheck.sh) are run BY the workflow and are
    not executable; only spar-run.sh is."""
    import os
    from pathlib import Path
    scripts = Path(__file__).resolve().parent.parent / "workflows/spar/scripts"
    executable = sorted(p.name for p in scripts.iterdir()
                        if p.is_file() and os.access(p, os.X_OK))
    assert executable == ["spar-run.sh"], executable


def _start(question, *extra):
    """Run `start` with medulla absent from PATH: preflight dies right after the
    litter guard, so the guard's verdict is observable without a real panel — and
    without the docker image build that a live start triggers."""
    env = {**os.environ, "PATH": "/bin:/usr/bin"}   # bash and git yes, medulla no
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "start", str(question), *extra],
                         capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return res.returncode, res.stdout + res.stderr


def test_start_refuses_a_question_file_left_in_the_tree(tmp_path):
    """`start` copies the question into the run's box, so the caller's file is dead
    weight the moment the panel begins — and left in a repo it stays for months. One
    workspace collected 71 stray panel-question/panel-handout/BRIEF files, 292 MB, in
    its root. The skill asks for $TMPDIR; this is what makes it true."""
    litter = ROOT / "test-question-litter.md"
    litter.write_text("brief\n")
    try:
        rc, out = _start(litter)
    finally:
        litter.unlink()
    assert rc != 0, out
    assert "mktemp" in out, "the refusal must say where the brief belongs"
    assert "--allow-outside-tmp" in out, "a human with a hand-written brief needs the door"


def test_start_accepts_a_question_under_tmp(tmp_path):
    """pytest's tmp_path lives under /var/folders on macOS and /tmp on Linux — both
    are canonical temp roots. A guard that rejected them would block the very place
    the skill now tells every caller to write."""
    q = tmp_path / "q.md"
    q.write_text("brief\n")
    rc, out = _start(q)
    assert "refusing to litter" not in out, out
    assert "medulla is not installed" in out, "the guard must pass before preflight"


def test_the_override_flag_is_not_passed_through_to_medulla():
    """--allow-outside-tmp is the launcher's own; medulla would reject it as unknown."""
    src = LAUNCHER.read_text()
    assert "--allow-outside-tmp" in src
    body = src.split("cmd_start() {")[1]
    assert "rotate each argument exactly once" in body, "the flag must be stripped, not forwarded"


def test_the_wait_outlives_the_workflow_deadline(tmp_path):
    """How long to wait is not an independent opinion — it is the workflow's own
    deadline plus room to conclude. They had drifted: the wait gave up at 2700s while
    the run was entitled to 3600, so a lane read "timed out, no verdict" fifteen
    minutes before the engine would have stopped anything. Measured live by finik-pm:
    container up 56 minutes, wait expired at 45, three of four delivered and
    min_success was already met — a finished round whose last worker was still inside
    its budget, pinning a lane slot with no work left in it."""
    wf = tmp_path / ".medulla" / "workflows" / "spar"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text('version: "2"\ntimeout: 1234\nstart: n\n')
    body = "\n".join([
        'WORKFLOW=spar', 'FALLBACK_TIMEOUT=3900',
        _extract(LAUNCHER.read_text(), "default_timeout"),
        'default_timeout',
    ])
    out = subprocess.run(["/bin/bash", "-c", body], cwd=tmp_path,
                         capture_output=True, text=True, check=False).stdout.strip()
    assert out == "1534", f"deadline+grace expected, got {out!r}"


def _extract(src, fname):
    start = src.index(f"{fname}() {{")
    end = src.index("\n}", start) + 2
    return src[start:end]


def test_alive_count_asks_about_one_run_not_every_panel():
    """`--filter name=^medulla-` answered for EVERY panel on the machine, so a wait
    sat quietly through its own round's death whenever a stranger's round was up.
    Measured by a lane whose wait hung on a round already dead, and by a round whose
    four panelists were SIGTERMed inside one second — the shape of an external stop,
    not four independent failures.

    Asserted on the source: stubbing `docker` here means stubbing `command -v` too,
    and a test that fakes the shell builtins it depends on proves less than reading
    the filter it is meant to check.
    """
    body = _extract(LAUNCHER.read_text(), "alive_count")
    assert 'name=^medulla-$(basename "$run")' in body, body
    assert 'local run="${1:-}"' in body, "the run must be an argument, not a global"
    assert "docker ps -q --filter 'name=^medulla-'" in body, \
        "the machine-wide form stays as the fallback when no run is named"


def test_a_container_is_named_after_its_run():
    """A random uuid made every panel indistinguishable: a kill by name or image hit
    whoever else was running, and I did exactly that to another team's panel."""
    src = (LAUNCHER.resolve().parent.parent.parent.parent
           / "scripts" / "dockerlib" / "process.py").read_text()
    assert 'container_name = f"medulla-{safe[:100]}"' in src
    assert "if run_dir_name:" in src
    assert 'f"medulla-{uuid.uuid4().hex[:8]}"' in src, "fallback stays for callers with no run dir"
