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
  via stdin (no ARG_MAX, no @file coupling); prefer the `cx` token-refreshing
  wrapper when present.
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

from .errors import EngineCrash, E_HARNESS, E_INTERNAL
from .model import AgentSpec

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
        argv = ["claude", "--dangerously-skip-permissions",
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
        # cx (the token-refreshing wrapper) alone is a valid install (audit G9):
        # a slim image with only cx must not crash E_HARNESS at boot
        if not (shutil.which("cx") or shutil.which("codex")):
            raise EngineCrash(E_HARNESS,
                              "harness 'codex': neither 'cx' nor 'codex' on PATH")

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        bin_ = shutil.which("cx") or "codex"    # cx refreshes the token via the broker
        inner_ms = (int(timeout_s) + INNER_SLACK_S) * 1000
        argv = [bin_, "exec", "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check"]
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
    _bootstrap_lock = __import__("threading").Lock()

    def prepare(self, spec, workdir):
        # The adapter is a cached singleton shared by pool workers: check-then-write
        # over a shared path must be locked, and the write atomic (tmp + replace) —
        # a concurrent opener must never see a truncated config. First writer wins;
        # per-input effort in one workdir shares one config by design (opencode
        # reads config per cwd — heterogeneous efforts need per-input workdirs).
        with self._bootstrap_lock:
            cfg = workdir / "opencode.json"
            if cfg.exists():
                return                           # never clobber an author's config
            data: dict = {"$schema": "https://opencode.ai/config.json", "permission": "allow"}
            if spec.model and "/" in spec.model:
                provider, model_id = spec.model.split("/", 1)
                pblock: dict = {"options": {"timeout": 3600000}}   # default 5m kills long runs
                if spec.effort:
                    pblock["models"] = {model_id: {"options": {"reasoningEffort": spec.effort}}}
                data["provider"] = {provider: pblock}
            tmp = workdir / ".opencode.json.tmp"
            tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
            os.replace(tmp, cfg)

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        argv = ["opencode", "run", "--agent", "build"]
        if spec.model:
            argv += ["-m", spec.model]
        argv += spec.args
        argv.append(prompt_text)                 # positional prompt (argv list, no shell)
        # opencode's run mode emits EVERYTHING (assistant text included) on
        # stderr with ANSI decoration; --format json is half-alive on 1.15.5
        # (single step_start event, rc 0 — probed live). Pilot's scar: merge
        # the streams, then filter hard.
        return Invoke(argv=argv, merge_stderr=True)

    def filter_stdout(self, stdout: str) -> str:
        # merged stream with ANSI decoration: strip escapes, then the
        # line-start heuristic gates routing (--format json probed half-alive
        # on 1.15.5 — a single step_start event; revisit on a newer opencode)
        return plain_text_signal_filter(_ANSI_RE.sub("", stdout))


# ── agy (Antigravity) ────────────────────────────────────────────────────────

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
        argv = ["agy", "--dangerously-skip-permissions",
                "--print-timeout", f"{int(timeout_s) + INNER_SLACK_S}s"]
        if spec.model:
            argv += ["--model", AGY_MODEL_ALIASES.get(spec.model, spec.model)]
        argv += spec.args
        # --print MUST be last: it consumes the next token as the prompt value.
        # Any flag placed after it silently becomes the prompt (verified v1.0.4).
        argv += ["--print", prompt_text]
        return Invoke(argv=argv)

    def filter_stdout(self, stdout: str) -> str:
        # agy has no structured output mode at all — the line-start heuristic
        # is the ceiling of what an adapter can do here.
        return plain_text_signal_filter(stdout)


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
