"""host.docker.internal — reaching the host from inside a container.

Docker Desktop answers that name from its own DNS. A mac therefore never needed the
flag, and nobody noticed it was absent. WSL and plain Linux do not answer it: the name
does not resolve, and a connector pointed at it fails with a DNS error that says nothing
about docker. Reported from a WSL host, where every lane container hit it.

Measured on this mac before the change: `/etc/hosts` had NO entry for the name, and
nslookup still answered it — which is exactly how the gap stayed invisible here.
"""
import importlib.util
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


def _mapping(cmd):
    """Every --add-host value in a built command, both spellings."""
    out = [v for i, v in enumerate(cmd) if i > 0 and cmd[i - 1] == "--add-host"]
    return out + [v.split("=", 1)[1] for v in cmd if v.startswith("--add-host=")]


def test_the_host_is_mapped_by_default(process):
    cmd = process.build_run_command("img", ["-v", "/a:/b"], ["-w", "."], "c")
    assert "host.docker.internal:host-gateway" in _mapping(cmd)


def test_host_gateway_is_resolved_by_the_daemon_not_guessed_by_us(process):
    """The literal string matters: host-gateway makes the DAEMON fill in the address, so
    the value is right on every host. A hardcoded 192.168.65.254 is this mac's answer and
    nobody else's."""
    cmd = process.build_run_command("img", [], [], "c")
    value = next(v for v in _mapping(cmd) if v.startswith("host.docker.internal:"))
    assert value.endswith(":host-gateway")


def test_a_callers_own_mapping_wins(process):
    """A second --add-host for the same name leaves two entries in /etc/hosts. Overriding
    a deliberate choice is not ours to do."""
    cmd = process.build_run_command(
        "img", ["-v", "/a:/b", "--add-host", "host.docker.internal:1.2.3.4"], [], "c")
    assert _mapping(cmd) == ["host.docker.internal:1.2.3.4"]


def test_the_equals_spelling_is_recognised_too(process):
    """`--add-host=name:ip` is the same flag. Missing it would add a duplicate."""
    cmd = process.build_run_command(
        "img", [], ["--add-host=host.docker.internal:1.2.3.4"], "c")
    assert _mapping(cmd) == ["host.docker.internal:1.2.3.4"]


def test_an_unrelated_add_host_does_not_suppress_ours(process):
    """Only this name is ours to map. A caller mapping something else still needs the
    host, and silently dropping it would reintroduce the WSL failure."""
    cmd = process.build_run_command(
        "img", ["--add-host", "broker.internal:10.0.0.5"], [], "c")
    assert "host.docker.internal:host-gateway" in _mapping(cmd)
    assert "broker.internal:10.0.0.5" in _mapping(cmd)


def test_a_kept_container_gets_it_too(process):
    """A kept session creates its container through this same builder, so the mapping
    travels with it. `docker exec` cannot add one later — /etc/hosts is written at create
    time."""
    src = (SCRIPTS / "dockerlib" / "session_run.py").read_text()
    assert "build_run_command" in src
