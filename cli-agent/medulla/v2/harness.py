"""Harness adapters: turn an AgentSpec + prompt file into argv, and own the
harness-specific stdout filtering (assistant-text only — tool output echoing
signal text must never route).

Part-2 scope: the `fake` harness only — a test double whose `model` field is a
path to a script invoked as `bash <script> <prompt_file>`. Real adapters
(claude-code, codex, opencode, agy) land in part 5 with their JSON filters.
"""
from __future__ import annotations

from pathlib import Path

from .errors import EngineCrash, E_HARNESS, E_INTERNAL
from .model import AgentSpec

REAL_HARNESSES = ("claude-code", "codex", "opencode", "agy")


class HarnessAdapter:
    name = "abstract"

    def build_argv(self, spec: AgentSpec, prompt_file: Path) -> list[str]:
        raise NotImplementedError

    def filter_stdout(self, stdout: str) -> str:
        """Reduce raw CLI output to signal-scannable text (assistant text only)."""
        return stdout


class FakeAdapter(HarnessAdapter):
    """agent: {harness: fake, model: path/to/script.sh} — the script receives the
    rendered prompt file as $1 and behaves however the test configures it."""
    name = "fake"

    def build_argv(self, spec: AgentSpec, prompt_file: Path) -> list[str]:
        if not spec.model:
            raise EngineCrash(E_HARNESS, "fake harness: model must be a script path")
        return ["bash", spec.model, str(prompt_file)]


def resolve(spec: AgentSpec) -> HarnessAdapter:
    if spec.harness == FakeAdapter.name:
        return FakeAdapter()
    if spec.harness in REAL_HARNESSES:
        # an engine limitation, not a missing binary — the E_HARNESS razor holds
        # (part 5 adapters will check shutil.which and raise E_HARNESS only then)
        raise EngineCrash(
            E_INTERNAL, f"harness '{spec.harness}' is not wired yet (adapters land in part 5)"
        )
    raise EngineCrash(E_HARNESS, f"unknown harness '{spec.harness}'")
