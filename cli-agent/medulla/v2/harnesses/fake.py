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

    def build(self, spec, prompt_file, prompt_text, timeout_s):
        if not spec.model:
            raise EngineCrash(E_HARNESS, "fake harness: model must be a script path")
        return Invoke(argv=["bash", spec.model, str(prompt_file), *spec.args])


