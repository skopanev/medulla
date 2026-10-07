"""Finding the container's $HOME — and failing loudly when it cannot be found.

Credentials mount under that path. Get it wrong and the broker config, a lane's NTK key
and every other home-based credential land where nothing reads them. The symptom appears
far from the cause: the harness reports a missing key, and nothing says the mount went to
the wrong place.

The old version took one 30s probe and, on ANY failure, returned the caller's default:
`except Exception: pass; return fallback`. Under host load the probe does not finish, so
an image whose home is /home/medulla got credentials at /home/hltm. Reported from a live
run. The probe timing out is weather; the silent guess was the defect.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def image_mod():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "dockerlib.image", SCRIPTS / "dockerlib" / "image.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(SCRIPTS))


def _runs(*results):
    """Queue subprocess.run results in call order."""
    it = iter(results)

    def fake(*a, **kw):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    return fake


def _ok(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


# ── metadata answers first, without a container ─────────────────────────────

def test_metadata_is_read_and_no_container_is_started(image_mod):
    """`docker image inspect` reads a local file: no scheduling, nothing to time out
    under load. Measured on this host, every image that declares HOME reports the value
    the probe returns: two images here report /home/medulla and /home/node."""
    calls = []

    def fake(argv, *a, **kw):
        calls.append(argv)
        return _ok(stdout="PATH=/usr/bin\nHOME=/home/medulla\nLANG=C\n")
    with mock.patch.object(subprocess, "run", fake):
        assert image_mod.image_home("img") == "/home/medulla"
    assert len(calls) == 1 and calls[0][1] == "image"
    assert all("run" != c[1] for c in calls), "no container may be started"


def test_a_useless_metadata_home_is_ignored(image_mod):
    """HOME=/ or a relative value is not a home. Accepting it would mount credentials
    at the filesystem root."""
    with mock.patch.object(subprocess, "run", _runs(
            _ok(stdout="HOME=/\n"), _ok(stdout="/home/probed"))):
        assert image_mod.image_home("img") == "/home/probed"


# ── the probe covers images that declare none ───────────────────────────────

def test_the_probe_runs_when_the_image_declares_no_home(image_mod):
    """OUR OWN default image declares none — measured: medulla-default has no HOME in
    its metadata. Metadata alone would break every panel, so the probe has to stay."""
    with mock.patch.object(subprocess, "run", _runs(
            _ok(stdout="PATH=/usr/bin\n"), _ok(stdout="/root"))):
        assert image_mod.image_home("img") == "/root"


def test_a_timed_out_probe_is_retried_once(image_mod):
    """A single timeout under load WAS the whole bug. One retry after a pause separates
    a busy daemon from a broken image."""
    with mock.patch.object(subprocess, "run", _runs(
            _ok(stdout=""),                                   # no metadata
            subprocess.TimeoutExpired("docker", 30),          # busy
            _ok(stdout="/home/medulla"))), \
         mock.patch.object(image_mod.time, "sleep", lambda s: None):
        assert image_mod.image_home("img") == "/home/medulla"


# ── it fails instead of guessing ────────────────────────────────────────────

def test_two_failed_probes_fail_the_run_rather_than_guess(image_mod, capsys):
    """THE defect. Credentials mount under this path, so continuing with a default puts
    them where nothing reads them — and the harness then blames a missing key."""
    with mock.patch.object(subprocess, "run", _runs(
            _ok(stdout=""),
            subprocess.TimeoutExpired("docker", 30),
            subprocess.TimeoutExpired("docker", 30))), \
         mock.patch.object(image_mod.time, "sleep", lambda s: None):
        with pytest.raises(SystemExit) as exit_info:
            image_mod.image_home("medulla-crew:latest")
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "cannot determine $HOME" in err
    assert "medulla-crew:latest" in err, "name the image — an operator has several"
    assert "did not finish" in err, "say which failure, not just that there was one"


def test_the_caller_passes_no_fallback(image_mod):
    """docker.py used to hand CONTAINER_HOME over as a default, which is what turned a
    timeout into a wrong mount path. Nothing in medulla passes one now."""
    src = (SCRIPTS / "docker.py").read_text()
    assert "image_home(image)" in src
    assert "image_home(image, " not in src


def test_home_is_never_derived_from_the_user(image_mod):
    """A uid is not a home. The two differ, and guessing /home/<user> is how this class
    of defect starts — so Config.User must not appear in the lookup at all."""
    src = (SCRIPTS / "dockerlib" / "image.py").read_text()
    body = src.split("def image_home", 1)[1].split("\ndef ", 1)[0]
    # The docstring NAMES Config.User to warn against it, so match the format template
    # that is actually sent to docker rather than the prose around it.
    code = [l for l in body.splitlines() if not l.lstrip().startswith("#")]
    sent = "\n".join(code)
    assert "{{range .Config.Env}}" in sent, "metadata must come from Config.Env"
    assert "{{.Config.User}}" not in sent, "a uid is not a home"
