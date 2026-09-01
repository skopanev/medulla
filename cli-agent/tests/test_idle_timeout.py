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
    workflow = load_workflow(write(tmp_path, AGENT % ", idle_timeout: 1800"))
    assert workflow.nodes["worker"].action.agent.idle_timeout == 1800


@pytest.mark.parametrize("value", ["slow", "0", "-2", "true"])
def test_invalid_idle_timeout_fails_at_load_and_names_node(tmp_path, value):
    message = load_err(tmp_path, AGENT % f", idle_timeout: {value}")
    assert "worker" in message
    assert "agent.idle_timeout" in message


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


def test_the_spar_panel_declares_its_own_threshold():
    """Live: qwen at xhigh printed one line and the 900s default killed it, twice, in a
    round the other four finished — a reasoning panelist is silent between writes for
    longer than an ordinary agent. The panel says so itself instead of the default
    moving for every workflow that inherits it."""
    from pathlib import Path

    import yaml as pyyaml
    yml = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"
    panel = pyyaml.safe_load(yml.read_text())["nodes"]["panel"]
    assert panel["agent"]["idle_timeout"] == 1800
    assert procrun.IDLE_OUTPUT_S == 900, "the engine default is not the panel's business"
