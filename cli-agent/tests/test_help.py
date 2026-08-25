"""`medulla help` must print paths that are TRUE on the machine it runs on.

The whole point of the page is that an agent can copy a line out of it and have it
work. A listing that names a workflow the resolver would not find, or omits one it
would, is worse than no listing: it manufactures exactly the wrong-path failure it
exists to prevent. So these tests check the listing against the resolver, not
against the prose.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medulla import help as mhelp  # noqa: E402
from medulla.v2.workflow_path import resolve_workflow_yaml  # noqa: E402

WORKFLOW = "version: '2'\nstart: n\nnodes:\n  n:\n    shell: 'true'\n    on_signal: {}\n"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fake HOME and CWD, so the listing describes THIS tree and nothing of the user's."""
    home, cwd = tmp_path / "home", tmp_path / "repo"
    (home / ".medulla" / "workflows").mkdir(parents=True)
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(cwd)
    return home, cwd


def _make(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "workflow.yaml").write_text(WORKFLOW)
    return d


def _page() -> str:
    buf = io.StringIO()
    mhelp.print_help(buf)
    return buf.getvalue()


def test_lists_the_machine_wide_workflow_by_bare_name(sandbox):
    home, _ = sandbox
    _make(home / ".medulla" / "workflows", "spar")
    page = _page()
    assert "medulla --docker -w spar" in page
    assert "(machine-wide)" in page


def test_every_listed_name_actually_resolves(sandbox):
    home, cwd = sandbox
    _make(home / ".medulla" / "workflows", "spar")
    _make(cwd / ".medulla" / "workflows", "denoise")
    for name, d, _scope in mhelp._discover():
        # the claim the page makes: `-w <name>` from here finds THIS definition
        assert resolve_workflow_yaml(Path(name)).is_file(), name
        assert d.is_dir()


def test_local_copy_shadows_the_shared_one_and_says_so(sandbox):
    home, cwd = sandbox
    _make(home / ".medulla" / "workflows", "spar")
    _make(cwd / ".medulla" / "workflows", "spar")
    page = _page()
    assert page.count("medulla --docker -w spar") == 2      # both are named
    assert "shadowed by the local copy" in page
    # and the winner is the local one — the resolver's rule, restated by the page
    assert str(resolve_workflow_yaml(Path("spar"))).startswith(str(cwd))


def test_a_directory_without_a_workflow_yaml_is_not_offered(sandbox):
    home, _ = sandbox
    (home / ".medulla" / "workflows" / "leftovers").mkdir(parents=True)
    assert mhelp._discover() == []
    assert "AVAILABLE HERE — none" in _page()


def test_a_shipped_launcher_is_pointed_at(sandbox):
    home, _ = sandbox
    d = _make(home / ".medulla" / "workflows", "spar")
    (d / "scripts").mkdir()
    (d / "scripts" / "spar-run.sh").write_text("#!/bin/sh\n")
    page = _page()
    assert "use it instead" in page
    assert "spar-run.sh" in page


def test_no_arguments_prints_the_page_not_a_usage_error(sandbox, monkeypatch, capsys):
    home, _ = sandbox
    _make(home / ".medulla" / "workflows", "spar")
    from medulla.cli import entry
    monkeypatch.setattr(sys, "argv", ["medulla"])
    assert entry() == 0
    out = capsys.readouterr().out
    assert "RUN ONE" in out and "-w spar" in out


def test_help_word_is_a_subcommand(sandbox, monkeypatch, capsys):
    from medulla.cli import entry
    monkeypatch.setattr(sys, "argv", ["medulla", "help"])
    assert entry() == 0
    assert "RUN ONE" in capsys.readouterr().out


def test_run_from_home_lists_each_workflow_once(sandbox, monkeypatch):
    """From $HOME the local root and the machine-wide root are the SAME directory —
    listing it twice made a workflow look like it shadowed itself."""
    home, _ = sandbox
    _make(home / ".medulla" / "workflows", "spar")
    monkeypatch.chdir(home)
    assert [n for n, _d, _s in mhelp._discover()] == ["spar"]
    page = _page()
    assert "shadowed" not in page
    # and it is the machine-wide copy — "local" would be true only of where you
    # are standing, and reads as a repo copy that does not exist
    assert "(machine-wide)" in page


