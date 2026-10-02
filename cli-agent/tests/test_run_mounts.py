"""Does a run say, from its own evidence, what it could write to?

Asked afterwards whether a panel could have written into the tree it was reviewing, the
answer had to be inferred: read the launcher, find the branch that adds --cwd-ro, confirm
the run took it. The container was gone (--rm), so the run directory — the thing that
exists to hold a run's evidence — could not answer. Now it records the kernel's own view.
"""
import pathlib

import pytest
from medulla.v2 import rundir

# a real /proc/self/mountinfo line, trimmed to the fields the parser reads
LINE = ("{id} 1 0:64 / {point} {opts} shared:1 - overlay overlay "
        "rw,lowerdir=/x,upperdir=/y")


def _mountinfo(*rows):
    return "\n".join(LINE.format(id=100 + i, point=p, opts=o)
                     for i, (p, o) in enumerate(rows)) + "\n"


@pytest.fixture
def fake_proc(monkeypatch):
    """Answer /proc/self/mountinfo with a given table; every other read is real."""
    def install(text):
        real = pathlib.Path.read_text

        def fake(self, *a, **kw):
            if str(self) == "/proc/self/mountinfo":
                if text is None:
                    raise OSError("no such file")
                return text
            return real(self, *a, **kw)
        monkeypatch.setattr(pathlib.Path, "read_text", fake)
    return install


def test_a_read_only_workspace_is_recorded_as_read_only(tmp_path, fake_proc):
    fake_proc(_mountinfo(("/workspace", "ro,relatime")))
    rundir._record_mounts(tmp_path)
    assert (tmp_path / "mounts.txt").read_text() == "ro\t/workspace\n"


def test_a_writable_workspace_is_recorded_as_writable(tmp_path, fake_proc):
    # The point is to be able to tell the two apart afterwards. A recorder that says
    # "ro" either way answers the question wrongly, which is worse than not answering.
    fake_proc(_mountinfo(("/workspace", "rw,relatime")))
    rundir._record_mounts(tmp_path)
    assert (tmp_path / "mounts.txt").read_text() == "rw\t/workspace\n"


def test_rw_is_not_read_as_ro(tmp_path, fake_proc):
    # "ro" is a substring of plenty of option names; the flag is a whole field.
    fake_proc(_mountinfo(("/workspace", "rw,nosuid,errors=remount-ro")))
    rundir._record_mounts(tmp_path)
    assert (tmp_path / "mounts.txt").read_text().startswith("rw\t")


def test_pseudo_filesystems_are_dropped_and_real_mounts_kept(tmp_path, fake_proc):
    fake_proc(_mountinfo(("/workspace", "ro"), ("/mnt/medulla-workflows", "ro"),
                         ("/proc", "rw"), ("/sys/fs/cgroup", "ro"), ("/dev/shm", "rw"),
                         ("/", "rw"), ("/Volumes/hdd/.medulla/panel-runs", "rw")))
    rundir._record_mounts(tmp_path)
    lines = (tmp_path / "mounts.txt").read_text().splitlines()
    assert lines == ["ro\t/mnt/medulla-workflows", "ro\t/workspace",
                     "rw\t/Volumes/hdd/.medulla/panel-runs"]


def test_a_NESTED_mount_is_recorded(tmp_path, fake_proc):
    """The case the first cut dropped. `--mount` places a sibling repository at
    /workspace/<name>, inside the reviewed tree, and that mount is why the tree reads
    dirty. A round was asked to explain its own dirty state and its mount table did not
    mention the mount responsible."""
    fake_proc(_mountinfo(("/workspace", "ro"), ("/workspace/sibling-repo", "ro")))
    rundir._record_mounts(tmp_path)
    lines = (tmp_path / "mounts.txt").read_text().splitlines()
    assert lines == ["ro\t/workspace", "ro\t/workspace/sibling-repo"]


def test_the_writable_run_directory_is_recorded(tmp_path, fake_proc):
    """It was invisible before, so "every line says ro" read as "nothing was writable" —
    which was never true. The run directory is writable by design, outside the tree."""
    fake_proc(_mountinfo(("/workspace", "ro"), ("/host/panel-runs", "rw")))
    rundir._record_mounts(tmp_path)
    assert "rw\t/host/panel-runs" in (tmp_path / "mounts.txt").read_text()


def test_no_proc_writes_nothing(tmp_path, fake_proc):
    # A native run on a mac. Not an error: a run that cannot describe its mounts still
    # has work to do, and an empty or half-written file would be read as evidence.
    fake_proc(None)
    rundir._record_mounts(tmp_path)
    assert not (tmp_path / "mounts.txt").exists()


def test_a_table_with_nothing_interesting_writes_nothing(tmp_path, fake_proc):
    fake_proc(_mountinfo(("/proc", "rw"), ("/", "rw")))
    rundir._record_mounts(tmp_path)
    assert not (tmp_path / "mounts.txt").exists()


def test_a_truncated_line_does_not_crash_the_run(tmp_path, fake_proc):
    fake_proc("garbage\n" + _mountinfo(("/workspace", "ro")))
    rundir._record_mounts(tmp_path)
    assert (tmp_path / "mounts.txt").read_text() == "ro\t/workspace\n"


