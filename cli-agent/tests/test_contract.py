"""Validator matrix — every rejection is E_VALIDATION at load time."""
import pytest
from medulla.v2.contract import load_workflow
from medulla.v2.errors import EngineCrash

MINIMAL = """
version: "2"
start: a
nodes:
  a:
    shell: "true"
    on_signal: {ok: __exit_ok__}
"""


def write(tmp_path, text):
    p = tmp_path / "workflow.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def load_err(tmp_path, text) -> str:
    with pytest.raises(EngineCrash) as exc:
        load_workflow(write(tmp_path, text))
    assert exc.value.code == "E_VALIDATION"
    return exc.value.message


def test_minimal_loads(tmp_path):
    p = load_workflow(write(tmp_path, MINIMAL))
    assert p.start == "a" and p.nodes["a"].action.kind == "shell"


def test_version_required(tmp_path):
    msg = load_err(tmp_path, MINIMAL.replace('version: "2"\n', ""))
    assert "v1" in msg and "Migrating" in msg


def test_duplicate_yaml_keys_rejected(tmp_path):
    msg = load_err(tmp_path, MINIMAL + "\nvars: {A: 1}\nvars: {A: 2}\n")
    assert "duplicate" in msg


def test_shell_and_agent_exclusive(tmp_path):
    text = MINIMAL.replace('shell: "true"', 'shell: "true"\n    agent: codex')
    assert "exactly one" in load_err(tmp_path, text)


def test_prompt_forbidden_on_shell(tmp_path):
    text = MINIMAL.replace('shell: "true"', 'shell: "true"\n    prompt: "x"')
    assert "prompt" in load_err(tmp_path, text)


def test_unknown_target(tmp_path):
    assert "unknown target" in load_err(tmp_path, MINIMAL.replace("__exit_ok__", "nope"))


def test_boolean_trap_node_name_quoted(tmp_path):
    text = MINIMAL + """
  "on":
    shell: "true"
    on_signal: {ok: __exit_ok__}
"""
    assert "boolean" in load_err(tmp_path, text)


def test_boolean_trap_node_name_unquoted(tmp_path):
    text = MINIMAL + """
  on:
    shell: "true"
    on_signal: {ok: __exit_ok__}
"""
    assert "not a string" in load_err(tmp_path, text)


def test_dunder_node_name_forbidden(tmp_path):
    text = MINIMAL + """
  __helper__:
    shell: "true"
    on_signal: {ok: __exit_ok__}
"""
    assert "engine namespace" in load_err(tmp_path, text)


def test_max_parallel_requires_inputs(tmp_path):
    text = MINIMAL.replace('shell: "true"', 'shell: "true"\n    max_parallel: 3')
    assert "requires inputs" in load_err(tmp_path, text)


def test_pool_must_route_done(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x, y]
    shell: "true"
    on_signal: {__failed__: __exit_fail__}
"""
    assert "__done__" in load_err(tmp_path, text)


def test_pool_bare_signal_key_forbidden(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x, y]
    shell: "true"
    on_signal: {__done__: __exit_ok__, ready: __exit_ok__}
"""
    assert "law of layers" in load_err(tmp_path, text)


def test_bare_string_inputs_rejected(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: "ls *.txt"
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    assert "ambiguous" in load_err(tmp_path, text)


def test_mixed_input_kinds_rejected(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x, {slug: y}]
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    assert "one kind" in load_err(tmp_path, text)


def test_array_inputs_rejected(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [[1, 2]]
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    assert "forbidden" in load_err(tmp_path, text)


def test_ignore_exit_code_forbidden_in_pools(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x]
    shell: "true"
    ignore_exit_code: true
    on_signal: {__done__: __exit_ok__}
"""
    assert "min_success" in load_err(tmp_path, text)


def test_channel_signal_not_routable(tmp_path):
    assert "channel" in load_err(
        tmp_path, MINIMAL.replace("{ok: __exit_ok__}", "{var: __exit_ok__}"))


def test_unknown_engine_dunder_signal(tmp_path):
    assert "unknown engine signal" in load_err(
        tmp_path, MINIMAL.replace("{ok: __exit_ok__}", "{__boom__: __exit_ok__}"))


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


# ── sandbox: enum validated at load, like every other agent field ────────────

AGENT = """
version: "2"
start: a
nodes:
  a:
    agent: {harness: codex%s}
    prompt: "p"
    on_signal: {ok: __exit_ok__}
"""


def test_sandbox_typo_rejected_at_load(tmp_path):
    # the whole point: a literal typo fails --validate, not mid-run
    msg = load_err(tmp_path, AGENT % ", sandbox: readonly")
    assert "sandbox" in msg and "read-only" in msg


def test_sandbox_valid_levels_load(tmp_path):
    for level in ("read-only", "danger"):
        p = load_workflow(write(tmp_path, AGENT % f", sandbox: {level}"))
        assert p.nodes["a"].action.agent.sandbox == level


def test_sandbox_templated_value_deferred(tmp_path):
    # a template can only resolve after render — load must not reject it (it
    # defers to the build-time _read_only check, like harness/model/effort)
    p = load_workflow(write(tmp_path, AGENT % ', sandbox: "{{var:SB}}"'))
    assert p.nodes["a"].action.agent.sandbox == "{{var:SB}}"


def test_harness_bin_is_gone_and_says_so(tmp_path):
    """`harness_bin:` used to let a workflow name another executable for a harness
    (a credential-refreshing wrapper). The wrapper belongs to the machine, not to the
    workflow — the container installs it as the harness's own name — so the block was
    removed. A yaml still carrying it must FAIL, not be silently ignored: silence
    would run the plain binary and quietly bypass the broker the block was there for.
    """
    from medulla.v2.contract import load_workflow
    from medulla.v2.errors import EngineCrash
    p = tmp_path / "workflow.yaml"
    p.write_text("""version: "2"
start: a
harness_bin: {codex: cx}
nodes:
  a: {shell: "true", on_signal: {ok: __exit_ok__}}
""", encoding="utf-8")
    try:
        load_workflow(p)
    except EngineCrash as exc:
        assert "harness_bin" in str(exc)
    else:
        raise AssertionError("harness_bin accepted after removal")


def test_validation_errors_name_the_file(tmp_path):
    from medulla.v2.contract import load_workflow
    from medulla.v2.errors import EngineCrash
    p = tmp_path / "workflow.yaml"
    p.write_text("just a string, not a mapping\n", encoding="utf-8")
    try:
        load_workflow(p)
    except EngineCrash as e:
        assert str(p) in e.message, "the error must name the offending file"
        return
    raise AssertionError("expected a validation crash")


def test_empty_workflow_says_so(tmp_path):
    # The message that cost a day: "workflow must be a YAML mapping" pointed nowhere.
    from medulla.v2.contract import load_workflow
    from medulla.v2.errors import EngineCrash
    p = tmp_path / "workflow.yaml"
    p.write_text("", encoding="utf-8")
    try:
        load_workflow(p)
    except EngineCrash as e:
        assert "EMPTY" in e.message and str(p) in e.message
        return
    raise AssertionError("expected a validation crash")
