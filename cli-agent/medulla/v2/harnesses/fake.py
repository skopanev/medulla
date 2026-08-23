"""The fake adapter."""
from __future__ import annotations

from ..errors import E_HARNESS, EngineCrash
from ..harness_base import (
    HarnessAdapter,
    Invoke,
)

# ── fake (tests) ─────────────────────────────────────────────────────────────

class FakeAdapter(HarnessAdapter):
    """agent: {harness: fake, model: path/to/script.sh} — the script receives the
    rendered prompt file as $1 (plus rendered args) and behaves as configured."""
    name = "fake"
    # mirrors claude's field, so the engine's session plumbing is testable
    # end to end without spending a real CLI turn
    session_key = "session_id"

    def build(self, spec, prompt_file, prompt_text, timeout_s, resume=None):
        if not spec.model:
            raise EngineCrash(E_HARNESS, "fake harness: model must be a script path")
        argv = ["bash", spec.model, str(prompt_file), *spec.args]
        if resume:
            argv += ["--resume", resume]
        return Invoke(argv=argv)


