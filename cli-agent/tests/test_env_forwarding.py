"""Knobs set on the host must mean the same inside the container.

Only three run ids travelled. Every tuning variable the engine reads stayed outside, so
the engine INSIDE ran on its built-in defaults whatever the host was told. Reported from
a loaded host: MEDULLA_FIRST_OUTPUT_S=180 was set, the inner watchdog used its 60s
default, and a coder was killed with "no output at all in 60s" — rc=124, reason watchdog.
An operator turned the knob and nothing moved.
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def process():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "dockerlib.process", SCRIPTS / "dockerlib" / "process.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(SCRIPTS))


def test_the_watchdog_knob_reaches_the_container(process, monkeypatch):
    """The reported case."""
    monkeypatch.setenv("MEDULLA_FIRST_OUTPUT_S", "180")
    assert process.forwarded_env_values().get("MEDULLA_FIRST_OUTPUT_S") == "180"


def test_every_knob_the_engine_reads_is_forwarded(process):
    """The whole class, not the one that bit. The engine reads these from os.environ and
    nowhere else, so missing one leaves the same defect under another name.

    This test DERIVES the list from the engine rather than repeating it: a future knob
    added to the engine and forgotten here fails, which a hand-written list cannot do.
    """
    engine = Path(__file__).resolve().parent.parent / "medulla"
    read = set()
    for src in engine.rglob("*.py"):
        text = src.read_text()
        read |= set(re.findall(r'environ\.get\(\s*"(MEDULLA_[A-Z_]+)"', text))
        read |= set(re.findall(r'_env_seconds\(\s*"(MEDULLA_[A-Z_]+)"', text))
    # Set BY docker.py for the container itself, not read from the host.
    set_by_docker = {"MEDULLA_DOCKER", "MEDULLA_RUNS_UNDER", "MEDULLA_RUN_DIR_NAME"}
    # Per-run identity, forwarded under its own name.
    ids = set(process.ORCHESTRATION_ENV_KEYS)
    missing = read - set_by_docker - ids - set(process.TUNING_ENV_KEYS)
    assert not missing, f"the engine reads these and nothing forwards them: {sorted(missing)}"


def test_an_unset_knob_stays_unset(process, monkeypatch):
    """No default is invented and no real timeout is masked: absent on the host is
    absent inside. Forwarding an empty value would override the engine's own default
    with nothing."""
    monkeypatch.delenv("MEDULLA_FIRST_OUTPUT_S", raising=False)
    monkeypatch.delenv("MEDULLA_IDLE_OUTPUT_S", raising=False)
    values = process.forwarded_env_values()
    assert "MEDULLA_FIRST_OUTPUT_S" not in values
    assert "MEDULLA_IDLE_OUTPUT_S" not in values


def test_the_value_travels_by_name_and_never_in_argv(process, monkeypatch):
    """docker reads the value from its own environment. A value in argv is readable by
    anyone running `ps`, and this forwards secrets' neighbours."""
    monkeypatch.setenv("MEDULLA_FIRST_OUTPUT_S", "1800")
    cmd = process.build_run_command("img", [], [], "c")
    named = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert "MEDULLA_FIRST_OUTPUT_S" in named
    assert "1800" not in " ".join(cmd)


def test_defaults_in_the_engine_are_untouched():
    """The fix forwards a value; it must not move a default. 60 and 900 stay."""
    from medulla.v2 import procrun
    assert procrun._env_seconds("MEDULLA_NOT_SET_ANYWHERE", 60) == 60
    src = (Path(__file__).resolve().parent.parent / "medulla/v2/procrun.py").read_text()
    assert '_env_seconds("MEDULLA_FIRST_OUTPUT_S", 60)' in src
    assert '_env_seconds("MEDULLA_IDLE_OUTPUT_S", 900)' in src


def test_the_knob_is_documented_where_an_operator_looks():
    """It was read by the engine and absent from --help, so the one way to find it was
    to read the source."""
    from medulla.v2.clihelp import ENV_HELP
    assert "MEDULLA_FIRST_OUTPUT_S" in ENV_HELP
    assert "forwarded into the container" in ENV_HELP
