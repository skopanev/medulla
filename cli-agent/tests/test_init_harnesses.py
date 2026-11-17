"""Apple skill installation matches bundled pools to host agent CLIs."""
from pathlib import Path

import yaml


def test_init_apple_disables_missing_harness_and_restores_it_later(
        tmp_path, monkeypatch, capsys):
    import medulla.cli as shim

    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "medulla.init_harnesses.shutil.which",
        lambda binary: None if binary == "claude" else f"/bin/{binary}",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--skill", "--apple"])

    assert shim.entry() == 0
    workflow_path = home / ".medulla" / "workflows" / "spar" / "workflow.yaml"
    workflow = workflow_path.read_text()
    panel = yaml.safe_load(workflow)["nodes"]["panel"]
    assert {row["harness"] for row in panel["inputs"]} == {"codex", "opencode"}
    assert "# medulla-disabled:" in workflow
    assert "min_success: 2" in workflow
    assert 'MIN_DECIDED: "2"' in workflow
    assert "omitted unavailable host harnesses: claude-code" in capsys.readouterr().out

    monkeypatch.setattr(
        "medulla.init_harnesses.shutil.which", lambda binary: f"/bin/{binary}")
    assert shim.entry() == 0
    restored = workflow_path.read_text()
    restored_panel = yaml.safe_load(restored)["nodes"]["panel"]
    assert len(restored_panel["inputs"]) == 4
    assert "# medulla-disabled:" not in restored
    assert restored_panel["min_success"] == 3
    assert yaml.safe_load(restored)["vars"]["MIN_DECIDED"] == "3"


def test_init_apple_refuses_to_install_a_panel_smaller_than_three(
        tmp_path, monkeypatch, capsys):
    import medulla.cli as shim

    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "medulla.init_harnesses.shutil.which",
        lambda binary: f"/bin/{binary}" if binary == "codex" else None,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--skill", "--apple"])

    assert shim.entry() == 1
    output = capsys.readouterr().out
    assert "at least 3 installed panelists are required; found 1" in output
    assert not (home / ".codex" / "skills" / "spar" / "SKILL.md").exists()


def test_bundled_spar_roster_is_the_supported_four_models():
    workflow = (
        Path(__file__).resolve().parent.parent / "workflows" / "spar" / "workflow.yaml"
    )
    panel = yaml.safe_load(workflow.read_text(encoding="utf-8"))["nodes"]["panel"]

    assert panel["inputs"] == [
        {"slug": "glm5", "harness": "opencode",
         "model": "zai-coding-plan/glm-5.3", "effort": "max"},
        {"slug": "gpt5", "harness": "codex",
         "model": "gpt-5.6-sol", "effort": "high"},
        {"slug": "sonnet", "harness": "claude-code", "model": "claude-sonnet-5"},
        {"slug": "qwen38max", "harness": "opencode",
         "model": "opencode-go/qwen3.8-max", "effort": "medium"},
    ]
    assert panel["min_success"] == 3
