"""Normalized v2 pipeline model. The contract lives in README.md; this mirrors it 1:1."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = "__exit_ok__"
EXIT_FAIL = "__exit_fail__"
TERMINALS = (EXIT_OK, EXIT_FAIL)

SIG_DONE = "__done__"
SIG_FAILED = "__failed__"
SIG_EMPTY = "__empty__"
SIG_DEFAULT = "__default__"
ENGINE_FACTS = (SIG_DONE, SIG_FAILED, SIG_EMPTY, SIG_DEFAULT)

CHANNEL_SIGNALS = ("var", "update")  # reserved bare words; never routable

BOOLEAN_TRAP_NAMES = {"on", "off", "yes", "no", "true", "false"}

DEFAULT_ACTION_TIMEOUT = 1800
DEFAULT_PIPELINE_TIMEOUT = 86400
DEFAULT_SOURCE_TIMEOUT = 60
HOOK_TIMEOUT_S = 60                 # pre/post are one-line artifact tests, not workloads
DEFAULT_KEEP_RUNS = 20
INPUTS_HARD_CAP = 10_000
TIMEOUT_RC = 124  # timeout is recognizable as rc 124 (contract)

# vars are exported to child env; these must never be clobbered
ENV_BLACKLIST_EXACT = {"PATH", "HOME", "SHELL", "USER", "TMPDIR", "LANG", "TERM", "PWD"}
ENV_BLACKLIST_PREFIX = ("LD_", "DYLD_", "PYTHON", "MEDULLA_")

# defaults: may hold only flat policy scalars + fallback + on_signal (per-key)
DEFAULTS_ALLOWED_KEYS = {"timeout", "max_attempts", "ignore_exit_code", "fallback", "on_signal"}


@dataclass
class AgentSpec:
    harness: str
    model: str | None = None
    effort: str | None = None
    args: list[str] = field(default_factory=list)


@dataclass
class Action:
    """One unit of work: shell XOR agent(+prompt), plus execution policy."""
    shell: str | None = None
    agent: AgentSpec | None = None
    prompt: str | None = None
    timeout: int | None = None          # per attempt; None -> defaults -> 1800
    max_attempts: int | None = None     # per runner; None -> defaults -> 1
    ignore_exit_code: bool | None = None
    fallback: "Action | None" = None    # agent-only; a fallback has no fallback

    @property
    def kind(self) -> str:
        return "shell" if self.shell is not None else "agent"


@dataclass
class InputsSpec:
    data: list | None = None            # YAML list = data
    shell: str | None = None            # {shell: cmd} = source
    shell_timeout: int = DEFAULT_SOURCE_TIMEOUT


@dataclass
class Pool:
    inputs: InputsSpec
    max_parallel: int | None = 1        # None = all
    min_success: int | None = None      # None = all


@dataclass
class Node:
    name: str
    action: Action
    pool: Pool | None = None
    pre: str | None = None              # shell hook, once per node run, before body render
    post: str | None = None             # shell hook, after every attempt, before resolution
    on_signal: dict[str, str] = field(default_factory=dict)

    @property
    def is_pool(self) -> bool:
        return self.pool is not None


@dataclass
class Defaults:
    timeout: int | None = None
    max_attempts: int | None = None
    ignore_exit_code: bool | None = None
    fallback: Action | None = None
    on_signal: dict[str, str] = field(default_factory=dict)


@dataclass
class Pipeline:
    version: str
    start: str
    nodes: dict[str, Node]
    vars: dict[str, str] = field(default_factory=dict)
    timeout: int | None = DEFAULT_PIPELINE_TIMEOUT  # whole-run deadline; None = unlimited (yaml: 0)
    keep_runs: int = DEFAULT_KEEP_RUNS
    defaults: Defaults = field(default_factory=Defaults)
    path: Path | None = None            # source file
    dir: Path | None = None             # pipeline dir (parent of pipeline.yaml)

    def action_timeout(self, action: Action) -> int:
        if action.timeout is not None:
            return action.timeout
        if self.defaults.timeout is not None:
            return self.defaults.timeout
        return DEFAULT_ACTION_TIMEOUT

    def action_max_attempts(self, action: Action) -> int:
        if action.max_attempts is not None:
            return action.max_attempts
        if self.defaults.max_attempts is not None:
            return self.defaults.max_attempts
        return 1

    def action_ignore_exit_code(self, action: Action) -> bool:
        if action.ignore_exit_code is not None:
            return action.ignore_exit_code
        if self.defaults.ignore_exit_code is not None:
            return self.defaults.ignore_exit_code
        return False

    def action_fallback(self, action: Action) -> Action | None:
        if action.fallback is not None:
            return action.fallback
        return self.defaults.fallback

    def known_signals(self, node: Node) -> set[str]:
        """Signals with a route available for this node (node keys + defaults keys)."""
        return set(node.on_signal) | set(self.defaults.on_signal)

    def resolve_route(self, node: Node, signal: str) -> str | None:
        """node on_signal -> defaults.on_signal -> built-ins (engine facts -> __exit_fail__)."""
        if signal in node.on_signal:
            return node.on_signal[signal]
        if signal in self.defaults.on_signal:
            return self.defaults.on_signal[signal]
        if signal in (SIG_FAILED, SIG_EMPTY, SIG_DEFAULT):
            return EXIT_FAIL
        return None  # __done__ and user signals have no built-in
