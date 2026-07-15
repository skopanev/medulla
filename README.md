# medulla

**medulla is a state machine for AI agents.** You describe work as a YAML pipeline — a graph of **nodes** — and medulla runs each node's action on a shell command or an agent harness (`claude-code`, `codex`, `opencode`, `agy`). Bodies emit **signals**; signals route the graph. Built in: retries, fallback to another model, pools with a success threshold, and crash-safe resume.

```
node  = action (shell | agent) [× inputs] → signals → next node
graph = nodes + on_signal edges + two terminals (__exit_ok__ / __exit_fail__)
```

**One machine.** Internally every node is a pool: a node without `inputs:` runs over a single phantom input. Policy (timeouts, attempts, fallback) works identically in both modes. The only thing `inputs:` switches is the **signal contract**: without inputs the body talks to the graph (decision node); with inputs the body writes to the manifest and only the join result routes (pool node).

> **Status**: v2 contract (frozen), engine implemented — the v1 engine is **deleted**; v1 pipelines are not supported. `version: "2"` is required in every pipeline; the engine rejects anything else with a pointer to the migration table below. Implementation phases: **1** — engine (this document), **2** — harness sessions & run history polish.

## Usage

```bash
medulla init <name> [--skill]                     # deploy a bundled template or scaffold a new pipeline
medulla -w .medulla/pipelines/<name>              # run a pipeline
medulla -w ... --var KEY=VALUE                    # set/override vars
medulla -w ... --node <name>                      # dev: start from a specific node
medulla -w ... --resume                           # continue the latest unfinished run
medulla -w ... --run <dir>                        # continue a specific run
medulla --docker -w ...                           # run inside the pipeline's Docker image
```

## Example

```yaml
version: "2"
start: triage
timeout: 86400                # whole-run deadline (default 24h; 0 = unlimited)
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
    on_signal: {found: plan, clean: __exit_ok__}

  plan:
    agent: {harness: codex, model: gpt-5.5, effort: xhigh}
    prompt: |
      {{file:prompts/plan.md}}
      Branch: {{var:BRANCH}}. Write the plan to plan.md and emit signal planned.
    max_attempts: 2
    fallback: {agent: {harness: claude-code, model: sonnet}}
    on_signal:
      planned: panel
      __failed__: __exit_fail__    # body died (rc != 0 after attempts+fallback)

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
    post: 'test -s "reviews/{{input.slug}}.md"'   # verdict = artifact exists, not agent mood
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
    on_signal: {ready: __exit_ok__, rework: plan}
```

## Reference

### Pipeline level

