# SPAR suggestions

## 2026-08-18 — full-roster delivery and OpenCode/Qwen timeout

Observed during a five-model Finik review run:

- Qwen/OpenCode produced only its startup banner and no review artifact before the run timed out with `rc=124`.
- The other panel inputs delivered normally, so the observed failure appeared isolated to the Qwen/OpenCode path rather than the whole panel pool.
- Retrying a stuck input can waste the full review window even after the remaining verdicts are available.

Possible product-level improvements, without requiring callers to edit a deployed SPAR workflow:

- Add an invocation-time option that requires full-roster delivery and reports an explicit incomplete result if any model is missing.
- Add an invocation-time way to stop or skip one stuck input while preserving already completed takes.
- Surface per-input attempt state, last meaningful output, elapsed time, and retry reason.
- Distinguish provider rate limiting, refusal/safety responses, transport failures, and harness startup hangs in the final artifact.
- Preserve the original per-model output and retry diagnostics alongside the synthesized report.

## 2026-08-21 — Docker SPAR `cx` missing its `hltm` package

Two consecutive five-model Finik diff rounds missed quorum. In both runs the GPT/Codex
input failed twice before reviewing any code:

```text
File "/usr/local/bin/cx", line 13, in <module>
  from hltm.cli import main
ModuleNotFoundError: No module named 'hltm'
```

Affected runs:

- `2026-08-21_21-04-47-7ba8feb4`
- `2026-08-21_21-18-00-a24bcd3c`

The failure is inside the Docker runner after Medulla startup/upgrade, not a model rate
limit. The remaining roster delivered only Sonnet and Gemini (2/5); GLM/Qwen did not
deliver, so the contract correctly treated both rounds as not having happened.

Suggested fix: make the Docker image/startup self-test `cx` imports before starting the
panel, install `hltm` in the same Python environment as `/usr/local/bin/cx`, and classify
this separately from provider/model failure so retry does not repeat an identical broken
harness attempt.
