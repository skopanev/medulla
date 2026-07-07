# Medulla v2 — Pipeline Contract

A pipeline is a graph of **nodes**. Each node runs one **action** — a shell command or an agent harness — optionally mapped over a set of **inputs**. Bodies emit **signals**; signals route the graph. That's the whole model.

```
node  = action (shell | agent) [× inputs] → signals → next node
graph = nodes + on_signal edges + two terminals (__succeed__ / __fail__)
```

**One machine.** Internally every node is a pool: a node without `inputs:` runs over a single phantom input. Policy (timeouts, attempts, fallback) works identically in both modes. The only thing `inputs:` switches is the **signal contract**: without inputs the body talks to the graph (decision node); with inputs the body writes to the manifest and only the join result routes (pool node).

Old-engine pipelines keep working: files with `version: "2"` run on the new engine, everything else runs on the legacy runner untouched.

## Example

```yaml
version: "2"
start: triage
timeout: 86400                # whole run deadline (default 24h; 0 = unlimited)
vars: {BRANCH: main}

defaults:                     # policy defaults for every action ("field absent → take from here")
  timeout: 1800
  fallback: {agent: {harness: claude-code, model: opus}}

nodes:

  # decision node: body signals route the graph
  triage:
    shell: |
      git worktree prune; rm -f .locks/*        # crash-only: clean up on entry
      n=$(rg -l "FIXME" src/ | wc -l | tr -d ' ')
      [ "$n" -gt 0 ] && echo "<signal:found>ok</signal:found>" || echo "<signal:clean>ok</signal:clean>"
    timeout: 60
    on_signal: {found: plan, clean: __succeed__}

  plan:
    agent: {harness: codex, model: gpt-5.5, effort: xhigh}
    prompt: |
      {{file:prompts/plan.md}}
      Branch: {{var:BRANCH}}. Write the plan to plan.md and emit signal planned.
    max_attempts: 2
    fallback: {agent: {harness: claude-code, model: sonnet}}
    on_signal:
      planned: panel
      __failed__: __fail__     # body died (rc != 0 after attempts+fallback)

  # pool node: inputs switch the contract — the join routes, bodies write to the manifest
  panel:
    inputs:
      - {slug: gpt5,   harness: codex,       model: gpt-5.5, effort: xhigh}
      - {slug: sonnet, harness: claude-code, model: sonnet}
      - {slug: gem,    harness: agy,         model: "Gemini 3.1 Pro (High)"}
    max_parallel: all
    min_success: 1
    agent: {harness: "{{input.harness}}", model: "{{input.model}}", effort: "{{input.effort:-}}"}
    prompt: |
      You are panelist {{input.slug}} ({{input_index}}/{{input_count}}).
      Critique the plan: {{file:plan.md}}
      Write your verdict to reviews/{{input.slug}}.md
    on_signal: {__done__: apply}

  apply:
    inputs: {shell: "python3 scripts/tickets.py --json", timeout: 60}
    max_parallel: 3
    min_success: 2
    shell: |
      bash scripts/fix_one.sh "$MEDULLA_INPUT_ID" "$MEDULLA_INPUT_TITLE"
    max_attempts: 2
    on_signal:
      __done__: report
      __empty__: report        # empty queue is legal here — route it explicitly

  report:
    shell: |
      ok=$(jq -s 'map(select(.ok))|length' "$MEDULLA_MANIFEST_APPLY")
      total=$(jq -s length "$MEDULLA_MANIFEST_APPLY")
      [ "$ok" -eq "$total" ] && echo "<signal:ready>ok</signal:ready>" || echo "<signal:rework>ok</signal:rework>"
    on_signal: {ready: __succeed__, rework: plan}
```

## Reference

### Pipeline level

