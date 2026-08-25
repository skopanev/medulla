"""Pools: one input failing must not take the round with it.

A render error, an exhausted retry, a post hook that refuses — each belongs to its own
input. min_success exists precisely so a panel survives a broken model.
"""
import json

from medulla.v2.engine import run_workflow

from conftest import fake_script, read_run, write_workflow as setup






def read_manifest(run, step_name):
    mp = run / "steps" / step_name / "manifest.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []





# ── per-input failure isolation ──────────────────────────────────────────────

def test_render_error_fails_one_input_not_the_run(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [{slug: good, cmd: "true"}, {slug: broken}]
    max_parallel: 2
    min_success: 1
    shell: 'echo "{{input.cmd}}"'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    rows = {r["input"]["slug"]: r for r in read_manifest(run, "001-p")}
    assert rows["good"]["ok"] is True
    assert rows["broken"]["ok"] is False and rows["broken"]["reason"] == "render"
    assert rows["broken"]["attempts"] == 0


def test_per_input_retry_and_reason_classes(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [flaky, dead]
    min_success: 2
    shell: |
      case "$MEDULLA_INPUT" in
        flaky) [ -f "m-$MEDULLA_INPUT_INDEX" ] && exit 0 || {{ touch "m-$MEDULLA_INPUT_INDEX"; exit 1; }} ;;
        dead) exit 1 ;;
      esac
    max_attempts: 2
    on_signal: {__done__: __exit_ok__}
"""
    # NB: yaml literal — escape braces via block scalar instead
    text = text.replace("{{ touch", "{ touch").replace("exit 1; }}", "exit 1; }")
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    run, outcome, _ = read_run(path.parent)
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["flaky"]["ok"] is True and rows["flaky"]["attempts"] == 2
    assert rows["dead"]["ok"] is False and rows["dead"]["reason"] == "rc"
    assert "1/2 inputs ok" in outcome["error"]["message"]


def test_pool_post_is_truth_channel(tmp_path):
    # bodies exit 0 but only one writes its artifact; post decides
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [writer, liar]
    max_parallel: 2
    min_success: 1
    shell: '[ "$MEDULLA_INPUT" = writer ] && echo art > "art-$MEDULLA_INPUT_INDEX.txt" || true'
    post: 'test -s "art-$MEDULLA_INPUT_INDEX.txt"'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["writer"]["ok"] is True
    assert rows["liar"]["ok"] is False and rows["liar"]["reason"] == "post"


def test_pre_guard_skips_input_as_ok(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [cached, fresh]
    pre: '[ "$MEDULLA_INPUT" = cached ] && echo "<signal:done_before>skip</signal:done_before>" || true'
    shell: 'echo "$MEDULLA_INPUT" >> worked.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "worked.txt").read_text().splitlines() == ["fresh"]
    run, _, _ = read_run(path.parent)
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["cached"]["ok"] is True and rows["cached"]["reason"] == "guard"
    assert rows["cached"]["attempts"] == 0 and rows["cached"]["signal"] == "done_before"


