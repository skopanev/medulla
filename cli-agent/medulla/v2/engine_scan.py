"""Reading a body's stdout, and the environment a body runs in.

Split from engine.py under the project's 250-line rule ($MAX_LOC). Nothing here knows
about the graph: it turns raw output into facts (signals, vars, updates) and builds the
environment those facts come from. The Engine decides what the facts MEAN.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import E_INPUTS, EngineCrash
from .model import CHANNEL_SIGNALS, SIG_DEFAULT, SIG_FAILED
from .signals import extract_signals


def log(msg: str) -> None:
    print(f"[medulla] {msg}", file=sys.stderr)


def _tail(text: str, n: int = 400) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text


def _retry_delay() -> None:
    """Fixed pause between attempts (pilot's battle scar: 2s beats a rate-limit
    storm). Env-tunable so tests run at 0."""
    delay = float(os.environ.get("MEDULLA_RETRY_DELAY_S", "2"))
    if delay > 0:
        time.sleep(delay)


def _timeout_env(seconds: float) -> str:
    """Env representation of a clamped timeout: never "0" for a live budget —
    an agent CLI sizing its own timeout from this must not read "no limit"."""
    return str(max(1, int(round(seconds))))


def _input_hash(value) -> str:
    """Stable input identity for resume/idempotency. Python's hash() is salted."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _sniff_inputs(stdout: str, node_name: str) -> list:
    """First non-blank byte decides: '[' JSON array, '{' JSON-lines, else plain lines."""
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngineCrash(E_INPUTS, f"inputs source: broken JSON array: {exc}",
                              node=node_name)
        if not isinstance(data, list):
            raise EngineCrash(E_INPUTS, "inputs source: JSON is not an array", node=node_name)
        return data
    if text.startswith("{"):
        rows = []
        for n, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EngineCrash(E_INPUTS, f"inputs source: broken JSON on line {n}: {exc}",
                                  node=node_name)
        return rows
    return [line.strip() for line in text.splitlines() if line.strip()]


# ── structured signal scan (foundation for pool manifests) ──────────────────

@dataclass
class ScanResult:
    first_known: str | None = None
    first_body: str = ""
    vars: dict[str, str] = field(default_factory=dict)
    updates: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)   # EVERY signal, stdout order


def scan_stdout(stdout: str, known: set[str] | None,
                strict: bool = False) -> ScanResult:
    """stdout only — stderr never routes. Engine facts are excluded from `known`
    upstream: a body printing <signal:__failed__> must never route (namespace law).

    known=None is pool mode: record the first ANY bare user signal (pool routing
    tables hold only dunders, yet body signals must reach the manifest)."""
    res = ScanResult()
    for name, attrs, body in extract_signals(stdout, strict=strict):
        event = {"name": name, "message": _tail(body, 2000)}
        if name == "var" and (attrs or {}).get("key"):
            event["key"] = attrs["key"]
        res.events.append(event)      # the journal's full record: routing rules
                                      # below stay untouched (namespace law)
        if name == "update":
            res.updates.append(body)
            continue
        if name == "var":
            key = (attrs or {}).get("key", "")
            if key and body:
                res.vars[key] = body
            continue
        if res.first_known is not None:
            continue
        if known is None:
            if name not in CHANNEL_SIGNALS and not name.startswith("__"):
                res.first_known, res.first_body = name, body
        elif name in known:
            res.first_known, res.first_body = name, body
    return res


@dataclass
class AttemptsOutcome:
    signal: str | None               # user signal | __failed__ | __default__ | None (pool silent ok)
    message: str
    attempts: int                    # total executions across phases (0 = pre decided)
    attempts_primary: int = 0
    attempts_fallback: int = 0
    rc: int | None = None
    timed_out: bool = False
    fallback_used: bool = False
    concluding_phase: str | None = None   # "primary" | "fallback" | None (pre decided)
    harness: str | None = None            # harness that produced the concluding outcome
    model: str | None = None
    guarded: bool = False                 # pre emitted a routing signal; body never ran
    failure_class: str | None = None      # for __failed__: "pre" | "rc" | "timeout" | "post"
    recorded_signal: str | None = None    # pool: first bare signal seen (data, never outcome)
    recorded_body: str = ""
    pending_vars: dict[str, str] = field(default_factory=dict)  # fold law: caller applies
    updates: list[str] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)  # every produced signal (concluding path)


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith("MEDULLA_"):
            log(f"warn: .env key '{key}' ignored (engine namespace)")
            continue
        out[key] = value
    return out


