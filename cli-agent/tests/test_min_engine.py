"""A definition newer than the engine must fail on the person who published it.

Twice in one hour the machine-wide spar copy carried a field from unreleased work.
The installed engine did not know it and died at parse time — before a run directory
existed, so there were no artifacts and no trace, only a line in an err log. Every
project pulling the engine from git was affected, and the lanes that reported it had
nothing to do with the change and no way to fix it.
"""
import subprocess
import sys
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
