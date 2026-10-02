"""--docker-engine: a private dockerd inside the run, for integration tests.

Off unless asked, and off is the path every other project on this machine takes. These
tests spend most of their effort on that: the flag's absence must change nothing, and its
presence must not quietly cancel a guarantee somebody else is relying on.

Handed over as a reviewed patch from another project. The three things fixed during
integration are each pinned below — a silent collision with --cwd-ro, a traceback where a
sentence belonged, and half of an anonymous-volume leak.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def box():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "dockerlib.box", SCRIPTS / "dockerlib" / "box.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(SCRIPTS))


def _image(user="1000:1000", env=("HOME=/home/box",)):
    """Answer `docker image inspect` with this Config."""
    payload = json.dumps({"User": user, "Env": list(env)})
    return mock.patch.object(
        subprocess, "run",
        return_value=subprocess.CompletedProcess([], 0, stdout=payload, stderr=""))


# ── the flag is off ─────────────────────────────────────────────────────────

def test_without_the_flag_nothing_is_touched(box):
    volumes = ["-v", "/host:/workspace:ro", "-v", "/runs:/runs"]
    args = ["-w", "."]
    out_v, out_a = box.prepare("img", volumes, args)
    assert out_v == volumes and out_a == args


def test_without_the_flag_docker_is_never_even_asked(box):
    """The metadata read costs a `docker image inspect` per run. Every workflow on this
    machine takes the off path, and none of them should pay for a feature they do not use
    — nor fail when the daemon is down for an unrelated reason."""
    with mock.patch.object(subprocess, "run") as run:
        box.prepare("img", ["-v", "/a:/b"], ["-w", "."])
    run.assert_not_called()


def test_exec_user_is_silent_on_an_ordinary_container(box):
    assert box.exec_user(["-v", "/a:/b", "-e", "FOO=bar"]) is None


# ── the flag is on ──────────────────────────────────────────────────────────

def test_the_flag_is_consumed_and_the_capability_added(box):
    with _image():
        volumes, args = box.prepare("img", ["-v", "/a:/b"], ["-w", ".", "--docker-engine"])
    assert "--docker-engine" not in args, "the engine must never see it"
    assert args == ["-w", "."]
    assert "--privileged" in volumes
    assert volumes[volumes.index("--user") + 1] == "0:0"
    assert "type=volume,target=/var/lib/docker" in volumes
    assert "BROKER_BOX_DOCKER=1" in volumes
    assert "BROKER_BOX_UID=1000" in volumes and "BROKER_BOX_GID=1000" in volumes
    assert "HOME=/home/box" in volumes


def test_the_kept_exec_drops_to_the_images_user(box):
    """`docker exec` does not run ENTRYPOINT, so the lazy startup that drops root never
    happens for a kept container. Without this the first run in a kept session is the
    image's user and every later one is root."""
    with _image():
        volumes, _args = box.prepare("img", [], ["--docker-engine"])
    assert box.exec_user(volumes) == "1000:1000"


def test_a_half_set_pair_is_refused_rather_than_guessed(box):
    """`--user 1000:` is read by docker as uid with the image's gid — quietly. prepare
    sets both or neither, so a half pair means something else wrote these and the honest
    answer is to not drop at all."""
    assert box.exec_user(["-e", "BROKER_BOX_DOCKER=1", "-e", "BROKER_BOX_UID=1000"]) is None


# ── the collision that was silent ───────────────────────────────────────────

def test_cwd_ro_is_allowed_and_said_out_loud(box, capsys):
    """THE finding of this integration, and its second shape.

    A privileged root container is not bound by a read-only bind mount — capabilities are
    what make ro mean anything and root in a privileged container has them all. Accepted
    in silence, the pair produced a run whose mounts.txt reads `ro /workspace` while the
    guarantee is gone, reading identically to one from an ordinary run. That line gets
    cited as evidence that nothing inside could write to the reviewed tree.

    My first fix REFUSED the combination. Wrong shape, and the lane that uses --cwd-ro
    said so: it protected the record by removing a capability, and the mount flags were
    never what was at risk. So the run is allowed and admits it instead. No mount spec
    changes; what changes is that the evidence stops claiming enforcement it lacks.
    """
    with _image():
        volumes, args = box.prepare("img", ["-v", "/host/tree:/workspace:ro"],
                                    ["--docker-engine"])
    assert "--privileged" in volumes, "the invocation must work"
    assert "-v" in volumes and "/host/tree:/workspace:ro" in volumes, \
        "the declared mount travels exactly as written"
    err = capsys.readouterr().err
    assert "--cwd-ro" in err and "NOT ENFORCED" in err