def load_dotenv(workflow_dir: Path, launch_dir: Path | None = None) -> dict[str, str]:
    """Secrets channel for bodies/hooks: NOT vars — never templated, never
    persisted. Three tiers, nearest wins:
      ~/.medulla/.env            global (machine-wide provider tokens)
      <project>/.medulla/.env    per-project (walk up from the workflow dir)
      <workflow>/.env            per-workflow
    """
    merged: dict[str, str] = {}
    merged.update(_parse_dotenv(Path.home() / ".medulla" / ".env"))
    # BOTH chains, never one: walking up only from the definition skipped the project
    # entirely for a machine-wide workflow (it lives under $HOME), and walking up only
    # from the launch dir dropped the .medulla/.env of a workflow nested BELOW the launch
    # dir — a directory that is a descendant of cwd, not an ancestor. Launch first, the
    # definition's own ancestors last: nearest to the workflow wins.
    launch = (launch_dir or Path.cwd()).resolve()
    wdir = workflow_dir.resolve()
    seen: set[Path] = set()
    for base in list(reversed(launch.parents)) + [launch] + list(reversed(wdir.parents)):
        candidate = base / ".medulla" / ".env"
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and candidate != Path.home() / ".medulla" / ".env":
            merged.update(_parse_dotenv(candidate))
    merged.update(_parse_dotenv(workflow_dir / ".env"))
    return merged


def conclusion_message(signal, action, result, total, limit_reason, fallback_used,
                       post_signal, post_scan, body_scan, known, *, agent_spec=None,
                       post_rc=None, post_stderr=""):
    """The human-facing sentence for how an attempt ended.

    Lives beside scan_stdout because every branch here is explaining what the scanner
    did or did not find, and because the retry loop was over the project's line limit
    carrying prose it does not otherwise need.
    """
    from .harness import resolve as resolve_harness
    if signal == SIG_FAILED:
        if post_rc and not result.rc and not result.timed_out:
            # Only when the body SURVIVED. When the body itself died, its stderr is
            # the evidence — a credential broker's stack trace, an OAuth prompt that
            # timed out — and the hook merely noticed the missing artifact afterwards.
            # Reporting the hook there would replace the cause with its symptom.
            # This branch is for the other case: the body delivered and exited 0, and
            # saying "body died: rc=0" is a contradiction that sent readers hunting a
            # crash that never happened while the artifact sat on disk, complete.
            return (f"post hook vetoed the attempt: rc={post_rc}, {total} attempt(s)"
                    f"{' (fallback tried)' if fallback_used else ''}; "
                    f"body rc={result.rc}; stderr: {_tail(post_stderr)}")
        if limit_reason:
            # Name the wall. "body died: rc=1" for an exhausted plan sent a
            # panel round down the wrong path more than once.
            message = (f"{limit_reason} ({total} attempt(s)"
                       f"{', fallback tried' if fallback_used else ''})")
        else:
            message = (f"body died: rc={result.rc}, {total} attempt(s)"
                       f"{' (fallback tried)' if fallback_used else ''}; "
                       f"stderr: {_tail(result.stderr)}")
        if action.kind == "agent":
            # harness-mined failure detail (codex error/turn.failed lives in
            # stdout JSON the signal filter rightly drops)
            detail = resolve_harness(agent_spec).extract_error(result.stdout)
            if detail:
                message += f"; {detail}"
    elif signal == SIG_DEFAULT:
        # "the node did its job, wrote its file, printed nothing, and failed"
        # is correct but startling — and it is only discoverable by reading the
        # journal. Name the rule in the message that reports it.
        message = ("no known signal emitted, so the node took __default__ "
                   "(route it, or print a signal, or the run fails here); "
                   f"stdout: {_tail(result.stdout)}")
        # A shell node whose signal sits mid-line used to be rescued by the
        # lenient parse, which is the same leniency that let an interpolated
        # value become a variable. Say what changed instead of leaving the
        # author with a node that "just stopped routing".
        if action.kind == "shell" and "<signal:" in result.stdout \
                and not scan_stdout(result.stdout, known).events == []:
            message += ("; note: a shell node's signal must start its own line "
                        "— a tag printed mid-line is treated as text")
    elif signal is None:
        message = ""                        # pool silent ok
    elif post_signal is not None and signal == post_signal:
        message = post_scan.first_body
    else:
        message = body_scan.first_body
    return message
