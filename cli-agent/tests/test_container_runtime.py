"""Direct host, Docker, and Apple Container dispatch."""
from pathlib import Path

import medulla.cli as shim


def test_no_runtime_flag_runs_host(monkeypatch):
    calls = []
    monkeypatch.setattr("medulla.v2.cli.main", lambda argv: (calls.append(argv), 0)[1])
    monkeypatch.setattr("sys.argv", ["medulla", "-w", "workflow", "--validate"])

    assert shim.entry() == 0
    assert calls == [["-w", "workflow", "--validate"]]


def test_docker_flag_dispatches_docker_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(shim, "_find_script", lambda name: Path("/runner") / name)
    monkeypatch.setattr("subprocess.call", lambda argv: (calls.append(argv), 0)[1])
    monkeypatch.setattr("sys.argv", ["medulla", "--docker", "-w", "workflow", "--validate"])

    assert shim.entry() == 0
    assert Path(calls[0][1]).as_posix() == "/runner/docker.py"
    assert calls[0][2:] == ["-w", "workflow", "--validate"]


def test_apple_flag_dispatches_apple_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(shim, "_find_script", lambda name: Path("/runner") / name)
    monkeypatch.setattr(
        shim,
        "_exec_runner",
        lambda runner, argv: (calls.append([runner, *argv]), 0)[1],
    )
    monkeypatch.setattr("sys.argv", ["medulla", "--apple", "-w", "workflow", "--validate"])

    assert shim.entry() == 0
    assert Path(calls[0][0]).as_posix() == "/runner/apple_container.py"
    assert calls[0][1:] == ["-w", "workflow", "--validate"]


def test_apple_and_docker_are_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["medulla", "--apple", "--docker", "-w", "workflow"])

    assert shim.entry() == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_runtime_conflict_is_rejected_before_upgrade_side_effects(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv))
    monkeypatch.setattr(
        "sys.argv", ["medulla", "upgrade", "--apple", "--docker"])

    assert shim.entry() == 1
    assert calls == []
    assert "mutually exclusive" in capsys.readouterr().err


def test_missing_apple_runner_has_actionable_error(monkeypatch, capsys):
    monkeypatch.setattr(shim, "_find_script", lambda _name: None)
    monkeypatch.setattr("sys.argv", ["medulla", "--apple", "-w", "workflow"])

    assert shim.entry() == 1
    assert "scripts/apple_container.py not found" in capsys.readouterr().err


def test_runtime_help_is_local(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["medulla", "--apple", "--help"])

    assert shim.entry() == 0
    help_text = capsys.readouterr().out
    assert "medulla --apple" in help_text
    assert "medulla --docker" in help_text


def test_init_help_exposes_apple_skill_opt_in(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["medulla", "init", "--help"])

    assert shim.entry() == 0
    help_text = capsys.readouterr().out
    assert "usage: medulla init <name> [--skill [--apple]]" in help_text
    assert "bundled Docker skills stay on Docker by default" in help_text
    assert "container-runtime" not in help_text


def test_refresh_help_is_available(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["medulla", "refresh", "--help"])

    assert shim.entry() == 0
    assert "medulla refresh <name> <root>" in capsys.readouterr().out


def test_init_apple_requires_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "spar", "--apple"])

    assert shim.entry() == 1
    assert not (tmp_path / ".medulla").exists()
    assert "--apple requires --skill" in capsys.readouterr().err


def test_removed_runtime_selection_is_rejected_before_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--container-runtime", "apple"])

    assert shim.entry() == 1
    assert not (tmp_path / ".medulla").exists()
    assert "unknown flag: --container-runtime" in capsys.readouterr().err
