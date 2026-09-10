"""A definition newer than the engine must fail on the person who published it.

Twice in one hour the machine-wide spar copy carried a field from unreleased work.
The installed engine did not know it and died at parse time — before a run directory
existed, so there were no artifacts and no trace, only a line in an err log. Every
project pulling the engine from git was affected, and the lanes that reported it had
nothing to do with the change and no way to fix it.
"""
import subprocess
import sys

import pytest
from pathlib import Path

from conftest import write_workflow as setup
from medulla.v2.contract import engine_is_older_than, engine_version
from medulla.refresh import _declared_min_engine

MINIMAL = """
version: "2"
{extra}start: n
nodes:
  n:
    shell: 'echo hi'
    on_signal: {{__done__: __exit_ok__}}
"""


def load_err(tmp_path, text):
    path, _ = setup(tmp_path, text)
    res = subprocess.run([sys.executable, "-m", "medulla", "-w", str(path), "--validate"],
                         capture_output=True, text=True, check=False)
    return res.stdout + res.stderr


def test_a_definition_newer_than_the_engine_says_so(tmp_path):
    """The old message named the symptom — an unknown field — not the cause."""
    err = load_err(tmp_path, MINIMAL.format(extra='min_engine: "99.0.0"\n'))
    assert "min_engine: 99.0.0" in err, err
    assert engine_version() in err, "name BOTH versions"
    assert "NEWER than the engine" in err


def test_a_definition_the_engine_satisfies_loads(tmp_path):
    err = load_err(tmp_path, MINIMAL.format(extra='min_engine: "0.1.0"\n'))
    assert "min_engine" not in err, err


def test_an_unknown_top_level_field_points_at_the_version_gap(tmp_path):
    """An older engine cannot know min_engine either, so the generic message has to
    carry the hint: that is the only thing a stale engine can still say usefully."""
    err = load_err(tmp_path, MINIMAL.format(extra="post_confirms_delivery: true\n"))
    assert "unknown top-level fields" in err
    assert "version gap" in err, err


def test_min_engine_must_look_like_a_version(tmp_path):
    err = load_err(tmp_path, MINIMAL.format(extra="min_engine: [4, 61]\n"))
    assert "must be a version string" in err, err


def test_the_comparison_is_numeric_not_lexicographic():
    assert engine_is_older_than("99.0.0")
    assert not engine_is_older_than("0.0.1")


def test_refresh_reads_the_declaration_without_a_yaml_parse(tmp_path):
    """It runs before any engine machinery, and the definition being caught is
    precisely one this engine may be unable to parse."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text('version: "2"\nmin_engine: "4.99.0"\nstart: n\n')
    assert _declared_min_engine(wf) == "4.99.0"
    wf.write_text('version: "2"\nstart: n\n')
    assert _declared_min_engine(wf) is None
    assert _declared_min_engine(tmp_path / "absent.yaml") is None


def test_an_unknown_engine_version_never_blocks(monkeypatch):
    """medulla.__version__ reads 3.0.29 against a 4.61 pyproject. Falling back to it
    would refuse a healthy engine over a stale constant, so an unmeasurable gate
    stays open instead of guessing."""
    from medulla.v2 import contract
    monkeypatch.setattr(contract, "engine_version", lambda: "")
    assert contract.engine_is_older_than("99.0.0") is False


def test_refresh_replaces_the_name_not_the_contents(tmp_path, monkeypatch):
    """copy2 writes through to the same inode, and bash reads a script AS it runs it.
    Refreshing under a live spar-run.sh fed it new bytes at an old offset and it died
    on `syntax error near unexpected token '('` mid-round — the verdict was lost for a
    panel that had already finished its work. Measured downstream during the
    4.65.0 -> 4.66.0 upgrade, with source.txt recording the older engine while the CLI
    reported the newer one."""
    import os
    from medulla.refresh import _copy_bundle_over

    src = tmp_path / "bundle"
    src.mkdir()
    (src / "run.sh").write_text("original\n")
    dst = tmp_path / "installed"
    dst.mkdir()
    (dst / "run.sh").write_text("old\n")

    before = os.stat(dst / "run.sh").st_ino
    _copy_bundle_over(src, dst)
    after = os.stat(dst / "run.sh").st_ino

    assert (dst / "run.sh").read_text() == "original\n"
    assert before != after, "same inode: a running reader would see spliced bytes"
    leftovers = [p.name for p in dst.iterdir() if "medulla-tmp" in p.name]
    assert not leftovers, leftovers


def test_skill_files_are_replaced_by_name_too(tmp_path):
    """The workflow copy was fixed first because a shell script fails LOUDLY when
    spliced. A SKILL.md fails silently: an agent reading it mid-refresh gets prose
    that makes slightly less sense than it should, and nobody files a bug.

    Written by breaking the property the check names — the assertion is on the inode,
    because asserting the text passes for the whole life of the defect.
    """
    import os
    from medulla.refresh import _replace_file

    src = tmp_path / "new.md"
    src.write_text("new skill\n")
    dst = tmp_path / "SKILL.md"
    dst.write_text("old skill\n")

    before = os.stat(dst).st_ino
    _replace_file(src, dst)

    assert dst.read_text() == "new skill\n"
    assert os.stat(dst).st_ino != before, "wrote through the inode a reader may hold"
    assert not [p for p in tmp_path.iterdir() if "medulla-tmp" in p.name]


def test_a_failed_replace_leaves_no_temp_behind(tmp_path, monkeypatch):
    """The recovery path nobody exercises: a half-copied temp beside a live deploy is
    worse than the failure that produced it.

    The first version of this test passed WITHOUT the fix — a missing source never
    creates a temp, so there was nothing to clean and the assertion proved nothing.
    Breaking the property properly means failing AFTER the temp exists, so os.replace
    is the thing that has to break.
    """
    import os as os_mod
    from medulla import refresh

    src = tmp_path / "new.md"
    src.write_text("new\n")
    dst = tmp_path / "SKILL.md"
    dst.write_text("old\n")

    def boom(*_args):
        raise OSError("cross-device link")

    monkeypatch.setattr(refresh.os, "replace", boom)
    with pytest.raises(OSError):
        refresh._replace_file(src, dst)

    assert dst.read_text() == "old\n", "the original must survive a failed swap"
    leftovers = [p.name for p in tmp_path.iterdir() if "medulla-tmp" in p.name]
    assert not leftovers, leftovers
