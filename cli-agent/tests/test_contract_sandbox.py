"""sandbox: an enum validated at load, like every other agent field.

A typo must fail --validate rather than surface as full access three hours into a run.
"""
import pytest
from conftest import load_err, write
from medulla.v2.contract import load_workflow
from medulla.v2.errors import EngineCrash

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