| Field | Default | Meaning |
|---|---|---|
| `version` | required | must be `"2"` |
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
| `max_attempts` | attempts per runner, default 1. Primary gets N, then fallback gets N. Retryable outcomes: non-zero exit, timeout (recorded as rc 124), and **silence** — an agent body exiting 0 with no known signal (the most common agent flake: the work happened, the tag didn't). Silence retries on the **primary only** and never triggers fallback (another model drops the tag just as often; blind fallback duplicates side effects); after all attempts it classifies as `__default__`. Shell silence is deterministic and is not retried. **Silence handling is decision-node behavior**: in pools the body is not expected to signal — silence at rc 0 is the normal ok outcome (see the pool rows) |
| `fallback` | alternate agent action after primary attempts are exhausted. Agent-only (a fallback for shell is meaningless); a fallback has no fallback |
| `ignore_exit_code` | rc != 0 doesn't classify the body as failed; outcome comes from signals. **Forbidden in pool nodes** — `min_success` already owns that role |

Hooks around the body — `pre` / `post` (shell-only; rendered like everything else). These are not lifecycle hooks but **deterministic voices in the signal channel** — the only way to put shell before/after an agent body (you can't append a line to a prompt):

| `pre` — once per node run, before the body renders | Effect |
|---|---|
| emits a routing signal | body and `post` are **skipped**, the signal routes (guard: "already done / not needed") |
| emits `var` | applied **before** the body renders — env prep, the prompt sees fresh vars |
| exits non-zero | `__failed__` (preparation broke), body never runs |
| exits 0, silent | normal path |

| `post` — after **every** attempt, before signal resolution; sees `MEDULLA_BODY_RC` / `MEDULLA_BODY_SIGNAL` | Effect |
|---|---|
| exits non-zero | the **attempt failed** → body+post retry within `max_attempts`, then fallback ("try until the artifact exists": `post: test -s out.md`) |
| exits 0 + emits a signal | **overrides** the body's signal (the agent said planned, the plan is garbage → route needs_rework) |
| exits 0, silent | the body's outcome stands |

In pools both run per input: a `pre` guard skips already-done inputs (manifest `ok: true` with the guard's signal); a `post` check is the truth channel for agent inputs — CLIs exit 0 even when the model did nothing, so without `post` a pool's `min_success` counts air.

Pool (presence of `inputs` turns the action into a pool):

| Field | Meaning |
|---|---|
| `inputs` | YAML list = **data** (scalars or objects, one kind per pool, arrays forbidden), or `{shell: "cmd", timeout: 60}` = **source**. Output sniffing by first byte: `[` JSON array, `{` JSON-lines, else plain lines. A bare string is a hard validation error (data vs code ambiguity). Hard cap: 10 000 inputs |
| `max_parallel` | pool cap: `1` (default, sequential) \| N \| `all` |
| `min_success` | join threshold: `all` (default) \| N ≥ 1. Input ok = rc == 0. No short-circuiting ever — all inputs run (side effects have value) |

Routing:

| Field | Meaning |
|---|---|
| `on_signal` | map of signal → target. Targets are plain strings: a node name, `__exit_ok__`, `__exit_fail__` |

### The reserved namespace — all 8 names

Left-side law: **a bare name is a signal your code emits; a dunder is an engine key.** Right-side law: **terminals are `__exit_*`.**

Routing signals (left side):

| Name | Emitted by | When | Built-in route |
|---|---|---|---|
| `__done__` | pool join | successes ≥ `min_success` | **none — must be routed explicitly** (it's a real edge) |
| `__failed__` | engine | decision node: body died after all attempts+fallback. Pool: join below threshold | `__exit_fail__` |
| `__empty__` | engine | source returned rc 0 and zero inputs (bodies never run) | `__exit_fail__` — loud by default; route explicitly where emptiness is legal |
| `__default__` | nobody (matcher) | body exited rc 0 with no known signal ("said nothing") | `__exit_fail__` |

Channel signals (emitted in stdout, never route; reserved bare words — using them as `on_signal` keys is a validation error):

| Name | Meaning |
|---|---|
| `var` | `<signal:var key=K>value</signal:var>` — set a variable (applied per the fold law) |
| `update` | `<signal:update>msg</signal:update>` — progress message |

Terminal nodes (right side):

| Name | Action |
|---|---|
| `__exit_ok__` | end the run, exit 0 |
| `__exit_fail__` | end the run, exit 2; the routing signal's body becomes the error message |

Signals are read from **stdout only**; stderr goes verbatim to the attempt log (a traceback that happens to echo signal text must never route the graph). A known signal emitted before a non-zero exit wins over the exit code. Pool bodies' signals never route — they are recorded in the manifest (law of layers: inputs produce data, joins produce transitions). Bare `done`/`failed` on decision nodes are ordinary user signals with no special meaning. User nodes may not be named `__*__` or `on/off/yes/no/true/false` (YAML 1.1 boolean traps).

### Templates & environment

| Template | Meaning |
|---|---|
| `{{var:KEY}}`, `{{var:KEY:-default}}` | variable, with optional default |
| `{{file:path}}` | file inclusion, recursive (depth ≤ 10, exceeding = render error with the inclusion chain; missing file = error). Relative paths resolve against the **including file's** directory. Paths are static — no vars inside |
| `{{input}}` | the input; objects render as compact JSON |
| `{{input.a.b:-default}}` | dot-walk into object inputs. Missing field without a default = hard render error |
| `{{input_index}}` (1-based), `{{input_count}}` | position / total |
| `{{last.node}}`, `{{last.signal}}`, `{{last.message}}`, `{{last.rc}}` | outcome of the previously completed node — transient tokens (not vars: no persistence, no fold-law constraints). The bridge for agent-bodied handlers whose prompts can't read env |

Render model: phase 1 — file inclusion; phase 2 — one simultaneous, **inert** pass of var/input substitution. **Files are code, values are data**: mustache inside included files resolves fully; mustache inside var/input *values* stays literal (injection-safe by construction). Every scalar field of an action is a template (that's why an ensemble is just a pool with per-input `harness`/`model`). A field rendering to an empty string counts as absent — for optional agent fields only; empty `shell`/`prompt`/`harness` is an error. Rendering happens **once per node run**, before attempt 1; retries reuse the same rendered text. A render error on a decision node (phantom input) is an engine crash (`E_RENDER`) — the template itself is broken. A render error for **one pool input** fails that input only (manifest row `reason: render` with the template path and input index) — one malformed row of producer data must not kill the other inputs.

Environment (data should flow to shell via env, templates are for slugs/paths — quoting-safe):

| Variable | Meaning |
|---|---|
| `MEDULLA_INPUT` | the input as JSON |
| `MEDULLA_INPUT_INDEX` / `MEDULLA_INPUT_COUNT` | position / total |
| `MEDULLA_INPUT_<KEY>` | each flat scalar field of an object input, uppercased |
| `MEDULLA_INPUT_KEY` / `MEDULLA_ATTEMPT_ID` | stable input identity `(index, hash)` / unique attempt id — the idempotency keys for bodies that mutate the outside world (branches, PRs, tickets) |
| `MEDULLA_LAST_NODE` / `_SIGNAL` / `_MESSAGE` / `_RC` | outcome of the previously completed node, published before every transition (after a pool, `_RC` is empty — a join has no single rc; read the manifest); `MEDULLA_LAST_EVENT_JSON` carries the same as one JSON object. Timeout is recognizable as rc 124 |
| `MEDULLA_MANIFEST_<NODE>` | path to a pool node's manifest (dashes → underscores) |
| `MEDULLA_RUN_ID` / `MEDULLA_RUN_DIR` | run id (settable from outside for correlation; else generated) / this run's directory. Put artifacts in `$MEDULLA_RUN_DIR/artifacts/` |
| `MEDULLA_TIMEOUT_S` | resolved step timeout, for CLIs that need to size their own |

All pipeline vars are exported to child processes. Reserved names (`PATH`, `HOME`, `SHELL`, `LD_*`, …) are rejected by the validator.

Var-signal semantics (fold law): variables are mutable state, and state mutation requires ordering — var signals are **applied only at `max_parallel: 1`** (which includes every decision node), in input order, atomically from the **successful** attempt only. At `max_parallel > 1` they are recorded in the manifest instead.

### Errors & exit codes

Two failure classes. The test: *can it be fixed by changing data/prompts/retrying?* → workflow failure, the graph decides. *Does it require editing the pipeline/environment?* → engine crash.

| Exit | Class | Meaning |
|---|---|---|
| 0 | — | `__exit_ok__` |
| 1 | engine crash | the pipeline itself is broken — fix the pipeline, retrying is pointless |
| 2 | workflow failure | the graph routed to `__exit_fail__` — fix the task/inputs |
| 130 | interrupt | — |

A fixed delay separates attempts and the fallback switch (`MEDULLA_RETRY_DELAY_S`, default 2s — retry storms hit provider rate limits).

Crash codes: `E_VALIDATION` (schema/XOR/unknown target/boolean node names/bare keys in pool routing/defaults-inherited self-edges), `E_RENDER` (broken decision-node template: missing file, depth > 10, empty required render), `E_DEADLINE` (pipeline timeout), `E_INPUTS` (source exited non-zero, or emitted mixed-kind/array elements — a broken producer is not an empty queue), `E_INPUTS_LIMIT` (> 10k), `E_HARNESS`, `E_INTERNAL`.

`E_HARNESS` is razor-thin by design: **only** "harness binary missing / unresolvable". An agent process dying unexpectedly (OOM, segfault, API hiccup) is always a non-zero exit — class B, retryable. If that boundary drifts, transient flakes start crashing whole runs.

#### Handling errors in the graph

Handlers are **ordinary nodes routed before the terminal** — never a body on the terminal (`kill -9` makes exit hooks an illusion; a node has a timeout, attempts and its own log). The error payload arrives through the same channels as all data: `$MEDULLA_LAST_*` for shell, `{{last.*}}` for prompts. Global catch-all is three lines of `defaults`:

```yaml
defaults:
  on_signal: {__failed__: notify, __default__: notify}   # root supervisor

nodes:
  notify:
    shell: 'curl -s "$HOOK" -d "text=[medulla] $MEDULLA_LAST_NODE ($MEDULLA_LAST_SIGNAL): $MEDULLA_LAST_MESSAGE"'
    timeout: 30
    on_signal: {__default__: __exit_fail__, __failed__: __exit_fail__}   # own dunders explicit!
```

A node-level edge beats the defaults catch-all, which beats built-ins — a three-tier supervision chain. The validator rejects a defaults-inherited edge that points at its own node (`notify` failing into `notify` would loop); explicit self-loops remain legal (retry pattern). A pool's `__failed__` message is pre-aggregated: `"2/5 inputs ok (min_success=3); rc!=0 x2, timeout x1, render x0"` — the manifest has the rest. Class A is not catchable in the graph by definition: the graph itself is what's broken.

The error **message is the body of the signal** that routed to `__exit_fail__` (engine facts carry their own: `__failed__` → rc/join stats, `__empty__` → "source returned 0 inputs", `__default__` → tail of stdout). Every run ends with an atomic `outcome.json`:

```json
{"outcome": "failed", "exit_code": 2,
 "error": {"code": "SIGNAL_FAIL", "message": "no repo access: git clone rc=128",
           "node": "plan", "step": 12, "signal": "blocked"},
 "steps": 12, "duration_s": 4180}
```

### Lifecycle

**Boot** — parse CLI → load yaml (`version: "2"` or a friendly migration error) → validate everything (`E_VALIDATION` before any run dir exists) → create `runs/<ts>-<run_id>/`, snapshot the config, seed vars, prune old runs → set the deadline.

**Node loop** (single program counter) — per node: deadline check → materialize inputs (no key → phantom; list → snapshot into the step dir; source → render, execute, sniff, check) → run the pool (`max_parallel` workers; per input: render once → attempts → fallback; stream stdout to the step log with realtime signal extraction) → classify (decision: signal | `__failed__` | `__default__`; pool: `__done__` | `__failed__` | `__empty__`) → resolve the edge (node `on_signal` → `defaults.on_signal` → built-ins) → append the journal row → next node or terminal.

**Finish** — atomic `outcome.json`, exit 0/2; crashes write `outcome.json` with the `E_*` code and exit 1; SIGINT → best-effort outcome, exit 130.

**Resume** — pick the latest run without `outcome.json`, or with `outcome: interrupted` **or `crashed`** (deliberate extension: the #1 resume trigger is the `E_DEADLINE` crash; config-class crashes simply crash again identically) — or `--run <dir>`: reload the **snapshot** config (a run's config is immutable), vars, journal position; an interrupted pool continues from the manifest done-mask (input identity = `(index, hash)`; sources are never re-executed on resume); an interrupted decision node re-runs whole (body idempotence is the author's concern). The deadline is fresh per invocation.

### Layout

A pipeline is a self-contained directory — contract, code, history:

```
.medulla/pipelines/<name>/
  pipeline.yaml            # the contract
  prompts/  scripts/       # its code
  harness/                 # phase 2: harness HOME (sessions), shared by all runs
  runs/<ts>-<run_id>/      # one directory per run
    pipeline.yaml          # config snapshot as loaded (immutable for the run)
    journal.jsonl          # graph chronology, append-only (step, node, rc, signal, message, duration)
    vars.yaml              # variables (updated on var signals)
    outcome.json           # written only on completion (atomic); absent = running or hard-killed
    steps/
      001-triage/                        # every step gets a directory
      002-plan/prompt.md                 # rendered agent input (what the agent actually saw)
      002-plan/attempt-1-codex.txt       # raw CLI stream per attempt
      004-apply/inputs.json              # inputs snapshot (resume)
      004-apply/input-2-sonnet.txt       # per-input logs
      004-apply/manifest.jsonl           # {index, key, input, ok, reason, signal, message,
                                         #  rc, timed_out, attempts, fallback, harness, model,
                                         #  vars, updates, duration_s, log}
```

Retention: on start, keep the newest `keep_runs` finished runs; directories without `outcome.json` younger than the pipeline timeout are never pruned. History browsing needs no CLI: `ls runs/`, `cat outcome.json`.

**Phase 2 (designed, not in phase 1):** pipeline-scoped harness sessions — `harness/` as the container HOME, session ids captured into journal/manifest from the CLI streams the engine already parses, and a `resume:` field on the agent block to continue a node's last successful session (current run first, then previous runs, newest first). Docker: everything lives under `/workspace` (already mounted RW); credentials are copied once from the read-only `/mnt/*` mounts.

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
- **DLQ (repair node)**: failed pool inputs are structured data — re-enqueue only them, never the whole pool:
  ```yaml
  retry-failed:
    inputs: {shell: "jq -c 'select(.ok|not) | .input' $MEDULLA_MANIFEST_APPLY"}
    max_parallel: 2
    shell: 'bash scripts/fix_one.sh "$MEDULLA_INPUT_ID"'
    on_signal: {__done__: report, __empty__: report}
  ```

### Migrating from v1

The v1 engine is removed; this table is the dictionary for porting old pipelines.

| v1 | v2 |
|---|---|
| `stages` / `starting` | `nodes` / `start` |
| `runner:` / `llm:` / `executor`+`command` | action fields directly on the node: `agent: {harness, model, effort, args}` xor `shell` |
| `loop:` + `list:` + `fetch:` + `parallel: true` | `inputs:` on the node + `max_parallel` |
| `done: __next_item__` + `loop_done` | pool joins route `__done__` / `__failed__` / `__empty__` via `min_success` |
| `max_iterations` / `reset_iterations` / `on_max` | removed — budget gate pattern (vars) |
| hardcoded `max_rounds=500` | pipeline `timeout` (wall-clock deadline) |
| `round_timeout` / `fallback_runner` | `defaults: {timeout, fallback}` |
| `__exit__` | `__exit_ok__` / `__exit_fail__` |
| `ignore_rc` | `ignore_exit_code` (decision nodes only) |
| `{{__item__}}` / `{{__list_item__}}` | `{{input}}` family + `MEDULLA_INPUT_*` env |
| `MEDULLA_TASK_ID` | `MEDULLA_RUN_ID` |
| `.medulla/vars.<task>.yaml` | `.medulla/pipelines/<name>/runs/<id>/` |
| `--stage` | `--node` |
| gemini executor | removed (use `agy`) |
| exit codes 0/1/2/3 ad-hoc | 0 = ok, 1 = engine crash, 2 = workflow fail, 130 = interrupt |

### Reserved (designed, not implemented — in priority order)

`stall_timeout` (no-stdout watchdog for hung agents; partially covered today by per-harness flags — codex `stream_idle_timeout_ms`, agy `--print-timeout`) · manifest attempt states (`claimed|started|completed`) + per-node resume policy `rerun|skip|probe` (exactly-once is impossible; make duplicate side effects detectable) · `resume:` (continue a node's session — phase 2) · `cancel_rest` (race joins) · `on_input_fail: abort` (fail-fast pools) · `format:` on sources (override sniffing) · `finally` (best-effort only, if reality ever demands it) · agent-block defaults.
