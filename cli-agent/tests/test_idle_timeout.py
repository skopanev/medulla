"""Agent idle_timeout: workflow contract first, machine fallback second."""
from __future__ import annotations

import importlib

import pytest
from conftest import fake_script, load_err, read_run, write, write_workflow
from medulla.v2 import procrun
from medulla.v2.contract import load_workflow
from medulla.v2.engine import run_workflow

AGENT = """
version: "2"
start: worker
nodes:
  worker:
    agent: {harness: fake, model: worker.sh%s}
    prompt: "work"
    on_signal: {ok: __exit_ok__}
"""


def test_positive_idle_timeout_loads_as_agent_contract(tmp_path):
    workflow = load_workflow(write(tmp_path, AGENT % ", idle_timeout: 900"))
    assert workflow.nodes["worker"].action.agent.idle_timeout == 900


@pytest.mark.parametrize("value", ["slow", "0", "-2", "true"])
def test_invalid_idle_timeout_fails_at_load_and_names_node(tmp_path, value):
    message = load_err(tmp_path, AGENT % f", idle_timeout: {value}")
    assert "worker" in message
    assert "agent.idle_timeout" in message


@pytest.mark.parametrize("idle", [10, 11])
def test_idle_timeout_must_be_less_than_explicit_attempt_timeout(tmp_path, idle):
    yaml = AGENT.replace('prompt: "work"', 'timeout: 10\n    prompt: "work"')
    message = load_err(tmp_path, yaml % f", idle_timeout: {idle}")
    assert f"agent.idle_timeout ({idle}s)" in message
    assert "less than its effective timeout (10s)" in message


def test_idle_timeout_must_be_less_than_inherited_attempt_timeout(tmp_path):
    yaml = AGENT.replace("nodes:", "defaults: {timeout: 5}\nnodes:")
    message = load_err(tmp_path, yaml % ", idle_timeout: 5")
    assert "less than its effective timeout (5s)" in message


def test_idle_timeout_accounts_for_workflow_deadline_clamp(tmp_path):
    yaml = AGENT.replace('start: worker', 'start: worker\ntimeout: 100')
    yaml = yaml.replace('prompt: "work"', 'timeout: 200\n    prompt: "work"')
    message = load_err(tmp_path, yaml % ", idle_timeout: 150")
    assert "agent.idle_timeout (150s)" in message
    assert "effective timeout (100s)" in message


def _silent_agent_workflow(tmp_path, idle_timeout=""):
    script = fake_script(tmp_path, "worker.sh", "echo started; sleep 30\n")
    field = f", idle_timeout: {idle_timeout}" if idle_timeout else ""
    yaml = AGENT.replace("worker.sh", script) % field
    return write_workflow(tmp_path, yaml)


def test_declared_timeout_wins_over_machine_value_and_watchdog_fires(
        tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "IDLE_OUTPUT_S", 30)
    yaml, work = _silent_agent_workflow(tmp_path, idle_timeout="1")

    assert run_workflow(yaml, workdir=work) == 2
    _, _, journal = read_run(yaml.parent)
    assert journal[0]["timed_out"] is True
    assert "silent for 1s" in journal[0]["message"]


def test_explicit_short_idle_timeout_enables_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(procrun, "FIRST_OUTPUT_S", 60)
    started = __import__("time").monotonic()
    res = procrun.run(
        ["bash", "-c", "echo started; sleep 30"], tmp_path, timeout_s=3,
        watch_output=True, idle_timeout_s=1,
    )
    elapsed = __import__("time").monotonic() - started
    assert res.timed_out and "silent for 1s" in res.killed_because
    assert elapsed < 2, f"explicit one-second idle took {elapsed:.3f}s"


def test_undeclared_timeout_uses_environment_then_builtin_default(
        tmp_path, monkeypatch):
    monkeypatch.setenv("MEDULLA_IDLE_OUTPUT_S", "1")
    importlib.reload(procrun)
    try:
        yaml, work = _silent_agent_workflow(tmp_path)
        assert run_workflow(yaml, workdir=work) == 2
        _, _, journal = read_run(yaml.parent)
        assert "silent for 1s" in journal[0]["message"]
    finally:
        monkeypatch.delenv("MEDULLA_IDLE_OUTPUT_S")
        importlib.reload(procrun)
    assert procrun.IDLE_OUTPUT_S == 900


def test_the_spar_panel_does_not_declare_a_threshold_of_its_own():
    """It declared 1800 for one release, on a misread log: the single line qwen produced
    in 1819 seconds was an opencode startup notice, not thinking. It was hung on a 429
    its pre hook failed open on — and a higher threshold only doubles what a hang costs.
    The panel stays on the engine default; the fix belongs in the pre hook."""
    from pathlib import Path

    import yaml as pyyaml
    yml = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"
    panel = pyyaml.safe_load(yml.read_text())["nodes"]["panel"]
    assert "idle_timeout" not in panel["agent"]
    assert procrun.IDLE_OUTPUT_S == 900