| Field | Default | Meaning |
|---|---|---|
| `version` | — | `"2"` selects this engine; anything else → legacy runner |
| `start` | required | first node |
| `vars` | `{}` | initial variables (also exported to env; see blacklist below) |
| `timeout` | 86400 | wall-clock deadline for the whole run; `0` = unlimited. The engine clamps every child timeout to the remaining budget |
| `defaults` | — | policy defaults for actions: `timeout`, `max_attempts`, `ignore_exit_code`, `fallback`, `on_signal` (per-key merge). Flat scalars only — never merged deep |
| `keep_runs` | 20 | auto-prune of run history on start |

### Node = action [+ pool] + routing

Action (exactly one of `shell` / `agent`):

| Field | Meaning |
|---|---|
| `shell` | shell command; its config *is* the command. `prompt` here is a validation error |
| `agent` | `{harness, model, effort, args}` — one entity, one block. Scalar shortcut: `agent: codex` ≡ `agent: {harness: codex}`. `args` is a raw CLI escape hatch — non-portable across harnesses |
| `prompt` | agent input (not config). Never quote signal syntax literally in prompts — describe it ("emit signal planned"); the engine delivers the syntax to the agent |
| `timeout` | per **attempt**, seconds |
| `max_attempts` | attempts per runner, default 1. Primary gets N, then fallback gets N |
| `fallback` | alternate agent action after primary attempts are exhausted. Agent-only (a fallback for shell is meaningless); a fallback has no fallback |
| `ignore_exit_code` | rc != 0 doesn't classify the body as failed; outcome comes from signals. **Forbidden in pool nodes** — `min_success` already owns that role |

Pool (presence of `inputs` turns the action into a pool):

| Field | Meaning |
|---|---|
| `inputs` | YAML list = **data** (scalars or objects, one kind per pool, arrays forbidden), or `{shell: "cmd", timeout: 60}` = **source**. Output sniffing by first byte: `[` JSON array, `{` JSON-lines, else plain lines. A bare string is a hard validation error (data vs code ambiguity). Hard cap: 10 000 inputs |
| `max_parallel` | pool cap: `1` (default, sequential) \| N \| `all` |
| `min_success` | join threshold: `all` (default) \| N ≥ 1. Input ok = rc == 0. No short-circuiting ever — all inputs run (side effects have value) |

Routing:

| Field | Meaning |
|---|---|
| `on_signal` | map of signal → target. Targets are plain strings: a node name, `__succeed__`, `__fail__` |

### Signals

Emitted as `<signal:NAME>body</signal:NAME>` on stdout. Grammar: a signal is a **fact** (perfect tense: `planned`, `failed`, `ready`); a target is a **command** (`__succeed__`, `__fail__`).

Left-side namespace law: **bare name = a signal your code emits; dunder = an engine key.**

