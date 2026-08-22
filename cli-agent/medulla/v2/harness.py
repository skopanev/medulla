"""Harness adapters: turn an AgentSpec + prompt into a subprocess invocation, and
own the harness-specific stdout filtering (assistant text ONLY — tool output or
file reads echoing signal text must never route).

Battle lineage: argv shapes and traps are mined from two production
implementations — medulla v1 (cli-agent/medulla/executor.py) and pilot
(pilot/pilot/executors/*). Notable inherited scars:
- agy: `--print` CONSUMES THE NEXT TOKEN as the prompt — it must be the last
  flag; an untrusted workspace makes `--dangerously-skip-permissions` hang
  forever, so trust is preflighted (outside Docker).
- codex: `--full-auto` is deprecated AND sandboxed — never use it; prompt goes
  via stdin (no ARG_MAX, no @file coupling).
- claude: ANTHROPIC_API_KEY is stripped (OAuth account must win); prompt is
  delivered as a system-prompt FILE, `-p "Execute."` stays tiny.
- opencode: permissions/effort/timeout live in opencode.json, bootstrapped
  idempotently (never clobbering an author's config).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import E_HARNESS, EngineCrash
from .model import SANDBOX_LEVELS, AgentSpec


def _read_only(spec: AgentSpec) -> bool:
    """Does this step ask to be unable to write?

    Default is off: under --docker the container IS the sandbox, and every workflow
    written before this field existed relies on that. Tightening the default would
    silently change them; a step that wants less power asks for less.

    Worth having because "the container is the sandbox" stops being enough the moment
    a workflow feeds the model UNTRUSTED text — mail, chat logs, scraped pages — while
    the workspace is mounted read-write. Then the blast radius of an injected
    instruction is exactly the data the pipeline exists to protect.

    A typo raises instead of falling through: a sandbox quietly weaker than the one
    asked for is the single failure this field exists to prevent.
    """
    sb = (spec.sandbox or "danger").strip()
    if sb not in SANDBOX_LEVELS:
        raise EngineCrash(E_HARNESS,
                          f"agent.sandbox: {sb!r} is not one of {list(SANDBOX_LEVELS)}")
    return sb == "read-only"

REAL_HARNESSES = ("claude-code", "codex", "opencode", "agy")

import re as _re

_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_LINE_START_SIGNAL_RE = _re.compile(
    r"(?ms)^[ \t]*(<signal:([a-zA-Z0-9_-]+)[^>]*>.*?</signal:\2>)")


def plain_text_signal_filter(stdout: str) -> str:
    """Defense for CLIs WITHOUT structured output (opencode, agy): keep only
    signal tags that START a line. Tool output echoing a tag mid-line
    ("$ cat notes.md: <signal:done>...") is dropped — an identity filter let
    any cat'd file route the graph (audit R3). Residual risk: a file whose
    line IS a bare tag still leaks; the structured filter (part-7 live logs)
    is the real fix, this is the best available heuristic until then."""
    return "\n".join(m.group(1) for m in _LINE_START_SIGNAL_RE.finditer(stdout))

# extra slack for a CLI's INNER timeout so the engine's own timeout always
# fires first and the CLI limit is just a net (v1 convention)
INNER_SLACK_S = 300


@dataclass
class Invoke:
    argv: list[str]
    stdin: str | None = None                      # piped to the child's stdin
    env: dict[str, str] = field(default_factory=dict)   # merged over the engine env
    env_remove: list[str] = field(default_factory=list)  # stripped from the child env
    merge_stderr: bool = False    # CLIs that talk on stderr (opencode); the filter
                                  # still gates what can route

    # Linux caps a SINGLE argv string at MAX_ARG_STRLEN (131072 bytes) — exceed it
    # and execve fails with E2BIG before the harness ever runs. Every adapter funnels
    # through here, so the tripwire catches the regression class once, not per-CLI.
    _MAX_ARGV_STR_BYTES = 100_000

    def __post_init__(self):
        for i, a in enumerate(self.argv):
            n = len(a.encode("utf-8", "surrogatepass"))
            if n >= self._MAX_ARGV_STR_BYTES:
                raise EngineCrash(
                    E_HARNESS,
                    f"Invoke: argv[{i}] is {n} bytes (>= {self._MAX_ARGV_STR_BYTES}); "
                    f"a single argv string this large will crash the child with E2BIG "
                    f"(Linux MAX_ARG_STRLEN=131072). Big payloads must ride stdin, not argv.")


class HarnessAdapter:
    name = "abstract"
    binary = ""            # shutil.which target; "" skips the check (fake)

    def __init__(self):
        if self.binary and not shutil.which(self.binary):
            raise EngineCrash(
                E_HARNESS, f"harness '{self.name}': binary '{self.binary}' not on PATH")

    def prepare(self, spec: AgentSpec, workdir: Path) -> None:
        """Idempotent preflight/setup before a phase's first attempt. May raise
        E_HARNESS only for unresolvable conditions (the razor)."""

    def build(self, spec: AgentSpec, prompt_file: Path, prompt_text: str,
              timeout_s: float) -> Invoke:
        raise NotImplementedError

    def filter_stdout(self, stdout: str) -> str:
        """Reduce raw CLI output to signal-scannable assistant text."""
        return stdout

    def extract_error(self, stdout: str) -> str | None:
        """Harness-specific failure detail mined from raw stdout (NOT the signal
        channel — appended to the __failed__ message only). Default: none."""
        return None

    def stream_line(self, line: str) -> str | None:
        """Operator-facing live rendering of ONE raw output line (v1 streamed,
        v2 was silent for 30-minute runs — spar panel/audit). Returns display
        text or None to hide. NEVER feeds the signal scanner — display only."""
        line = _ANSI_RE.sub("", line.rstrip())
        if not line or line.lstrip().startswith("<signal:"):
            return None
        return line

    def retry_pointless(self, stdout: str) -> str | None:
        """A failure that WILL repeat until something outside the run changes — a plan
        limit, an exhausted quota. Distinct from fatal_error: that one crashes the whole
        run, which is wrong for a pool where the other inputs are fine. This one just
        stops burning attempts on this input and names the reason; a fallback agent, if
        declared, still gets its turn. Default: none."""
        return None

    def fatal_error(self, stdout: str) -> str | None:
        """A DETERMINISTIC environment failure (not logged in, invalid key):
        retrying is pointless, the whole run must crash E_HARNESS with a clear
        message — the same razor call as agy's untrusted-workspace preflight.
        Found live: an unauthenticated claude burned 2 attempts x 15 inputs.
        Default: none."""
        return None


# ── fake (tests) ─────────────────────────────────────────────────────────────

class FakeAdapter(HarnessAdapter):
    """agent: {harness: fake, model: path/to/script.sh} — the script receives the
    rendered prompt file as $1 (plus rendered args) and behaves as configured."""
    name = "fake"

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        if not spec.model:
            raise EngineCrash(E_HARNESS, "fake harness: model must be a script path")
        return Invoke(argv=["bash", spec.model, str(prompt_file), *spec.args])


# ── claude-code ──────────────────────────────────────────────────────────────

class ClaudeAdapter(HarnessAdapter):
    name = "claude-code"
    binary = "claude"

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        # sandbox → claude permission modes. `plan` is the only mode that refuses
        # edits outright, so it is what read-only means here.
        ro = _read_only(spec)
        perm = ["--permission-mode", "plan"] if ro else ["--dangerously-skip-permissions"]
        argv = ["claude", *perm,
                "--output-format", "stream-json", "--verbose"]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]   # low|medium|high|xhigh|max (claude --help)
        argv += ["--append-system-prompt-file", str(prompt_file)]
        argv += spec.args
        argv += ["-p", "Execute."]
        inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
        return Invoke(argv=argv,
                      env={"API_TIMEOUT_MS": str(inner_ms)},
                      env_remove=["ANTHROPIC_API_KEY"])   # the OAuth account must win

    def filter_stdout(self, stdout: str) -> str:
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue                    # non-JSON preamble is never assistant text
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif etype == "result":
                raw = event.get("result", "")
                if isinstance(raw, str):
                    parts.append(raw)
                elif isinstance(raw, dict):         # dict-shaped final result (pilot scar):
                    parts.append(raw.get("output", ""))  # dropping it = permanent __default__
            # user messages (tool_result), tool_use blocks, system events: SKIP
        return "\n".join(p for p in parts if p)

    def retry_pointless(self, stdout: str) -> str | None:
        # "You've hit your weekly limit · resets Aug 21, 3pm (UTC)" arrives as a normal
        # error result with api_error_status 429, so the engine saw only "body died:
        # rc=1" and spent a second attempt on something that cannot change until the reset.
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    kind = str(info.get("rateLimitType") or "rate").replace("_", "-")
                    return f"claude-code: {kind} plan limit reached — retrying cannot help"
            if event.get("type") == "result" and event.get("api_error_status") == 429:
                return f"claude-code: {str(event.get('result', 'rate limited')).strip()}"
        return None

    def fatal_error(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and event.get("is_error"):
                text = str(event.get("result", ""))
                if "Not logged in" in text or "Invalid API key" in text \
                        or "/login" in text:
                    return f"claude-code is not authenticated: {text!r} — " \
                           f"run `claude /login` (in docker: keychain-bound OAuth " \
                           f"does not reach the container; use CLAUDE_CODE_OAUTH_TOKEN)"
        return None

    def stream_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") == "assistant":
            texts = [b.get("text", "") for b in event.get("message", {}).get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            out = " ".join(x for x in texts if x).strip()
            return out or None
        return None


# ── codex ────────────────────────────────────────────────────────────────────

class CodexAdapter(HarnessAdapter):
    name = "codex"
    binary = "codex"

    def __init__(self):
        # A broker wrapper may stand in for codex (the container's `codex` can be
        # a shim into it), so availability is judged by the name the workflow
        # actually invokes — nothing here knows or cares about wrapper names.
        if not shutil.which("codex"):
            raise EngineCrash(
                E_HARNESS,
                f"harness '{self.name}': 'codex' not on PATH")

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        bin_ = "codex"
        inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
        # sandbox: codex has native modes, so this maps exactly. The historical
        # default stays `danger` — the container IS the sandbox for most workflows,
        # and tightening it silently would break every existing one.
        ro = _read_only(spec)
        argv = [bin_, "exec", "--json", "--skip-git-repo-check"]
        argv.insert(3, "-s" if ro else "--dangerously-bypass-approvals-and-sandbox")
        if ro:
            argv.insert(4, "read-only")
        if spec.model:
            argv += ["-c", f'model="{spec.model}"']
        if spec.effort:
            argv += ["-c", f"model_reasoning_effort={spec.effort}"]
        argv += ["-c", f"stream_idle_timeout_ms={inner_ms}"]
        argv += spec.args                        # last -c wins: authors can override
        # stdin carries the COMPLETE prompt (no "Execute." prefix needed — that was
        # v1's convention for @file references); no ARG_MAX, no @-expansion coupling
        return Invoke(argv=argv, stdin=prompt_text)

    def extract_error(self, stdout: str) -> str | None:
        # codex reports real failure causes as stdout JSON events that the signal
        # filter rightly drops; without this, a turn.failed run yields a useless
        # __failed__ message (pilot scar: "0 output, exit 1")
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("error", "turn.failed") or event.get("error"):
                detail = event.get("message") or event.get("error") or event
                if isinstance(detail, (dict, list)):
                    detail = json.dumps(detail, ensure_ascii=False)
                return f"codex {event.get('type', 'error')}: {detail}"
        return None

    def filter_stdout(self, stdout: str) -> str:
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                parts.append(item.get("text", ""))
            # command_execution aggregated_output is TOOL OUTPUT — never scanned.
            # (v1's <500-char exception violated the contract; dropped.)
        return "\n".join(p for p in parts if p)

    def stream_line(self, line: str) -> str | None:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            return item.get("text") or None
        if event.get("type") == "item.started" and item.get("type") == "command_execution":
            return f"$ {item.get('command', '')}"     # progress, pilot-style
        return None


# ── opencode ─────────────────────────────────────────────────────────────────

class OpenCodeAdapter(HarnessAdapter):
    name = "opencode"
    binary = "opencode"

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        # --format json for the same reason as agy: the default buffers the answer and
        # hands it over at the end, so a working panelist looks like a hung one
        # (measured on opencode 1.18.19: events at +2s/+3s versus one block at the
        # close). It also ends the guessing — the answer arrives as `text` parts, so
        # tool output can no longer be mistaken for a signal.
        argv = ["opencode", "run", "--format", "json", "--agent", "build"]
        if spec.model:
            argv += ["-m", spec.model]
        argv += spec.args
        # The prompt rides STDIN, not positional argv — as in CodexAdapter. Linux
        # caps a SINGLE argv string at MAX_ARG_STRLEN=131072 bytes, so a positional
        # prompt made every run with a >128KB prompt die with E2BIG. Model/effort
        # flags stay on argv: they are tiny.
        # Config rides in OPENCODE_CONFIG_CONTENT (ported from a parallel v1 fix,
        # main@217f751): the old on-disk opencode.json lingered in the workdir,
        # was stale-reused across runs, needed a TOCTOU lock under parallel
        # pools, and forced one shared config per workdir. The env layers on
        # top of any real project config and is naturally PER-INVOCATION —
        # heterogeneous per-input efforts now just work.
        # sandbox → opencode permissions. It has no read-only flag; the config is
        # the only lever, so deny the tools that mutate. bash is denied for
        # read-only because a shell is a write primitive.
        ro = _read_only(spec)
        # bash is denied too: a shell is a write primitive, so allowing it would make
        # read-only a label rather than a constraint.
        perm: object = ({"edit": "deny", "write": "deny", "patch": "deny", "bash": "deny"}
                        if ro else "allow")
        data: dict = {"$schema": "https://opencode.ai/config.json", "permission": perm}
        if spec.model and "/" in spec.model:
            provider, model_id = spec.model.split("/", 1)
            inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
            pblock: dict = {"options": {"timeout": inner_ms}}
            if spec.effort:
                pblock["models"] = {model_id: {"options": {"reasoningEffort": spec.effort}}}
            data["provider"] = {provider: pblock}
        # opencode's run mode emits EVERYTHING (assistant text included) on
        # stderr with ANSI decoration; --format json is half-alive on 1.15.5
        # (single step_start event, rc 0 — probed live). Pilot's scar: merge
        # the streams, then filter hard.
        return Invoke(argv=argv, stdin=prompt_text, merge_stderr=True,
                      env={"OPENCODE_CONFIG_CONTENT": json.dumps(data)})

    def stream_line(self, line: str) -> str | None:
        """One JSON event -> what a watcher should see, or nothing."""
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if ev.get("type") == "text":
            return ((ev.get("part") or {}).get("text") or "").rstrip("\n") or None
        return None

    def filter_stdout(self, stdout: str) -> str:
        """Signals come from what opencode SAID, never from what it printed.

        With --format json the answer arrives as `text` parts, so the line-start
        heuristic — the ceiling while opencode had no structured output — is no longer
        the best available. Anything that is not JSON at all still falls back to it,
        for an older opencode or for plain text on stderr.
        """
        answer, saw_json = [], False
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            # OUR events only. Any line starting with `{` used to count, so an agent
            # that printed a JSON snippet in its answer disarmed the fallback and every
            # signal in that answer was dropped.
            if ev.get("type") in ("text", "step_start", "step_finish", "tool_use"):
                saw_json = True
            if ev.get("type") == "text":
                text = (ev.get("part") or {}).get("text")
                if text:
                    answer.append(text)
        if answer:
            return "".join(answer)
        return "" if saw_json else plain_text_signal_filter(stdout)


_AGY_SETTINGS = "~/.gemini/antigravity-cli/settings.json"

# convenience slugs -> exact `agy models` names (effort lives in the suffix)
AGY_MODEL_ALIASES = {
    "gemini-3.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
}


def _agy_trusted(workdir: Path) -> bool:
    try:
        with open(os.path.expanduser(_AGY_SETTINGS)) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    cwd = os.path.realpath(workdir)
    for root in data.get("trustedWorkspaces") or []:
        if not isinstance(root, str):
            continue
        root = os.path.realpath(os.path.expanduser(root))
        if cwd == root or cwd.startswith(root + os.sep):
            return True
    return False


class AgyAdapter(HarnessAdapter):
    name = "agy"
    binary = "agy"

    def prepare(self, spec, workdir):
        # Untrusted workspace makes --dangerously-skip-permissions HANG waiting for
        # interactive trust — deterministic, environment-level, unresolvable at
        # runtime: the E_HARNESS razor's spirit ("unresolvable"), not a flake.
        # In Docker the container is the sandbox and trust files are unreliable;
        # --print-timeout bounds any residual hang, so the guard is host-only.
        if os.environ.get("MEDULLA_DOCKER") == "1":
            return
        if not _agy_trusted(workdir):
            raise EngineCrash(
                E_HARNESS,
                f"agy: workspace '{workdir}' is not in trustedWorkspaces — "
                f"`agy --dangerously-skip-permissions` would hang forever. Trust it "
                f"once (open `agy` there interactively, or add the path to "
                f"\"trustedWorkspaces\" in {_AGY_SETTINGS}).")

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        # sandbox → agy `--mode plan`, exactly as claude maps it to `--permission-mode
        # plan`. This used to raise "not expressible": true when agy offered only the
        # boolean `--sandbox` (terminal restrictions, no write protection), stale since
        # `--mode (accept-edits, plan)` landed. Plan mode refuses writes at the CLI's
        # permission layer — the write RPC is rejected, not merely discouraged — so it is
        # a real lock, not a polite request (verified on agy 1.1.17: the file was never
        # created). Refusing a read-only step on a harness that supports it pushed panels
        # onto other models for no reason.
        ro = _read_only(spec)
        # stream-json, not the default text: `text` buffers the whole answer and hands
        # it over at the very end, so a live run is indistinguishable from a hung one —
        # measured on agy 1.1.18, every line landed at +15s together, while stream-json
        # emitted at +4s, +5s, +13s, +16s. It also gives agy the structured output this
        # file elsewhere says it lacks: the final text arrives as result.response
        # instead of being guessed out of console prose.
        argv = ["agy",
                *(["--mode", "plan"] if ro else ["--dangerously-skip-permissions"]),
                "--output-format", "stream-json",
                "--print-timeout", f"{int(timeout_s) + INNER_SLACK_S}s"]
        if spec.model:
            argv += ["--model", AGY_MODEL_ALIASES.get(spec.model, spec.model)]
        argv += spec.args
        # --print MUST be last: it consumes the next token as the prompt value.
        # Any flag placed after it silently becomes the prompt (verified v1.0.4).
        argv += ["--print", prompt_text]
        return Invoke(argv=argv)

    def stream_line(self, line: str) -> str | None:
        """One NDJSON event -> what a watcher should see, or nothing."""
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        kind = ev.get("event")
        if kind == "step_update":
            step = ev.get("step_update") or {}
            if step.get("step_type") == "agent_response":
                return (step.get("text_delta") or "").rstrip("\n") or None
            # Tools, because answer text is scarce while real work happens: one panel
            # run emitted 54 agent_response events carrying only 2 non-empty deltas,
            # against 104 tool steps. Text alone would leave a working agent looking
            # idle for minutes — the very thing streaming is here to fix.
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                info = step.get("tool_info") or {}
                params = info.get("parameters") or {}
                cmd = params.get("CommandLine") or params.get("command")
                name = step.get("tool_name") or info.get("name") or "tool"
                return f"$ {cmd}" if cmd else f"[{name}]"
            return None
        if kind == "result":
            status = (ev.get("result") or {}).get("status")
            return None if status == "SUCCESS" else f"agy: {status}"
        return None

    def filter_stdout(self, stdout: str) -> str:
        """Signals come from what agy SAID, never from what it printed.

        With --output-format stream-json the answer arrives as result.response, so the
        old line-start heuristic — the ceiling while agy had no structured output — is
        no longer the best available: tool output and console prose can no longer be
        mistaken for a signal, exactly as with claude-code and codex.
        """
        answer, saw_json = [], False
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") in ("init", "step_update", "result"):
                saw_json = True     # OUR events only — see OpenCodeAdapter.filter_stdout
            if ev.get("event") == "result":
                res = ev.get("result") or {}
                # A FAILED turn can still carry a response, and a signal mined out of it
                # would route the graph as though the turn had succeeded.
                if res.get("status") not in (None, "SUCCESS"):
                    return ""
                text = res.get("response")
                if text:
                    return text
            elif ev.get("event") == "step_update":
                step = ev.get("step_update") or {}
                if step.get("step_type") == "agent_response" and step.get("text_delta"):
                    answer.append(step["text_delta"])
        if answer:
            return "".join(answer)        # no result event (killed mid-turn): use the deltas
        # Not stream-json at all — an older agy, or plain text on stderr. Fall back to
        # the heuristic rather than returning nothing.
        return "" if saw_json else plain_text_signal_filter(stdout)


# ── registry ─────────────────────────────────────────────────────────────────

_ADAPTERS = {
    FakeAdapter.name: FakeAdapter,
    ClaudeAdapter.name: ClaudeAdapter,
    CodexAdapter.name: CodexAdapter,
    OpenCodeAdapter.name: OpenCodeAdapter,
    AgyAdapter.name: AgyAdapter,
}
_instances: dict[str, HarnessAdapter] = {}


def resolve(spec: AgentSpec) -> HarnessAdapter:
    cls = _ADAPTERS.get(spec.harness)
    if cls is None:
        raise EngineCrash(E_HARNESS, f"unknown harness '{spec.harness}'")
    if spec.harness not in _instances:
        _instances[spec.harness] = cls()        # binary check happens here (E_HARNESS)
    return _instances[spec.harness]


def reset_registry() -> None:
    """Test hook: drop cached instances (binary availability may change per test)."""
    _instances.clear()