def test_a_native_run_still_starts(tmp_path):
    # The recorder sits on the run-creation path. On this host there is no /proc, so
    # this is the real-world check that it stays out of the way.
    from conftest import write_workflow
    from medulla.v2.engine import run_workflow
    text = """
version: "2"
start: a
nodes:
  a:
    shell: 'echo "<signal:ok>k</signal:ok>"'
    on_signal: {ok: __exit_ok__}
"""
    path, work = write_workflow(tmp_path, text)
    assert run_workflow(path, workdir=work) == 0


# ── when ro is not a guarantee, the file says so ────────────────────────────
#
# A read-only bind mount is read-only because the process lacks the capability to remount
# it. A privileged container has that capability, so its `ro` lines are DECLARED and not
# ENFORCED — while reading exactly like an ordinary run's. That line gets cited as
# evidence that nothing inside could write to the reviewed tree; I have cited it myself,
# twice, in incident answers. It cannot carry that meaning here, so the file admits it.

def _status(cap_bnd: str, cap_eff: str = "0000000000000000"):
    """Answer /proc/self/status with these caps, /proc/self/mountinfo with one ro mount.

    CapEff defaults to ZERO on purpose: that is the real shape of the run this annotation
    exists for. The engine inside the container starts after setpriv / `docker exec
    --user`, uid already dropped, so an effective-set check sees nothing — which is how
    the first version of this managed to stay silent on exactly the run it was written
    for. The bounding set is what survives that drop.
    """
    import pathlib as _p
    real = _p.Path.read_text

    def fake(self, *a, **kw):
        if str(self) == "/proc/self/status":
            return f"Name:\tpython\nCapEff:\t{cap_eff}\nCapBnd:\t{cap_bnd}\n"
        if str(self) == "/proc/self/mountinfo":
            return _mountinfo(("/workspace", "ro"))
        return real(self, *a, **kw)
    return fake


def test_a_privileged_container_is_declared_in_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    monkeypatch.setattr(pathlib.Path, "read_text", _status("000001ffffffffff"))
    rundir._record_mounts(tmp_path)
    text = (tmp_path / "mounts.txt").read_text()
    assert "PRIVILEGED" in text and "NOT ENFORCED" in text
    assert "ro\t/workspace" in text, "the mounts themselves are still recorded"


def test_an_ordinary_container_gets_no_such_line(tmp_path, monkeypatch):
    """CAP_SYS_ADMIN absent — a normal run, where the ro line means what it says. A
    header on every run would train readers to ignore it."""
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    monkeypatch.setattr(pathlib.Path, "read_text", _status("00000000a80425fb"))
    rundir._record_mounts(tmp_path)
    assert "PRIVILEGED" not in (tmp_path / "mounts.txt").read_text()


def test_it_reads_capabilities_not_our_own_flag(tmp_path, monkeypatch):
    """The question is about the container, not about which flag created it. Anything
    granting CAP_SYS_ADMIN makes the same line untrue, whoever asked for it — so the
    --docker-engine marker is not consulted."""
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    monkeypatch.delenv("BROKER_BOX_DOCKER", raising=False)
    monkeypatch.setattr(pathlib.Path, "read_text", _status("000001ffffffffff"))
    rundir._record_mounts(tmp_path)
    assert "PRIVILEGED" in (tmp_path / "mounts.txt").read_text()


def test_it_survives_the_uid_drop(tmp_path, monkeypatch):
    """THE defect PK found, pinned. The inner engine runs after setpriv / `docker exec
    --user`, so CapEff is 0 — and the first version of this checked CapEff and therefore
    said nothing on a real privileged kept run. Verified on the actual published release
    before it was caught: the run completed, the annotation was absent. A guard that
    reports nothing is worse than the silence it replaced, because I had already told them
    the evidence was honest."""
    monkeypatch.setenv("MEDULLA_DOCKER", "1")
    monkeypatch.setattr(pathlib.Path, "read_text",
                        _status("000001ffffffffff", cap_eff="0000000000000000"))
    rundir._record_mounts(tmp_path)
    assert "PRIVILEGED" in (tmp_path / "mounts.txt").read_text()


def test_a_native_run_says_nothing_however_full_its_bounding_set(tmp_path, monkeypatch):
    """An ordinary Linux login shell carries a full bounding set too. Annotating every
    native run would train readers to ignore the line, which costs more than it buys."""
    monkeypatch.delenv("MEDULLA_DOCKER", raising=False)
    monkeypatch.setattr(pathlib.Path, "read_text", _status("000001ffffffffff"))
    rundir._record_mounts(tmp_path)
    assert "PRIVILEGED" not in (tmp_path / "mounts.txt").read_text()


def test_unreadable_capabilities_add_nothing(tmp_path, monkeypatch):
    """No /proc/self/status, or a CapEff that will not parse: say nothing rather than
    guess. A false "PRIVILEGED" would discredit honest evidence."""
    import pathlib as _p
    real = _p.Path.read_text

    monkeypatch.setenv("MEDULLA_DOCKER", "1")

    def fake(self, *a, **kw):
        if str(self) == "/proc/self/status":
            return "CapBnd:\tnot-a-number\n"
        if str(self) == "/proc/self/mountinfo":
            return _mountinfo(("/workspace", "ro"))
        return real(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", fake)
    rundir._record_mounts(tmp_path)
    assert "PRIVILEGED" not in (tmp_path / "mounts.txt").read_text()
