"""Part 6: CLI, resume, flock, prune. The panel's hardest scenario first:
an interrupted pool must not re-source and must not re-run done inputs."""
import fcntl
import json
import os

from medulla.v2.cli import main as cli_main
from medulla.v2.engine import find_resumable, run_workflow


def setup(tmp_path, text):
    pdir = tmp_path / "pipe"
    pdir.mkdir(exist_ok=True)
    (pdir / "workflow.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return pdir / "workflow.yaml", work


def runs_of(pdir):
    return sorted((pdir / "runs").iterdir())


def read_outcome(run):
    return json.loads((run / "outcome.json").read_text())


POOL_RESUME = """
version: "2"
start: p
timeout: {timeout}
nodes:
  p:
    inputs: {{shell: "echo source >> {work}/source-calls; printf 'a\\nb\\nc\\nd\\n'"}}
    shell: |
      echo run >> "{work}/body-$MEDULLA_INPUT"
      if [ "$MEDULLA_INPUT" = d ] && [ ! -f "{work}/second-pass" ]; then sleep 30; fi
    timeout: 300
    on_signal: {{__done__: __exit_ok__}}
"""


def test_pool_resume_no_resource_no_rerun(tmp_path):
    # THE dangerous scenario: pool dies on deadline mid-flight; resume must
    # (1) not re-run the source, (2) not re-run done inputs, (3) join over old+new
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE
    run = runs_of(path.parent)[0]
    assert read_outcome(run)["error"]["code"] == "E_DEADLINE"
    assert (work / "source-calls").read_text().count("source") == 1
    done_before = {f.name for f in work.glob("body-*")}
    assert {"body-a", "body-b", "body-c"} <= done_before

    (work / "second-pass").touch()                              # input d becomes fast
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    # source NOT re-executed; a/b/c NOT re-run; d ran exactly once more
    assert (work / "source-calls").read_text().count("source") == 1
    for name in ("a", "b", "c"):
        assert (work / f"body-{name}").read_text().count("run") == 1
    assert (work / "body-d").read_text().count("run") == 2      # first try + resumed
    assert read_outcome(run)["outcome"] == "succeeded"
    manifest = [json.loads(l) for l in
                (run / "steps" / "001-p" / "manifest.jsonl").read_text().splitlines()]
    assert sum(1 for r in manifest if r["ok"]) == 4


def work_dir(tmp_path):
    return str(tmp_path / "work")


DECISION_RESUME = """
version: "2"
start: a
timeout: 2
nodes:
  a:
    shell: 'echo a >> {work}/a-runs; echo "<signal:go>from-a</signal:go>"'
    on_signal: {{go: b}}
  b:
    shell: |
      echo b >> {work}/b-runs
      if [ ! -f {work}/fast ]; then sleep 30; fi
      [ "$MEDULLA_LAST_MESSAGE" = "from-a" ] && echo "<signal:ok>k</signal:ok>"
    timeout: 300
    on_signal: {{ok: __exit_ok__}}
"""


def test_decision_resume_continues_at_interrupted_node(tmp_path):
    path, work = setup(tmp_path, DECISION_RESUME.format(work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE at b
    run = runs_of(path.parent)[0]

    (work / "fast").touch()
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    assert (work / "a-runs").read_text().count("a") == 1        # a NOT re-run
    assert (work / "b-runs").read_text().count("b") == 2        # b re-ran whole (contract)
    journal = [json.loads(l) for l in (run / "journal.jsonl").read_text().splitlines()]
    assert [r["step"] for r in journal] == [1, 2]               # numbering continued, no dupes
    assert journal[0]["message"] == "from-a"                    # last.message survived resume


def test_legacy_pipeline_yaml_still_reads(tmp_path):
    # 4.1 renamed the config to workflow.yaml; untouched pre-4.1 projects
    # (pipeline.yaml) keep working — read side falls back, writes use the
    # new name (the run snapshot is workflow.yaml)
    from medulla.v2.rundir import config_yaml
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    pdir = tmp_path / "legacy"
    pdir.mkdir()
    (pdir / "pipeline.yaml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    assert run_workflow(config_yaml(pdir), workdir=work) == 0
    run = runs_of(pdir)[0]
    assert (run / "workflow.yaml").is_file()          # snapshot: new name
    # a pre-4.1 run dir (pipeline.yaml snapshot) is still seen by find_resumable
    old_run = pdir / "runs" / "2026-01-01_00-00-00-old1"
    old_run.mkdir(parents=True)
    (old_run / "pipeline.yaml").write_text("x", encoding="utf-8")
    assert find_resumable(pdir) == old_run            # no outcome.json = resumable


def test_crash_window_after_terminal_journal_row_finalizes(tmp_path):
    # the window: terminal row hits journal.jsonl, process dies BEFORE
    # outcome.json — resume must synthesize the outcome, not E_VALIDATION
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>payload</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    (run / "outcome.json").unlink()                 # simulate the kill window
    assert find_resumable(path.parent) == run       # no outcome = resumable
    assert run_workflow(path, workdir=work, resume_dir=run) == 0
    out = read_outcome(run)
    assert out["outcome"] == "succeeded"
    assert {"steps", "duration_s", "run_id"} <= set(out)

    # same window, failed terminal: exit code and error body survive
    text_fail = text.replace("__exit_ok__", "__exit_fail__")
    (tmp_path / "f").mkdir()
    path2, work2 = setup(tmp_path / "f", text_fail)
    assert run_workflow(path2, workdir=work2) == 2
    run2 = runs_of(path2.parent)[0]
    (run2 / "outcome.json").unlink()
    assert run_workflow(path2, workdir=work2, resume_dir=run2) == 2
    out2 = read_outcome(run2)
    assert out2["outcome"] == "failed"
    assert out2["error"]["message"] == "payload"    # rebuilt from the journal row


def test_crashed_outcome_shape_normalized(tmp_path):
    # crashed/interrupted outcomes lacked steps/duration_s/run_id (field
    # diff across real runs) — one shape for every outcome.json now
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1                # E_DEADLINE crash
    out = read_outcome(runs_of(path.parent)[0])
    assert out["outcome"] == "crashed"
    assert {"steps", "duration_s", "run_id", "error"} <= set(out)
    assert out["run_id"] == runs_of(path.parent)[0].name.rsplit("-", 1)[-1]


def test_resume_refuses_finished_run(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    assert run_workflow(path, workdir=work, resume_dir=run) == 1   # refuse, exit 1
    assert read_outcome(run)["outcome"] == "succeeded"             # outcome untouched


def test_find_resumable_selection(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: "sleep 30"
    timeout: 300
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text.replace('"sleep 30"', '"exit 1"'))
    assert run_workflow(path, workdir=work) == 2                # failed -> NOT resumable
    assert find_resumable(path.parent) is None

    # a crashed (E_DEADLINE-class) run IS resumable (documented deviation)
    (path.parent / "runs" / "2026-01-01_00-00-00-aaaa").mkdir(parents=True)
    crashed = path.parent / "runs" / "2026-01-01_00-00-00-aaaa"
    (crashed / "workflow.yaml").write_text("x", encoding="utf-8")
    (crashed / "outcome.json").write_text(
        json.dumps({"outcome": "crashed", "error": {"code": "E_DEADLINE"}}), encoding="utf-8")
    assert find_resumable(path.parent) == crashed


def test_flock_blocks_second_process(tmp_path):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    run = runs_of(path.parent)[0]
    (run / "outcome.json").unlink()                             # make it resumable
    # simulate a live holder
    fd = os.open(run / ".lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_workflow(path, workdir=work, resume_dir=run) == 1
    finally:
        os.close(fd)


def test_truncated_manifest_tail_tolerated(tmp_path):
    path, work = setup(tmp_path, POOL_RESUME.format(timeout=3, work=work_dir(tmp_path)))
    assert run_workflow(path, workdir=work) == 1
    run = runs_of(path.parent)[0]
    manifest = run / "steps" / "001-p" / "manifest.jsonl"
    with open(manifest, "a", encoding="utf-8") as f:
        f.write('{"index": 99, "key": "99:torn')                # crash-torn tail
    (work / "second-pass").touch()
    assert run_workflow(path, workdir=work, resume_dir=run) == 0   # tail dropped, not fatal


def test_prune_keeps_newest_and_active(tmp_path):
    text = """
version: "2"
start: a
keep_runs: 3
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    runs = path.parent / "runs"
    runs.mkdir(exist_ok=True)
    for i in range(6):                                          # old finished runs
        d = runs / f"2026-01-0{i + 1}_00-00-00-old{i}"
        d.mkdir(parents=True)
        (d / "workflow.yaml").write_text("x", encoding="utf-8")
        (d / "outcome.json").write_text('{"outcome": "succeeded"}', encoding="utf-8")
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    young = runs / f"{ts}-live"                                 # unfinished + young: shielded
    young.mkdir()
    (young / "workflow.yaml").write_text("x", encoding="utf-8")

    assert run_workflow(path, workdir=work) == 0
    names = {p.name for p in runs.iterdir()}
    assert young.name in names                                  # active shield held
    # prune runs at BOOT (the new run isn't finished yet): 6 finished -> keep 3 newest
    assert sorted(n for n in names if "old" in n) == [
        "2026-01-04_00-00-00-old3", "2026-01-05_00-00-00-old4", "2026-01-06_00-00-00-old5"]


# ── CLI surface ──────────────────────────────────────────────────────────────

def test_cli_flag_based_run_and_validate(tmp_path, monkeypatch, capsys):
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    monkeypatch.chdir(work)
    assert cli_main(["-w", str(path.parent), "--validate"]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert cli_main(["-w", str(path.parent)]) == 0              # fresh run
    assert cli_main(["-w", str(path.parent), "--resume"]) == 1  # nothing resumable


def test_cli_dry_run_prints_plan_without_running(tmp_path, monkeypatch, capsys):
    text = """
version: "2"
start: a
nodes:
  a:
    inputs: [x, y]
    max_parallel: 2
    shell: "touch should-not-run"
    on_signal: {__done__: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    monkeypatch.chdir(work)
    assert cli_main(["-w", str(path.parent), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[pool]" in out and "max_parallel: 2" in out and "__done__ -> __exit_ok__" in out
    assert not (work / "should-not-run").exists()
    assert not (path.parent / "runs").exists()                  # no run dir at all


def test_cli_usage_errors_exit_1(tmp_path):
    text = 'version: "2"\nstart: a\nnodes:\n  a:\n    shell: "true"\n    on_signal: {ok: __exit_ok__}\n'
    path, _ = setup(tmp_path, text)
    for argv in (
        ["-w", str(path.parent), "--resume", "--run", "x"],     # mutually exclusive
        ["-w", str(path.parent), "--resume", "--var", "A=1"],   # var is fresh-only
        ["-w", str(path.parent), "--resume", "--node", "a"],    # node is fresh-only
        [],                                                     # missing -w
    ):
        try:
            rc = cli_main(argv)
        except SystemExit as exc:
            rc = exc.code
        assert rc == 1                                          # never argparse's 2


def test_entry_dispatches_documented_subcommands(tmp_path, monkeypatch):
    # final-panel blocker: init/install-skill/upgrade were documented but
    # unreachable — the v2 shim lost the dispatch when v1 was deleted
    import medulla.cli as shim
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "disp-check"])
    assert shim.entry() == 0
    assert (tmp_path / ".medulla").is_dir()

    called = {}
    monkeypatch.setattr("subprocess.call",
                        lambda argv: (called.setdefault("argv", argv), 0)[1])
    monkeypatch.setattr("sys.argv", ["medulla", "upgrade"])
    # panel FIX-FIRST #1: upgrade must match the install method.
    # no installer venv → pipx-managed → pipx upgrade
    from pathlib import Path
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert shim.entry() == 0
    assert called["argv"] == ["pipx", "upgrade", "medulla"]

    # installer venv present (install.sh path, the README default) →
    # re-run install.sh; pipx upgrade would error or touch a different copy
    venv_bin = tmp_path / ".medulla" / "engine" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "medulla").touch()
    called.clear()
    assert shim.entry() == 0
    assert called["argv"][:2] == ["bash", "-c"] and "install.sh" in called["argv"][2]

    # pre-4.0.4 legacy path still detected (installer migrates it on next run)
    import shutil
    shutil.move(str(tmp_path / ".medulla" / "engine"), str(tmp_path / ".medulla-engine"))
    called.clear()
    assert shim.entry() == 0
    assert called["argv"][:2] == ["bash", "-c"] and "install.sh" in called["argv"][2]


def test_run_does_not_touch_workflow_dir_files(tmp_path):
    # owner decision: no gitignore magic at run time — init owns scaffolding
    text = 'version: "2"\nstart: a\nnodes:\n  a:\n    shell: \'echo "<signal:ok>k</signal:ok>"\'\n    on_signal: {ok: __exit_ok__}\n'
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert not (path.parent / ".gitignore").exists()


def test_init_without_name_prints_usage(tmp_path, monkeypatch, capsys):
    import medulla.cli as shim
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init"])
    assert shim.entry() == 1
    err = capsys.readouterr().err
    assert "usage: medulla init <name>" in err and "spar" in err


def test_init_deploys_bundled_template(tmp_path, monkeypatch):
    import medulla.cli as shim
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "spar"])
    assert shim.entry() == 0
    pdir = tmp_path / ".medulla" / "workflows" / "spar"
    assert (pdir / "workflow.yaml").is_file()
    assert (pdir / "prompts" / "spar.md").is_file()
    assert ".env" in (pdir / ".gitignore").read_text()
    assert not (pdir / "runs").exists()                  # template noise excluded


def test_init_scaffolds_a_workflow(tmp_path, monkeypatch):
    import medulla.cli as shim
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "my-pipe"])
    assert shim.entry() == 0
    pdir = tmp_path / ".medulla" / "workflows" / "my-pipe"
    assert (pdir / "workflow.yaml").is_file()
    assert (pdir / "README.md").is_file()
    assert ".env" in (pdir / ".gitignore").read_text()
    assert (pdir / "prompts").is_dir()
    # the scaffold must be a VALID, runnable workflow out of the box
    assert run_workflow(pdir / "workflow.yaml", workdir=tmp_path) == 0
    # re-init refuses to clobber
    monkeypatch.setattr("sys.argv", ["medulla", "init", "my-pipe"])
    assert shim.entry() == 1


def test_init_skill_flag(tmp_path, monkeypatch):
    import medulla.cli as shim
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "spar", "--skill"])
    assert shim.entry() == 0
    for root in (".claude", ".agents", ".opencode"):
        skill = tmp_path / root / "skills" / "spar" / "SKILL.md"
        assert skill.is_file() and "spar" in skill.read_text()
    monkeypatch.setattr("sys.argv", ["medulla", "init", "fresh", "--skill"])
    assert shim.entry() == 0
    # scaffold got a starter SKILL.md, and it is registered
    assert (tmp_path / ".medulla" / "workflows" / "fresh" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "fresh" / "SKILL.md").is_file()
