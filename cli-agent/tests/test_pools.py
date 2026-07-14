"""Part 3: pool nodes — materialization, parallel execution, manifest, join, fold."""
import json

from medulla.v2.engine import run_pipeline


def setup(tmp_path, text):
    pdir = tmp_path / "pipe"
    pdir.mkdir()
    (pdir / "pipeline.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    return pdir / "pipeline.yaml", work


def read_run(pdir):
    runs = sorted((pdir / "runs").iterdir())
    run = runs[0]
    outcome = json.loads((run / "outcome.json").read_text())
    jp = run / "journal.jsonl"
    journal = [json.loads(l) for l in jp.read_text().splitlines()] if jp.exists() else []
    return run, outcome, journal


def read_manifest(run, step_name):
    mp = run / "steps" / step_name / "manifest.jsonl"
    return [json.loads(l) for l in mp.read_text().splitlines()] if mp.exists() else []


def fake_script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    return str(p)


# ── basics: silence is ok, manifest rows, join ──────────────────────────────

def test_pool_silent_shell_inputs_are_ok(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [alpha, beta, gamma]
    max_parallel: 3
    shell: 'echo "processed $MEDULLA_INPUT" >> done.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    run, _, journal = read_run(path.parent)
    rows = read_manifest(run, "001-p")
    assert len(rows) == 3 and all(r["ok"] and r["reason"] == "ok" for r in rows)
    assert sorted(r["input"] for r in rows) == ["alpha", "beta", "gamma"]
    assert journal[0]["kind"] == "pool" and journal[0]["inputs_ok"] == 3
    assert len((work / "done.txt").read_text().splitlines()) == 3


def test_pool_silence_never_burns_attempts(tmp_path):
    # pool_mode: an agent body that writes artifacts but emits no signal is OK
    script = fake_script(tmp_path, "worker.sh", 'echo run >> "invocations-$MEDULLA_INPUT_INDEX"\nexit 0\n')
    text = f"""
version: "2"
start: p
nodes:
  p:
    inputs: [x]
    agent: {{harness: fake, model: "{script}"}}
    prompt: "p"
    max_attempts: 3
    on_signal: {{__done__: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    assert (work / "invocations-1").read_text().count("run") == 1   # no silence retries
    run, _, _ = read_run(path.parent)
    assert read_manifest(run, "001-p")[0]["attempts"] == 1


def test_min_success_threshold_done_and_failed(tmp_path):
    body = 'case "$MEDULLA_INPUT" in ok*) exit 0;; *) exit 1;; esac'
    for ms, expected_exit, expected_signal in ((2, 0, "__done__"), (3, 2, "__failed__")):
        base = tmp_path / f"ms{ms}"
        base.mkdir()
        text = f"""
version: "2"
start: p
nodes:
  p:
    inputs: [ok1, ok2, bad]
    max_parallel: 3
    min_success: {ms}
    shell: '{body}'
    on_signal: {{__done__: __exit_ok__}}
"""
        path, work = setup(base, text)
        assert run_pipeline(path, workdir=work) == expected_exit
        _, outcome, journal = read_run(path.parent)
        assert journal[0]["signal"] == expected_signal
        if expected_exit == 2:
            assert "2/3 inputs ok" in outcome["error"]["message"]
            assert "rc x1" in outcome["error"]["message"]


def test_no_short_circuit_all_inputs_run(tmp_path):
    # min_success: 1 satisfied by the first input — the rest must still run
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [a, b, c, d]
    min_success: 1
    shell: 'echo "$MEDULLA_INPUT" >> ran.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    assert len((work / "ran.txt").read_text().splitlines()) == 4


def test_min_success_above_total_is_failed_not_crash(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: [only-one]
    min_success: 5
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 2


# ── inputs: source, sniffing, empty, errors ─────────────────────────────────

def test_source_json_array_object_inputs(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf '[{\\"id\\": \\"T-1\\", \\"title\\": \\"fix a\\"}, {\\"id\\": \\"T-2\\", \\"title\\": \\"fix b\\"}]'"}
    max_parallel: 2
    shell: 'echo "$MEDULLA_INPUT_ID: {{input.title}}" >> out.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    out = sorted((work / "out.txt").read_text().splitlines())
    assert out == ["T-1: fix a", "T-2: fix b"]
    run, _, _ = read_run(path.parent)
    snapshot = json.loads((run / "steps" / "001-p" / "inputs.json").read_text())
    assert snapshot[0]["id"] == "T-1"


def test_source_plain_lines(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "printf 'one\\n\\ntwo\\n'"}
    shell: 'echo "{{input}}" >> got.txt'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    assert (work / "got.txt").read_text().splitlines() == ["one", "two"]


def test_empty_source_routes_empty_with_manifest(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "true"}
    shell: "touch never.txt"
    on_signal: {__done__: __exit_fail__, __empty__: after}
  after:
    shell: |
      wc -l < "$MEDULLA_MANIFEST_P" | tr -d ' ' > count.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    assert not (work / "never.txt").exists()            # bodies never ran
    assert (work / "count.txt").read_text().strip() == "0"   # empty manifest EXISTS


def test_empty_static_list_routes_empty(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: []
    shell: "true"
    on_signal: {__done__: __exit_fail__, __empty__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0


def test_broken_source_is_e_inputs(tmp_path):
    text = """
version: "2"
start: p
nodes:
  p:
    inputs: {shell: "echo partial; exit 3"}
    shell: "true"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1
    _, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_INPUTS"


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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 2
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
    assert run_pipeline(path, workdir=work) == 0
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
    assert run_pipeline(path, workdir=work) == 0
    assert (work / "worked.txt").read_text().splitlines() == ["fresh"]
    run, _, _ = read_run(path.parent)
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["cached"]["ok"] is True and rows["cached"]["reason"] == "guard"
    assert rows["cached"]["attempts"] == 0 and rows["cached"]["signal"] == "done_before"


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
    assert run_pipeline(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    p1 = (run / "steps" / "001-p" / "input-0001" / "prompt.md").read_text()
    p4 = (run / "steps" / "001-p" / "input-0004" / "prompt.md").read_text()
    assert p1 == "task for aa" and p4 == "task for dd"


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
    assert run_pipeline(path, workdir=work) == 0


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
    assert run_pipeline(path, workdir=work) == 0
    run, _, _ = read_run(path.parent)
    assert "RACE" not in (run / "vars.yaml").read_text()
    rows = {r["input"]: r for r in read_manifest(run, "001-p")}
    assert rows["x"]["vars"] == {"RACE": "x"} and rows["y"]["vars"] == {"RACE": "y"}


def test_manifest_env_and_key_stability(tmp_path):
    text = """
version: "2"
start: fix-all
nodes:
  fix-all:
    inputs: [{id: 7}]
    shell: "true"
    on_signal: {__done__: consume}
  consume:
    shell: |
      jq -s -r '.[0].key' "$MEDULLA_MANIFEST_FIX_ALL" > key.txt
      echo "<signal:ok>k</signal:ok>"
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 0
    key = (work / "key.txt").read_text().strip()
    assert key.startswith("1:") and len(key.split(":")[1]) == 16   # (index, sha256[:16])


def test_deadline_mid_pool_preserves_manifest_rows(tmp_path):
    text = """
version: "2"
start: p
timeout: 2
nodes:
  p:
    inputs: [fast1, fast2, slow]
    shell: '[ "$MEDULLA_INPUT" = slow ] && sleep 30 || true'
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_pipeline(path, workdir=work) == 1
    run, outcome, _ = read_run(path.parent)
    assert outcome["error"]["code"] == "E_DEADLINE"
    rows = read_manifest(run, "001-p")
    done = {r["input"] for r in rows}
    assert {"fast1", "fast2"} <= done                   # concluded rows survive
    assert all(r["input"] != "slow" or not r["ok"] for r in rows)
