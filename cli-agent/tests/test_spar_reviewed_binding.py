"""A verdict must say what it actually reviewed — not what it was told about.

A thirty-kilobyte verdict costing an hour and four models used to carry no record
of the thing it looked at: a day later nobody could say whether it applied to the
current tree or to a state three days gone. HEAD alone does not answer it either,
because the panel reads the WORKING TREE: a dirty or untracked file is part of what
was reviewed while belonging to no commit.
"""
import subprocess

import pytest
import yaml as pyyaml

from conftest import read_run
from conftest import write_workflow as setup
from medulla.v2.engine import run_workflow

WORKFLOW = __import__("pathlib").Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"


def prepare_shell():
    return pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]


def run_prepare(cwd, question="a question"):
    # The run directory lives OUTSIDE the reviewed tree, exactly as it does in
    # production (~/.medulla/panel-runs). Putting it inside makes the tree dirty —
    # which the binding correctly noticed, and which is the bug this fixture had.
    run_dir = cwd.parent / f"{cwd.name}-run"
    run_dir.mkdir(exist_ok=True)
    res = subprocess.run(["bash", "-c", prepare_shell()], capture_output=True, text=True,
                         cwd=cwd, env={**__import__("os").environ,
                                       "QUESTION": question,
                                       "MEDULLA_RUN_DIR": str(run_dir)},
                         check=False)
    return res.stdout + res.stderr


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "file.py").write_text("x = 1\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first")
    return tmp_path


def digest_of(out):
    for line in out.splitlines():
        if "REVIEWED_DIGEST" in line:
            return line.split(">", 1)[1].split("<", 1)[0]
    return None


def state_of(out):
    for line in out.splitlines():
        if "REVIEWED_STATE" in line:
            return line.split(">", 1)[1].split("<", 1)[0]
    return None


def test_a_clean_tree_is_bound_to_its_head(repo):
    out = run_prepare(repo)
    assert state_of(out) == "clean", out
    assert "REVIEWED_HEAD" in out
    assert len(digest_of(out)) == 64


def test_an_uncommitted_edit_changes_the_binding(repo):
    before = digest_of(run_prepare(repo))
    (repo / "file.py").write_text("x = 2\n")
    after = run_prepare(repo)
    assert state_of(after) == "dirty"
    assert digest_of(after) != before, "a dirty tree reviewed as if it were HEAD"


def test_an_untracked_file_changes_the_binding(repo):
    """It belongs to no commit, and the panel can still open and review it."""
    before = digest_of(run_prepare(repo))
    (repo / "new.py").write_text("y = 1\n")
    after = run_prepare(repo)
    assert state_of(after) == "dirty"
    assert digest_of(after) != before, "an untracked file left no trace in the binding"


def test_untracked_CONTENT_changes_the_binding(repo):
    """Not just its name: the bytes the panelists could read."""
    (repo / "new.py").write_text("y = 1\n")
    before = digest_of(run_prepare(repo))
    (repo / "new.py").write_text("y = 2\n")
    assert digest_of(run_prepare(repo)) != before


def test_the_same_tree_gives_the_same_binding(repo):
    """Reproducible, or it proves nothing."""
    assert digest_of(run_prepare(repo)) == digest_of(run_prepare(repo))


def test_a_non_git_tree_says_so_instead_of_inventing(tmp_path):
    """The non-code question mode — a strategy call with no diff at all — must keep
    working, and must not be handed a fabricated revision."""
    (tmp_path / "notes.md").write_text("just a question\n")
    out = run_prepare(tmp_path)
    assert state_of(out) == "not-a-git-repository", out
    assert "REVIEWED_DIGEST" not in out
    assert "<signal:ready>" in out, "the round still starts"


def test_a_worktree_without_its_gitdir_is_not_called_unversioned(tmp_path):
    """A lane worktree's .git is a FILE pointing at a gitdir OUTSIDE the mount, so
    inside a container every git command fails with "not a git repository" while
    the tree plainly is one. Reported by finik-pm from a live round.

    Calling that "not a git repository" lies in the dangerous direction: the reader
    concludes the code is unversioned, when the truth is the MOUNT is wrong. And the
    opposite fix — asserting git always works — is the same error with the sign
    flipped, because both mounts are real. Only a probe separates them.
    """
    tree = tmp_path / "wt"
    tree.mkdir()
    (tree / "file.py").write_text("x = 1\n")
    (tree / ".git").write_text("gitdir: /nonexistent/gitdir\n")
    out = run_prepare(tree)
    assert state_of(out) == "git-unavailable", out
    assert digest_of(out) is None, "nothing can be bound to a revision here"
    assert "<signal:ready>" in out, "the round still runs — the bytes are reviewable"
    assert "worktree" in out.lower(), "say WHY, or the reader guesses"


def run_prepare_with(cwd, **extra):
    import os as os_mod
    run_dir = cwd.parent / f"{cwd.name}-run"
    run_dir.mkdir(exist_ok=True)
    res = subprocess.run(["bash", "-c", prepare_shell()], capture_output=True, text=True,
                         cwd=cwd, env={**os_mod.environ, "QUESTION": "q",
                                       "MEDULLA_RUN_DIR": str(run_dir), **extra},
                         check=False)
    return res.returncode, res.stdout + res.stderr


def test_a_named_head_that_does_not_match_the_tree_stops_the_round(repo):
    """virtiofs served a STALE snapshot: the tree was not empty, it held yesterday's
    code, and four panelists returned four confident INSUFFICIENTs about a subject
    that no longer existed. An empty tree produces honest INSUFFICIENT; the wrong
    tree produces a full-looking round with a machine-readable verdict and nothing
    saying the subject was swapped. One comparison beats twenty minutes."""
    rc, out = run_prepare_with(repo, HEAD="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert rc == 1, out
    assert "subject mismatch" in out
    assert "deadbeef" in out and "you passed HEAD" in out, "name BOTH, or it is unactionable"


def test_a_matching_head_runs(repo):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    rc, out = run_prepare_with(repo, HEAD=head)
    assert rc == 0 and "<signal:ready>" in out, out


def test_an_abbreviated_head_still_matches(repo):
    """Callers paste short shas. Treating one as a mismatch would fail rounds that
    are perfectly correct — worse than not checking, because it trains people to
    ignore the check."""
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    rc, out = run_prepare_with(repo, HEAD=full[:9])
    assert rc == 0 and "<signal:ready>" in out, out


def test_no_head_passed_behaves_as_before(repo):
    """Nothing is required: with no expectation there is nothing to check against,
    and inventing one would be worse than no check."""
    rc, out = run_prepare_with(repo)
    assert rc == 0 and "subject mismatch" not in out
