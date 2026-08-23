"""Which harness runs a node, and the registry that names them.

The adapters live one per file in v2/harnesses/ — split under the project's 250-line
rule ($MAX_LOC). Everything they share is in v2/harness_base.py, which they import;
this module only names them and hands one back.

Re-exports the base so the many `from .harness import Invoke` call sites — and the
suite, which reaches every adapter through this module — keep working unchanged.
"""
from __future__ import annotations

from .errors import E_HARNESS, EngineCrash  # noqa: F401
from .harness_base import (  # noqa: F401
    INNER_SLACK_S,
    HarnessAdapter,
    Invoke,
    _read_only,
    plain_text_signal_filter,
)
from .harnesses.agy import AGY_MODEL_ALIASES, AgyAdapter, _agy_trusted  # noqa: F401
from .harnesses.claude import ClaudeAdapter  # noqa: F401
from .harnesses.codex import CodexAdapter  # noqa: F401
from .harnesses.fake import FakeAdapter  # noqa: F401
from .harnesses.opencode import OpenCodeAdapter  # noqa: F401
from .model import AgentSpec  # noqa: F401

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
