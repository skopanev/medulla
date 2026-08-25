"""The CLI's flags and exit codes — the surface a caller scripts against.
"""
import fcntl
import json
import os
from pathlib import Path

from medulla.v2.cli import main as cli_main
from medulla.v2.engine import find_resumable, run_workflow

from conftest import write_workflow as setup

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


