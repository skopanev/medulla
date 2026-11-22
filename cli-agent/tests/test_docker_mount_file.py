"""--mount-file: one host file, read-only, at a container path the caller names.

Argv-level throughout. No docker, no daemon: what is under test is the contract — which
targets are refused, where the -v flag lands, and that the flag never reaches the
engine. A refusal that only happens inside a container is a refusal nobody can read.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from dockerlib import cliargs, mountfiles  # noqa: E402


@pytest.fixture
def secret(tmp_path):
    p = tmp_path / "token.txt"
    p.write_text("s3cret\n", encoding="utf-8")
    return p


def parse(args):
    return cliargs.parse(args)


# ── what reaches docker, and what reaches the engine ────────────────────────

def test_the_flag_is_ours_and_never_reaches_the_engine(secret):
    args, opts = parse(["-w", "spar", "--mount-file", f"{secret}:/run/secrets/token"])
    assert args == ["-w", "spar"], "the engine has no container to mount into"
    assert opts["mount_files"] == [(secret.resolve(), "/run/secrets/token")]


def test_the_volume_is_read_only(secret):
    vols = []
    mountfiles.mount_secret_files(vols, [(secret, "/run/secrets/token")])
    assert vols == ["-v", f"{secret}:/run/secrets/token:ro"]


def test_repeatable_and_ordered_by_argv(secret, tmp_path):
    second = tmp_path / "other.txt"
    second.write_text("x\n", encoding="utf-8")
    _, opts = parse(["--mount-file", f"{secret}:/a/one",
                     "--mount-file", f"{second}:/b/two"])
    assert [t for _, t in opts["mount_files"]] == ["/a/one", "/b/two"]


def test_the_session_identity_changes_with_the_file(secret, tmp_path):
    """A different secret is a different world, so it must not reuse a container.

    keep.spec_digest hashes the finished volume list, so this comes free — the test
    exists because a later refactor that builds the mount AFTER the digest would be
    silent, and the symptom would be one lane reading another lane's secret.
    """
    from dockerlib import keep
    other = tmp_path / "other.txt"
    other.write_text("different\n", encoding="utf-8")
    a, b = [], []
    mountfiles.mount_secret_files(a, [(secret, "/run/s")])
    mountfiles.mount_secret_files(b, [(other, "/run/s")])
    assert keep.spec_digest("img", a) != keep.spec_digest("img", b)


# ── the refusals, every one on the host ─────────────────────────────────────

def refusal(args) -> str:
    with pytest.raises(SystemExit) as exc:
        parse(args)
    return str(exc.value)


def test_a_directory_is_refused_and_points_at_the_right_flag(tmp_path):
    msg = refusal(["--mount-file", f"{tmp_path}:/run/secrets"])
    assert "directory" in msg and "--mount" in msg


def test_a_missing_file_is_refused(tmp_path):
    assert "no such file" in refusal(
        ["--mount-file", f"{tmp_path / 'gone.txt'}:/run/s"])


def test_a_relative_container_path_is_refused(secret):
    assert "absolute" in refusal(["--mount-file", f"{secret}:run/s"])


def test_an_unnormalised_container_path_is_refused(secret):
    assert "normalised" in refusal(["--mount-file", f"{secret}:/run/../etc/passwd"])


def test_a_trailing_mode_is_refused_and_says_why(secret):
    """The likeliest mistake, because docker's own -v takes :ro. The last colon
    separates, so the mode would be read as the container path."""
    msg = refusal(["--mount-file", f"{secret}:/run/s:ro"])
    assert "takes no mode" in msg and "read-only always" in msg


def test_a_target_in_the_workspace_is_refused(secret):
    """The daemon creates a missing target inside a bind mount, and for a file that
    leaves an empty file in the user's repository. Under --cwd-ro it cannot create it
    at all. Both were measured here before this guard existed."""
    assert "/workspace" in refusal(["--mount-file", f"{secret}:/workspace/token"])


@pytest.mark.parametrize("target", [
    "/mnt/init-docker.sh",
    "/mnt/agy-token",
    "/mnt/agy-safe-key",
    "/mnt/medulla-workflows/spar/workflow.yaml",
])
def test_medullas_own_mounts_are_refused(secret, target):
    """A caller target over one of these replaces something the run needs, and the
    failure then arrives far from the flag: a missing entrypoint, or an agy seat
    without its key."""
    assert "collides" in refusal(["--mount-file", f"{secret}:{target}"])


def test_the_reserved_list_matches_the_real_mounts():
    """One source of truth. A new mount of ours that is not reserved is a target a
    caller may take, and the drift would only show as a broken container."""
    assert mountfiles.INIT_DOCKER_TARGET in mountfiles.RESERVED_TARGETS
    assert mountfiles.AGY_TOKEN_TARGET in mountfiles.RESERVED_TARGETS
    assert mountfiles.AGY_SAFE_KEY_TARGET in mountfiles.RESERVED_TARGETS
    assert mountfiles.DEFINITION_TARGET_ROOT in mountfiles.RESERVED_TARGETS


def test_the_same_target_twice_is_refused(secret, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("x\n", encoding="utf-8")
    msg = refusal(["--mount-file", f"{secret}:/run/s",
                   "--mount-file", f"{other}:/run/s"])
    assert "already mounted" in msg


def test_a_missing_separator_is_refused(secret):
    assert "HOST:CONTAINER" in refusal(["--mount-file", str(secret)])


# ── the error paths this flag's neighbours used to take ─────────────────────

def test_a_missing_var_file_exits_instead_of_a_traceback(tmp_path):
    """It printed the right message and then died on `cannot unpack non-iterable int
    object`, because parse returns a pair and the error path returned 1. The operator
    read a traceback about unpacking rather than the line naming their file."""
    assert "no such file" in refusal(["--var-file", f"k={tmp_path / 'gone'}"])


def test_cwd_ro_without_a_runs_folder_exits(tmp_path):
    assert "--runs-folder" in refusal(["--cwd-ro"])
