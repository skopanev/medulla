"""Pools: what one worker must not see of another.

Parallel inputs share an engine but not a state — prompts, vars and pre-hook effects
stay in their own lane, or a panel of five becomes five copies of one.
"""
import json

from medulla.v2.engine import run_workflow

from conftest import fake_script, read_run, write_workflow as setup






def read_manifest(run, step_name):
    mp = run / "steps" / step_name / "manifest.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []





# ── isolation, fold, env ─────────────────────────────────────────────────────

def test_parallel_prompt_isolation(tmp_path):
    # each agent input must read ITS OWN rendered prompt (the corruption trap)
    script = fake_script(tmp_path, "reader.sh",
                         'grep -q "for $MEDULLA_INPUT" "$1" || exit 9\n')
    text = f"""
version: "2"
start: p
nodes:
  p:
    inputs: [aa, bb, cc, dd]
    max_parallel: 4
    agent: {{harness: fake, model: "{script}"}}
    prompt: "task for {{{{input}}}}"
    on_signal: {{__done__: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    p1 = (run / "steps" / "001-p" / "input-0001" / "prompt.md").read_text()
    p4 = (run / "steps" / "001-p" / "input-0004" / "prompt.md").read_text()
    assert p1.startswith("task for aa") and p4.startswith("task for dd")


def test_fold_sequential_accumulator(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [a, b, c]
    shell: 'echo "<signal:var key=TOTAL>$(( ${TOTAL:-0} + 1 ))</signal:var>"'
    on_signal: {__done__: report}
  report:
    shell: '[ "$TOTAL" = "3" ] && echo "<signal:ok>3</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


def test_parallel_vars_go_to_manifest_not_state(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [x, y]
    max_parallel: 2
    shell: 'echo "<signal:var key=RACE>$MEDULLA_INPUT</signal:var>"'
    on_signal: {__done__: check}
  check:
    shell: '[ -z "${RACE:-}" ] && echo "<signal:ok>clean</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "RACE" not in (run / "vars.yaml").read_text()
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["x"]["vars"] == {"RACE": "x"} and rows["y"]["vars"] == {"RACE": "y"}


