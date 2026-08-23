"""What every harness adapter shares: the call it builds, and the contract it obeys.

Split from harness.py under the project's 250-line rule ($MAX_LOC). One adapter per
file now — a CLI's quirks are its own business, and reading claude's should not mean
scrolling past agy's.
"""
from __future__ import annotations

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


