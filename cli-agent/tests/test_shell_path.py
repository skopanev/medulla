"""A body and a hook must get the tools the engine was started with.

Bodies and hooks ran under `bash -lc`. A login shell re-reads the profile and can replace
the PATH medulla inherited, so a workflow saw a different interpreter than the engine
running it — and a body calling python3 got an older one than the package requires.

The trade: a workflow that needs a tool must put it on PATH itself. Inheriting the
caller's PATH is reproducible; re-reading a login profile is not. BASH_ENV still applies.
"""
import os
import sys

import pytest
from conftest import read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow


def test_a_shell_body_keeps_the_callers_PATH(tmp_path, monkeypatch):
    """The whole defect in one assertion: a tool the caller put on PATH must be the one
    the body finds."""
    fake_bin = tmp_path / "toolbox"
    fake_bin.mkdir()
    tool = fake_bin / "mytool"
    tool.write_text("#!/usr/bin/env bash\necho CALLER-TOOL\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'mytool > seen.txt; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "seen.txt").read_text().strip() == "CALLER-TOOL"


def test_a_hook_keeps_it_too(tmp_path, monkeypatch):
    """A hook and a body that disagree about their PATH is worse than either choice, so
    both take the same flag."""
    fake_bin = tmp_path / "toolbox"
    fake_bin.mkdir()
    tool = fake_bin / "mytool"
    tool.write_text("#!/usr/bin/env bash\necho CALLER-TOOL\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    text = """
version: "2"
start: a
nodes:
  a:
    pre: 'mytool > from-hook.txt'
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert (work / "from-hook.txt").read_text().strip() == "CALLER-TOOL"


def test_the_same_python_the_engine_runs_is_the_one_a_body_finds(tmp_path):
    """A body calling python3 must not get an older interpreter than the engine's. This
    package requires >=3.10, and a login profile can put an older one first."""
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'python3 -c "import sys; print(sys.version_info[:2])" > ver.txt; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    got = (work / "ver.txt").read_text().strip()
    assert got == str(sys.version_info[:2]), f"body saw {got}, engine runs {sys.version_info[:2]}"


def test_neither_place_uses_a_login_shell():
    """Both call sites, asserted directly: a future edit that restores -lc on one of them
    reintroduces the split PATH."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "medulla" / "v2"
    for name in ("engine_body.py", "procrun.py"):
        src = (root / name).read_text()
        assert '"-lc"' not in src, f"{name} still uses a login shell"
        assert '"-c"' in src


def test_MEDULLA_SHELL_still_chooses_the_shell(tmp_path, monkeypatch):
    """The override is unrelated to the flag and must survive the change."""
    monkeypatch.setenv("MEDULLA_SHELL", "sh")
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo $0 > which-shell.txt; echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0
    assert "sh" in (work / "which-shell.txt").read_text()
