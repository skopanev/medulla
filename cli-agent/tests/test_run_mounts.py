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


def test_mnt_mounts_are_kept_and_the_rest_dropped(tmp_path, fake_proc):
    fake_proc(_mountinfo(("/workspace", "ro"), ("/mnt/medulla-workflows", "ro"),
                         ("/proc", "rw"), ("/sys/fs/cgroup", "ro")))
    rundir._record_mounts(tmp_path)
    lines = (tmp_path / "mounts.txt").read_text().splitlines()
    assert lines == ["ro\t/mnt/medulla-workflows", "ro\t/workspace"]


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
