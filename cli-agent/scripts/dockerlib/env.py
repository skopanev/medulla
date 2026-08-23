"""Secrets for a containerised run: the three .env tiers and the transient env-file.

Split out of docker.py under the project's 250-line rule. This is the only part of the
wrapper that handles values nobody should see — keeping it apart makes "who can read a
token" a question about one short file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.path.realpath(__file__)).parent.parent
try:
    from medulla.v2.workflow_path import workflow_dir_for
except ImportError:                                   # running from a source checkout
    sys.path.insert(0, str(PROJECT_ROOT))
    from medulla.v2.workflow_path import workflow_dir_for

CLAUDE_TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
env_file_for_run: str | None = None


def _parse_env_file(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and not k.startswith("MEDULLA_"):
            out[k] = v.strip().strip('"').strip("'")
    return out


def _collect_dotenv(workflow: str | None) -> dict:
    """All three .env tiers forward WHOLE (owner decision: the user's zone,
    the engine is not a nanny). Merge order explicit and fixed:
    global < project < workflow — THE NEAREST TIER WINS on key conflict."""
    merged = _parse_env_file(Path.home() / ".medulla" / ".env")
    if workflow:
        # The PROJECT tier belongs to the repo you launch from — which for a shared
        # definition is not an ancestor of the yaml at all (it lives under $HOME).
        launch = Path.cwd().resolve()
        wdir = workflow_dir_for(Path(workflow)).resolve()
        seen = set()
        for base in list(reversed(launch.parents)) + [launch] + list(reversed(wdir.parents)):
            candidate = base / ".medulla" / ".env"
            if candidate in seen or candidate == Path.home() / ".medulla" / ".env":
                continue                                  # already the global tier
            seen.add(candidate)
            merged.update(_parse_env_file(candidate))
        # The WORKFLOW tier belongs to the definition, wherever it resolved to. Taking
        # it from the raw -w argument meant the flagship shared layout (no local file
        # at all) read this documented tier from a path that does not exist, and the
        # real ~/.medulla/workflows/<name>/.env was silently never forwarded.
        merged.update(_parse_env_file(wdir / ".env"))
    return merged


def _add_claude_token_fallback(env: dict) -> None:
    """Use the standard Claude profile token when no explicit token exists."""
    if os.environ.get(CLAUDE_TOKEN_KEY) or env.get(CLAUDE_TOKEN_KEY):
        return
    token_path = Path.home() / ".claude" / "token-home"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SystemExit(f"error: cannot read Claude OAuth token: {token_path}: {exc}") from exc
    if not token:
        return
    if "\n" in token or "\r" in token:
        raise SystemExit(f"error: Claude OAuth token must be one line: {token_path}")
    env[CLAUDE_TOKEN_KEY] = token




def _unlink_env_file() -> None:
    """The env-file holds merged provider tokens (0600 in $TMPDIR). Docker's
    client reads --env-file at startup, so the file is only needed until the
    run ends — remove it on EVERY exit path (finally + atexit belt): a timer
    thread dies with the process and leaks secrets on Ctrl-C / early return."""
    global env_file_for_run
    if env_file_for_run:
        try:
            os.unlink(env_file_for_run)
        except OSError:
            pass
        env_file_for_run = None