def test_no_second_flag_is_required_to_acknowledge_the_first(box):
    """Passing --docker-engine IS the acknowledgement. A flag to acknowledge a flag is
    ceremony, and the lane documents the limitation on its own side already."""
    with _image():
        _volumes, args = box.prepare("img", ["-v", "/t:/workspace:ro"],
                                     ["-w", ".", "--docker-engine"])
    assert args == ["-w", "."]


def test_a_writable_workspace_says_nothing(box, capsys):
    """Only the read-only promise is worth a warning. An ordinary --docker run makes no
    promise to break, and a warning on every run gets muted."""
    with _image():
        volumes, _args = box.prepare("img", ["-v", "/host/tree:/workspace"],
                                     ["--docker-engine"])
    assert "--privileged" in volumes
    assert capsys.readouterr().err == ""


def test_ro_elsewhere_is_not_the_promise(box, capsys):
    """The credential mounts are read-only on every run — /mnt/claude and friends. Only
    the WORKSPACE promise is the one --cwd-ro makes, so only it is worth saying."""
    with _image():
        volumes, _args = box.prepare(
            "img", ["-v", "/h/.claude:/mnt/claude:ro", "-v", "/host/tree:/workspace"],
            ["--docker-engine"])
    assert "--privileged" in volumes
    assert capsys.readouterr().err == ""


# ── bad images and absent daemons say so ────────────────────────────────────

def test_a_missing_image_gets_a_sentence_not_a_traceback(box, capsys):
    """check=True alone raised CalledProcessError out of run_docker. The usual cause is
    mundane — the image is not built on this host yet — and it deserves a sentence."""
    boom = subprocess.CalledProcessError(1, "docker", stderr="Error: No such image: img")
    with mock.patch.object(subprocess, "run", side_effect=boom):
        with pytest.raises(SystemExit) as exit_info:
            box.prepare("img", [], ["--docker-engine"])
    assert exit_info.value.code == 1
    assert "cannot read image metadata" in capsys.readouterr().err


def test_a_hung_daemon_says_so(box, capsys):
    with mock.patch.object(subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("docker", 30)):
        with pytest.raises(SystemExit):
            box.prepare("img", [], ["--docker-engine"])
    assert "did not answer" in capsys.readouterr().err


def test_broken_metadata_says_so(box, capsys):
    with mock.patch.object(subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, stdout="{oops",
                                                                    stderr="")):
        with pytest.raises(SystemExit):
            box.prepare("img", [], ["--docker-engine"])
    assert "not JSON" in capsys.readouterr().err


@pytest.mark.parametrize("user,env,why", [
    ("", ("HOME=/home/box",), "no USER at all"),
    ("box:box", ("HOME=/home/box",), "a name is not a uid the exec can reuse"),
    ("0:0", ("HOME=/home/box",), "root is what this flag exists to drop out of"),
    ("1000", ("HOME=/home/box",), "uid with no gid"),
    ("1000:1000", (), "no HOME"),
    ("1000:1000", ("HOME=relative",), "HOME must be absolute"),
])
def test_an_image_that_cannot_support_it_is_refused_with_its_reason(box, capsys,
                                                                   user, env, why):
    with pytest.raises(SystemExit), _image(user=user, env=env):
        box.prepare("img", [], ["--docker-engine"])
    err = capsys.readouterr().err
    assert "numeric non-root USER" in err, why
    assert "img" in err, "name the image — an operator has several"


# ── it is wired into the run path ───────────────────────────────────────────

def test_run_docker_consumes_the_flag_before_either_branch():
    """Plain and kept both go through prepare, so the capability and the uid drop travel
    together. A kept container built without them would exec as root forever."""
    src = (SCRIPTS / "dockerlib" / "process.py").read_text()
    assert "box.prepare(image, volumes, args)" in src
    assert src.index("box.prepare") < src.index("if keep_session")


def test_the_flag_is_documented_where_the_other_docker_flags_are():
    """It is a host-side flag like --mount and --build, and `medulla --help` is where an
    operator looks. The patch printed a line above usage instead, which only appeared for
    the exact spelling `--help` and never for `-h`."""
    from medulla.v2.clihelp import ENV_HELP
    assert "--docker-engine" in ENV_HELP
    assert "--cwd-ro" in ENV_HELP.split("--docker-engine", 1)[1][:600], \
        "the collision belongs in the help, not only in the code"