| Engine key | Emitted when | Built-in route |
|---|---|---|
| `__done__` | pool join reached `min_success` | **must be routed explicitly** (it's a real edge) |
| `__failed__` | decision node: body died (rc != 0 / timeout after all attempts+fallback). Pool: join below threshold | `__fail__` |
| `__empty__` | source returned rc 0 and zero inputs (bodies never run, manifest empty) | `__fail__` — loud by default; route explicitly where emptiness is legal |
| `__default__` | body exited rc 0 but emitted no known signal ("said nothing") | `__fail__` |

A known signal emitted before a non-zero exit wins over the exit code. Non-routing engine channels: `<signal:var key=K>value</signal:var>` (sets a variable), `<signal:update>msg</signal:update>` (progress). Pool bodies' signals never route — they are recorded in the manifest (law of layers: inputs produce data, joins produce transitions).

### Templates & environment

| Template | Meaning |
|---|---|
| `{{var:KEY}}`, `{{var:KEY:-default}}` | variable, with optional default |
| `{{file:path}}` | file inclusion, recursive (depth ≤ 10, exceeding = render error with the inclusion chain; missing file = error). Relative paths resolve against the **including file's** directory. Paths are static — no vars inside |
| `{{input}}` | the input; objects render as compact JSON |
| `{{input.a.b:-default}}` | dot-walk into object inputs. Missing field without a default = hard render error |
| `{{input_index}}` (1-based), `{{input_count}}` | position / total |

Render model: phase 1 — file inclusion; phase 2 — one simultaneous, **inert** pass of var/input substitution. **Files are code, values are data**: mustache inside included files resolves fully; mustache inside var/input *values* stays literal (injection-safe by construction). Every scalar field of an action is a template (that's why an ensemble is just a pool with per-input `harness`/`model`). A field rendering to an empty string counts as absent — for optional agent fields only; empty `shell`/`prompt`/`harness` is an error. Rendering happens **once per node run**; retries reuse the same rendered text.

Environment (data should flow to shell via env, templates are for slugs/paths — quoting-safe):

| Variable | Meaning |
|---|---|
| `MEDULLA_INPUT` | the input as JSON |
| `MEDULLA_INPUT_INDEX` / `MEDULLA_INPUT_COUNT` | position / total |
| `MEDULLA_INPUT_<KEY>` | each flat scalar field of an object input, uppercased |
| `MEDULLA_MANIFEST_<NODE>` | path to a pool node's manifest (dashes → underscores) |
| `MEDULLA_RUN_DIR` | this run's directory (put artifacts in `$MEDULLA_RUN_DIR/artifacts/`) |
| `MEDULLA_TASK_ID`, `MEDULLA_TIMEOUT_S` | run isolation id; resolved step timeout |

All pipeline vars are exported to child processes. Reserved names (`PATH`, `HOME`, `SHELL`, `LD_*`, …) are rejected by the validator.

Var-signal semantics (fold law): variables are mutable state, and state mutation requires ordering — var signals are **applied only at `max_parallel: 1`** (which includes every decision node), in input order, atomically from the **successful** attempt only. At `max_parallel > 1` they are recorded in the manifest instead.

### Errors & exit codes

Two failure classes, two exit codes:

| Exit | Class | Meaning |
|---|---|---|
| 0 | — | `__succeed__` |
| 1 | engine crash | the pipeline itself is broken — fix the pipeline, retrying is pointless |
| 2 | workflow failure | the graph routed to `__fail__` — fix the task/inputs |
| 130 | interrupt | — |

Crash codes: `E_VALIDATION` (load-time schema/XOR/unknown target/boolean node names/bare keys in pool routing), `E_RENDER` (missing file, depth > 10, missing input field, empty required render), `E_DEADLINE` (pipeline timeout), `E_INPUTS` (source exited non-zero — a broken producer is not an empty queue), `E_INPUTS_LIMIT` (> 10k), `E_HARNESS`, `E_INTERNAL`.

The error **message is the body of the signal** that routed to `__fail__` (engine emits: `__failed__` → join/rc stats, `__empty__` → "source returned 0 inputs", `__default__` → tail of stdout). Every run ends with an atomic `outcome.json`:

```json
{"outcome": "failed", "exit_code": 2,
 "error": {"code": "SIGNAL_FAIL", "message": "no repo access: git clone rc=128",
           "node": "plan", "step": 12, "signal": "blocked"},
 "steps": 12, "duration_s": 4180}
```

### Layout, runs, sessions

A pipeline is a self-contained directory — contract, code, memory, history:

```
.medulla/pipelines/<name>/
  pipeline.yaml            # the contract
  prompts/  scripts/       # its code
  harness/                 # its memory: harness HOME (sessions), shared by all runs
  runs/<ts>-<task_id>/     # its history: one directory per run
    pipeline.yaml          # config snapshot as loaded
    journal.jsonl          # graph chronology, append-only (step, node, rc, signal, duration, session)
    vars.yaml              # variables (updated on var signals)
    outcome.json           # written only on completion (atomic); absent = running or hard-killed
    state/                 # input snapshots + done-masks (pool resume)
    steps/
      001-triage.txt                     # single-attempt node → one file
      002-plan/prompt.md                 # rendered agent input (what the agent actually saw)
      002-plan/attempt-1-codex.txt       # raw CLI stream per attempt
      003-panel/input-2-sonnet.txt       # per-input logs
      003-panel/manifest.jsonl           # {index, input, ok, rc, signal, duration_s, session, log}
```

- **Resume**: `--resume` picks the latest run without `outcome.json`; `--run <dir>` picks one. Vars, journal and manifest done-masks restore position; input identity is `(index, hash)`; sources are snapshotted — never re-executed on resume. A fresh start is a fresh directory with clean vars.
- **Sessions**: harness state is **pipeline-scoped** (session lookup is by node name, and node names are pipeline-scoped). Session ids are captured into journal/manifest rows from the CLI streams the engine already parses (claude: stream-json init; codex: `exec --json` thread event; claude can also be pre-seeded via `--session-id`). Reserved field `resume:` on the agent block will continue a node's last successful session, current run first, then previous runs (journal walk, newest first).
- **Docker**: everything lives under `/workspace` (already mounted RW); container `HOME` = the pipeline's `harness/` dir; credentials are copied from the read-only `/mnt/*` mounts once, idempotently. Runs of different pipelines are fully isolated; concurrent runs of one pipeline share `harness/` safely (sessions are per-id files).
- **Retention**: on start, keep the newest `keep_runs` finished runs; directories without `outcome.json` younger than the pipeline timeout are never pruned. `harness/` is never pruned (session cleanup belongs to the CLIs).

Supported harnesses: `claude-code`, `codex`, `opencode`, `agy`.

### Canonical patterns

- **Budget gate** (bounded rework — the engine has no per-node visit caps; cycle semantics belong to the workflow):
  ```yaml
  gate:
    shell: |
      n=$(( ${PLAN_TRIES:-0} + 1 ))
      echo "<signal:var key=PLAN_TRIES>$n</signal:var>"
      [ "$n" -le 3 ] && echo "<signal:go>ok</signal:go>" || echo "<signal:budget_out>ok</signal:budget_out>"
    on_signal: {go: plan, budget_out: escalate}
  ```
- **Crawler**: producer node → pool → probe node → edge back; the source re-runs on each new node run.
- **Heterogeneous pool**: inputs carry `role`/`prompt` fields; the node prompt is the template (code, vars resolve), input fragments are inserted inert.
- **Entry cleanup** (crash-only): there is no `finally`; exit hooks are an illusion under `kill -9`. Clean stale state idempotently at the start of the node/run that needs it clean.
- **Parallel tickets**: the engine guarantees vars isolation; file/git isolation is the body's job (`git worktree` per input) and disjointness is the producer script's contract.

### Changes from v1

| v1 | v2 |
|---|---|
| `stages` / `starting` / `on_signal` | `nodes` / `start` / `on_signal` |
| `runner:` / `llm:` / `executor`+`command` | action fields directly on the node: `agent: {harness, model, effort, args}` xor `shell` |
| `loop:` + `list:` + `fetch:` + `parallel: true` | `inputs:` on the node + `max_parallel` |
| `done: __next_item__` + `loop_done` | pool joins route `__done__` / `__failed__` / `__empty__` via `min_success` |
| `max_iterations` / `reset_iterations` / `on_max` | removed — budget gate pattern (vars) |
| hardcoded `max_rounds=500` | pipeline `timeout` (wall-clock deadline) |
| `round_timeout` / `fallback_runner` | `defaults: {timeout, fallback}` |
| `__exit__` | `__succeed__` / `__fail__` |
| `ignore_rc` | `ignore_exit_code` (decision nodes only) |
| `{{__item__}}` / `{{__list_item__}}` | `{{input}}` family + `MEDULLA_INPUT_*` env |
| `.medulla/vars.<task>.yaml` | `.medulla/pipelines/<name>/runs/<id>/` |
| exit codes 0/1/2/3 ad-hoc | 0 = succeed, 1 = engine crash, 2 = workflow fail, 130 = interrupt |

### Reserved (designed, not in v2.0)

`check:` (post-condition on an action, verdict replaces rc, retried with the body) · `resume:` (continue a node's session) · `cancel_rest` (race joins) · `on_input_fail: abort` (fail-fast pools) · `format:` on sources (override sniffing) · `finally` (best-effort only, if reality ever demands it) · agent-block defaults.
