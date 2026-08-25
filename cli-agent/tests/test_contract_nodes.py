"""The contract: what a node may declare, and what the graph must hold together.

An action is shell XOR agent, a fallback belongs to an agent, and a dunder is the
engine's word — a workflow cannot mint one.
"""
import pytest
from medulla.v2.contract import load_workflow
from medulla.v2.errors import EngineCrash

from conftest import MINIMAL, load_err, write

def test_fallback_forbidden_on_shell(tmp_path):
    text = MINIMAL.replace(
        'shell: "true"', 'shell: "true"\n    fallback: {agent: codex}')
    assert "meaningless for shell" in load_err(tmp_path, text)


def test_nested_fallback_forbidden(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    agent: codex
    prompt: "p"
    fallback:
      agent: opus
      fallback: {agent: sonnet}
    on_signal: {ok: __exit_ok__}
"""
    assert "no fallback" in load_err(tmp_path, text)


def test_defaults_unknown_key(tmp_path):
    assert "unknown keys" in load_err(tmp_path, MINIMAL + "\ndefaults: {retries: 2}\n")


def test_defaults_fallback_must_be_agent(tmp_path):
    assert "agent action" in load_err(
        tmp_path, MINIMAL + '\ndefaults:\n  fallback: {shell: "true"}\n')


def test_reserved_var_names(tmp_path):
    assert "reserved" in load_err(tmp_path, MINIMAL + "\nvars: {PATH: /tmp}\n")
    assert "reserved" in load_err(tmp_path, MINIMAL + "\nvars: {MEDULLA_X: y}\n")


def test_defaults_self_edge_rejected(tmp_path):
    text = """
version: "2"
start: a
defaults:
  on_signal: {__failed__: notify}
nodes:
  a:
    shell: "true"
    on_signal: {ok: __exit_ok__}
  notify:
    shell: "true"
    on_signal: {ok: __exit_fail__}
"""
    assert "self-loop via defaults" in load_err(tmp_path, text)


def test_defaults_self_edge_ok_when_overridden(tmp_path):
    text = """
version: "2"
start: a
defaults:
  on_signal: {__failed__: notify}
nodes:
  a:
    shell: "true"
    on_signal: {ok: __exit_ok__}
  notify:
    shell: "true"
    on_signal: {ok: __exit_fail__, __failed__: __exit_fail__}
"""
    p = load_workflow(write(tmp_path, text))
    assert p.defaults.on_signal["__failed__"] == "notify"


def test_empty_shell_rejected(tmp_path):
    assert "non-empty" in load_err(tmp_path, MINIMAL.replace('shell: "true"', 'shell: ""'))


def test_defaults_unknown_dunder_rejected(tmp_path):
    assert "unknown engine signal" in load_err(
        tmp_path, MINIMAL + "\ndefaults:\n  on_signal: {__bogus__: __exit_ok__}\n")


def test_inputs_format_reserved(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: {shell: "echo x", format: lines}
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    assert "reserved" in load_err(tmp_path, text)


def test_docker_block_valid_loads(tmp_path):
    p = load_workflow(write(tmp_path, MINIMAL + "\ndocker: {shadow: [secrets, .git]}\n"))
    assert p.start == "a"                       # block accepted, engine ignores it


def test_docker_block_unknown_key(tmp_path):
    assert "unknown fields" in load_err(
        tmp_path, MINIMAL + "\ndocker: {network: none}\n")


def test_docker_shadow_escape_rejected(tmp_path):
    assert "workspace" in load_err(tmp_path, MINIMAL + "\ndocker: {shadow: [/etc]}\n")
    assert "workspace" in load_err(tmp_path, MINIMAL + '\ndocker: {shadow: ["../x"]}\n')
    assert "workspace" in load_err(tmp_path, MINIMAL + '\ndocker: {shadow: ["."]}\n')


def test_normalization(tmp_path):
    text = """
version: "2"
start: a
timeout: 0
nodes:
  a:
    agent: codex
    prompt: "p"
    on_signal: {ok: __exit_ok__}
  b:
    inputs: {shell: "echo x", timeout: 5}
    max_parallel: all
    min_success: 2
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    p = load_workflow(write(tmp_path, text))
    assert p.timeout is None                       # 0 = unlimited
    assert p.nodes["a"].action.agent.harness == "codex"   # scalar shortcut
    pool = p.nodes["b"].pool
    assert pool.max_parallel is None and pool.min_success == 2
    assert pool.inputs.shell == "echo x" and pool.inputs.shell_timeout == 5