def test_refresh_updates_the_machine_wide_copy_too(sandbox, monkeypatch, capsys):
    """`refresh <name> ~/Projects` used to report success while leaving the copy every
    bare name resolves to two versions behind — it lives in $HOME, not in the folder
    anyone passes as the search root."""
    from medulla.refresh import refresh_skill

    home, cwd = sandbox
    shared = home / ".medulla" / "workflows" / "spar"
    (shared / "scripts").mkdir(parents=True)
    (shared / "workflow.yaml").write_text("stale\n")
    (shared / "scripts" / "spar-run.sh").write_text("#!/bin/sh\necho stale\n")

    # a bundle to refresh FROM, standing in for the installed package's copy
    bundle = home / "bundle" / "spar"
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "workflow.yaml").write_text("current\n")
    (bundle / "SKILL.md").write_text("skill\n")
    (bundle / "scripts" / "spar-run.sh").write_text("#!/bin/sh\necho current\n")
    monkeypatch.setattr("medulla.refresh._bundle_dir", lambda _n: bundle)

    assert refresh_skill("spar", str(cwd)) == 0        # search root does NOT contain it
    assert (shared / "workflow.yaml").read_text() == "current\n"
    assert "current" in (shared / "scripts" / "spar-run.sh").read_text()
    assert "machine-wide" in capsys.readouterr().out


def test_refresh_updates_machine_wide_skills_including_profiles(sandbox, monkeypatch, capsys):
    """A skill installed in $HOME is never under the search root either — and Claude
    Code profiles (~/.claude-<name>) multiply the copies. Five of them stayed stale
    while refresh reported success."""
    from medulla.refresh import refresh_skill

    home, cwd = sandbox
    bundle = home / "bundle" / "spar"
    bundle.mkdir(parents=True)
    (bundle / "workflow.yaml").write_text("current\n")
    (bundle / "SKILL.md").write_text("current skill\n")
    monkeypatch.setattr("medulla.refresh._bundle_dir", lambda _n: bundle)

    installed = []
    for rel in (".claude/skills", ".agents/skills", ".config/opencode/skills",
                ".claude-work/skills", ".claude-personal/skills"):
        d = home / rel / "spar"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("stale\n")
        installed.append(d / "SKILL.md")
    # a profile with no skills dir must not be conjured into one
    (home / ".claude-empty").mkdir()

    assert refresh_skill("spar", str(cwd)) == 0
    for target in installed:
        assert target.read_text() == "current skill\n", target
    assert not (home / ".claude-empty" / "skills").exists()
    assert capsys.readouterr().out.count("(machine-wide)") >= 5


def test_codex_gets_the_skill_where_it_actually_reads(sandbox, monkeypatch):
    """codex reads ~/.codex/skills. The list sent it to ~/.agents/skills instead, so
    codex was the one harness of four that could not convene a panel — its skills
    directory held only OpenAI's own .system/ entries."""
    from medulla.init import skill_dests_global

    home, _cwd = sandbox
    for rel in (".claude/skills", ".agents/skills", ".codex/skills",
                ".config/opencode/skills"):
        (home / rel).mkdir(parents=True)

    dests = [str(d).replace(str(home), "~") for d in skill_dests_global()]
    assert "~/.codex/skills" in dests
    assert "~/.claude/skills" in dests
    assert "~/.config/opencode/skills" in dests


def test_refresh_reaches_the_codex_copy(sandbox, monkeypatch, capsys):
    from medulla.refresh import refresh_skill

    home, cwd = sandbox
    bundle = home / "bundle" / "spar"
    bundle.mkdir(parents=True)
    (bundle / "workflow.yaml").write_text("current\n")
    (bundle / "SKILL.md").write_text("current skill\n")
    monkeypatch.setattr("medulla.refresh._bundle_dir", lambda _n: bundle)

    target = home / ".codex" / "skills" / "spar"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("stale\n")

    assert refresh_skill("spar", str(cwd)) == 0
    assert (target / "SKILL.md").read_text() == "current skill\n"


def test_a_symlinked_skill_is_left_alone(sandbox, monkeypatch):
    """Someone may prefer one copy and a link to it. refresh never writes THROUGH a
    symlink (CWE-59), so that choice survives — and the target updates anyway."""
    from medulla.refresh import refresh_skill

    home, cwd = sandbox
    bundle = home / "bundle" / "spar"
    bundle.mkdir(parents=True)
    (bundle / "workflow.yaml").write_text("current\n")
    (bundle / "SKILL.md").write_text("current skill\n")
    monkeypatch.setattr("medulla.refresh._bundle_dir", lambda _n: bundle)

    real = home / ".medulla" / "workflows" / "spar"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("stale\n")
    (real / "workflow.yaml").write_text("stale\n")
    link_parent = home / ".codex" / "skills"
    link_parent.mkdir(parents=True)
    (link_parent / "spar").symlink_to(real)

    assert refresh_skill("spar", str(cwd)) == 0
    assert not (link_parent / "spar").is_symlink() is False   # still a link
    assert (real / "SKILL.md").read_text() == "current skill\n"   # target refreshed
