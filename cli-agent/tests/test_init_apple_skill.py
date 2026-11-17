"""Apple skill installation stays global, runtime-specific, and symlink-safe."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def host_harnesses_are_not_test_machine_dependencies(monkeypatch):
    monkeypatch.setattr("medulla.init_harnesses.shutil.which", lambda binary: binary)


def _skill_paths(home: Path, workflow_dir: Path) -> list[Path]:
    return [workflow_dir / "SKILL.md", *(
        root / "spar" / "SKILL.md" for root in (
            home / ".claude" / "skills",
            home / ".agents" / "skills",
            home / ".codex" / "skills",
        )
    )]


def test_init_apple_skill_switch_survives_refresh(tmp_path, monkeypatch):
    import medulla.cli as shim
    from medulla.init import _bundle_dir, refresh_skill

    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--skill", "--apple"])
    assert shim.entry() == 0

    workflow_dir = home / ".medulla" / "workflows" / "spar"
    skill_paths = _skill_paths(home, workflow_dir)
    for skill in skill_paths:
        text = skill.read_text()
        assert "medulla launch spar --apple start" in text
        assert "medulla launch spar start" not in text

    bundle = _bundle_dir("spar")
    assert bundle is not None
    assert "medulla launch spar start" in (bundle / "SKILL.md").read_text()

    assert refresh_skill("spar", str(tmp_path)) == 0
    for skill in skill_paths:
        assert "medulla launch spar --apple start" in skill.read_text()

    custom = (workflow_dir / "workflow.yaml").read_text() + "\n# keep me\n"
    (workflow_dir / "workflow.yaml").write_text(custom, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["medulla", "init", "spar", "--skill"])
    assert shim.entry() == 0
    assert (workflow_dir / "workflow.yaml").read_text() == custom
    for skill in skill_paths:
        text = skill.read_text()
        assert "medulla launch spar start" in text
        assert "medulla launch spar --apple" not in text


def test_init_local_apple_skill_stays_local(tmp_path, monkeypatch):
    import medulla.cli as shim

    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--skill", "--apple", "--local"])
    assert shim.entry() == 0
    for root in (tmp_path / ".medulla" / "workflows",
                 tmp_path / ".claude" / "skills",
                 tmp_path / ".agents" / "skills",
                 tmp_path / ".codex" / "skills"):
        text = (root / "spar" / "SKILL.md").read_text()
        assert "medulla launch spar --apple start" in text
    assert not (tmp_path / ".opencode" / "skills" / "spar").exists()
    assert not (tmp_path / ".gemini" / "skills" / "spar").exists()
    assert not (home / ".medulla" / "workflows" / "spar").exists()


def test_init_skill_refuses_source_or_destination_symlink(tmp_path, monkeypatch):
    import medulla.cli as shim

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["medulla", "init", "spar", "--local"])
    assert shim.entry() == 0
    source = tmp_path / ".medulla" / "workflows" / "spar" / "SKILL.md"
    source_text = source.read_text()
    victim = tmp_path / "victim.md"
    victim.write_text(source_text, encoding="utf-8")
    source.unlink()
    source.symlink_to(victim)

    monkeypatch.setattr(
        "sys.argv", ["medulla", "init", "spar", "--skill", "--apple", "--local"])
    assert shim.entry() == 1
    assert victim.read_text() == source_text

    source.unlink()
    source.write_text(source_text, encoding="utf-8")
    dest = tmp_path / ".claude" / "skills" / "spar"
    dest.mkdir(parents=True)
    target_victim = tmp_path / "target-victim.md"
    target_victim.write_text("safe", encoding="utf-8")
    (dest / "SKILL.md").symlink_to(target_victim)

    assert shim.entry() == 1
    assert target_victim.read_text() == "safe"
