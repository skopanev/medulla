# medulla

**medulla is a state machine for AI agents.** You describe work as a YAML pipeline — a graph of **nodes** — and medulla runs each node's action on a shell command or an agent harness (`claude-code`, `codex`, `opencode`, `agy`). Bodies emit **signals**; signals route the graph. Built in: retries, fallback to another model, parallel pools with a success threshold, live operator streaming, and crash-safe resume.

```
node  = action (shell | agent) [× inputs] → signals → next node
graph = nodes + on_signal edges + two terminals (__exit_ok__ / __exit_fail__)
```

> **Status**: v2 (the only engine — v1 is deleted; see [Migrating from v1](#migrating-from-v1)). Phase 2 (harness sessions, cost telemetry) is designed but not built.

## Getting started

```bash
curl -sSL https://raw.githubusercontent.com/skopanev/medulla/main/install.sh | bash
# (own venv + ~/.local/bin/medulla; re-run to update. Alternatives:
#  pipx install git+https://github.com/skopanev/medulla.git
#  dev: MEDULLA_REPO=/path/to/checkout bash install.sh — editable, edits apply instantly)

cd your-project
medulla init my-pipe              # scaffold: commented pipeline.yaml, README, .gitignore, prompts/
medulla init spar                 # ...or deploy a bundled template (spar: a panel of models)
medulla init spar --skill         # ...and register its SKILL.md with claude-code / codex / opencode
medulla init                      # lists available bundled templates

medulla -w .medulla/pipelines/my-pipe                  # run
medulla -w .medulla/pipelines/my-pipe --dry-run        # print the resolved plan, run nothing
medulla -w .medulla/pipelines/my-pipe --var KEY=VALUE  # override vars (fresh runs only)
medulla -w .medulla/pipelines/my-pipe --resume         # continue the latest unfinished run
medulla -w .medulla/pipelines/my-pipe --run <dir>      # continue a specific run directory
medulla -w .medulla/pipelines/my-pipe --validate       # load + validate only
medulla --docker -w .medulla/pipelines/my-pipe         # run inside the pipeline's Docker image
medulla upgrade                                        # pipx upgrade medulla
medulla --help                                         # the full env/signal reference, always current
```

The scaffold runs out of the box — edit `pipeline.yaml` from there. Exit codes: `0` succeeded, `2` the workflow routed to `__exit_fail__`, `1` the pipeline itself is broken, `130` interrupted (Ctrl-C and `docker stop` both stop the run gracefully: children are killed, the run stays resumable).

## Writing pipelines

### A node is an action plus routing

```yaml
version: "2"
start: triage
nodes:
  triage:
    shell: |
      n=$(rg -l "FIXME" src/ | wc -l | tr -d ' ')
      [ "$n" -gt 0 ] && echo "<signal:found>$n files</signal:found>" \
                     || echo "<signal:clean>ok</signal:clean>"
    timeout: 60
    on_signal: {found: plan, clean: __exit_ok__}
```

A node runs exactly one of `shell:` (a command) or `agent:` (an AI harness). The body prints **signals** to stdout; `on_signal` maps them to the next node or a terminal. That's the whole model.

### Signals

```
<signal:NAME>short message</signal:NAME>       route the graph (the message travels with it)
<signal:var key=K>value</signal:var>           set a pipeline var (never routes)
<signal:update>progress line</signal:update>   progress only (never routes)
```

In agent prompts, naming the signal is enough — "emit the signal named done" — because the engine appends the exact protocol to every agent prompt automatically. Custom signal names are unrestricted. Quoting literal syntax in a prompt also works (scanning is post-hoc, the body always runs to completion); the one residual risk is a model echoing the quoted tag without doing the work — `post:` is the antidote. Signals are read from **stdout only** and, for plain-text harnesses, only when the tag **starts a line** — tool output echoing a tag mid-line can never route.

### Agents

```yaml
  plan:
    agent: {harness: codex, model: gpt-5.5, effort: xhigh}   # shortcut: agent: codex
    prompt: |
      {{file:prompts/plan.md}}
      Branch: {{var:BRANCH}}. Write the plan to plan.md and emit the signal named planned.
    post: 'test -s plan.md'          # the truth channel: verify the artifact, don't trust rc
    max_attempts: 2
    fallback: {agent: {harness: claude-code, model: sonnet}}
    on_signal: {planned: review, __failed__: __exit_fail__}
```

Harnesses: `claude-code`, `codex`, `opencode`, `agy`. `effort` maps to each CLI's native knob. `max_attempts` retries flaky attempts (non-zero exit, timeout, agent silence); `fallback` is a second agent tried after the primary's attempts are exhausted. While an agent works, its text streams live to your terminal (`MEDULLA_STREAM=0` to silence).

### Hooks: pre and post

Shell around any body — the only way to put deterministic checks before/after an agent. Hooks get a fixed 60s timeout (deadline-clamped); they are one-line artifact tests, not workloads:

- **`pre`** runs once before the body renders. Emits a routing signal → the body is **skipped** (guard: "already done"); emits `var` → the body's prompt sees it; exits non-zero → `__failed__`.
- **`post`** runs after **every** attempt. Exits non-zero → that attempt failed (retry, then fallback — "try until the artifact exists"); emits a signal → **overrides** the body's signal; silent → the body's outcome stands.

### Pools: fan-out over inputs

```yaml
  fix-tickets:
    inputs: {shell: "python3 scripts/tickets.py --json", timeout: 60}
    max_parallel: 3
    min_success: 2
    shell: 'bash scripts/fix_one.sh "$MEDULLA_INPUT_ID" "$MEDULLA_INPUT_TITLE"'
    max_attempts: 2
    on_signal: {__done__: report, __empty__: report, __failed__: __exit_fail__}
```

Adding `inputs:` turns the action into a pool: the body runs once per input, `max_parallel` at a time. `inputs` is a YAML list (scalars or objects) or a `{shell: ...}` source whose output is sniffed: `[` → JSON array, `{` → JSON-lines, else plain lines. Per-input results land in a **manifest** (JSONL); the join routes `__done__` when successes reach `min_success`. Pool bodies' own signals never route — they're recorded as data. Every input always runs (no short-circuit: side effects have value). An interrupted pool **resumes** from the manifest: done inputs are skipped, the source is never re-executed.

### Secrets: .env

Put `KEY=VALUE` lines into `<pipeline>/.env` — bodies and hooks see them as environment (that's where provider API keys live). Deliberately **not** vars: never templated by `{{var:}}`, never persisted into run history. `init` seeds a `.gitignore` (`.env`, `runs/`) into every pipeline it creates.

## All variables

### Environment the engine provides (bodies and hooks)

| Variable | When | Meaning |
|---|---|---|
| `MEDULLA_RUN_ID` / `MEDULLA_RUN_DIR` | always | run id / run directory. Put deliverables in `$MEDULLA_RUN_DIR/artifacts/` |
| *all pipeline vars* | always | exported as-is, including `<signal:var>`-set ones |
| *all `.env` entries* | always | secrets channel (see above) |
| `MEDULLA_TIMEOUT_S` | always | resolved, deadline-clamped timeout of the current step (CLIs size their own limits from it) |
| `MEDULLA_ATTEMPT_ID` | body | unique attempt id: `<step>[.i<input>].<p|f><attempt>` — `003.p1` (decision, primary, 1st try), `003.i2.f1` (pool input 2, fallback, 1st try) |
| `MEDULLA_HARNESS` | body + hooks | `shell` or the harness name |
| `MEDULLA_LAST_NODE` / `_SIGNAL` / `_MESSAGE` / `_RC` | after the first transition | outcome of the previous node (after a pool `_RC` is empty — a join has no single rc). Timeout is recognizable as rc 124 |
| `MEDULLA_LAST_EVENT_JSON` | after the first transition | the same as one JSON object |
| `MEDULLA_MANIFEST_<NODE>` | after a pool completes | path to its manifest.jsonl (dashes → underscores, uppercased) |
| `MEDULLA_INPUT` | pool input | the input (objects as compact JSON) |
| `MEDULLA_INPUT_INDEX` / `_COUNT` | pool input | 1-based position / total |
| `MEDULLA_INPUT_KEY` | pool input | stable identity `<index>:<sha256[:16]>` — the idempotency key |
| `MEDULLA_INPUT_<KEY>` | pool input | each flat scalar field of an object input, uppercased |
| `MEDULLA_BODY_RC` / `MEDULLA_BODY_SIGNAL` | post hook only | the body attempt's exit code and raw signal |

### Environment the engine reads

| Variable | Meaning |
|---|---|
| `MEDULLA_RETRY_DELAY_S` | pause between attempts / before fallback (default 2 — retry storms hit rate limits) |
| `MEDULLA_RUN_ID` | pre-seed the run id (external correlation) |
| `MEDULLA_STREAM=0` | silence live operator streaming |
| `MEDULLA_IMAGE` | docker: run this ready image instead of building |
| `MEDULLA_DOCKER=1` | set by docker.py inside containers (adapters key off it) |

### Templates (rendered in prompts, shell commands, agent fields)

| Template | Meaning |
|---|---|
| `{{var:KEY}}`, `{{var:KEY:-default}}` | pipeline variable, with optional default |
| `{{file:path}}` | file inclusion, recursive (depth ≤ 10); relative paths resolve against the **including file** |
| `{{input}}` | the pool input; objects render as compact JSON |
| `{{input.a.b:-default}}` | dot-walk into object inputs; missing field without default = render error |
| `{{input_index}}` / `{{input_count}}` | 1-based position / total |
| `{{last.node}}` / `{{last.signal}}` / `{{last.message}}` / `{{last.rc}}` | previous node's outcome — transient tokens (the bridge for agent prompts, which can't read env) |

Rule of thumb: **data flows to shell via env** (quoting-safe — `"$MEDULLA_INPUT_TITLE"` survives any bytes), templates are for slugs, paths and prompts.

### vars vs .env vs docker vars

- `vars:` — workflow **data**: templated, exported to env, persisted in run history, mutable via `<signal:var>`. Reserved names (`PATH`, `HOME`, `LD_*`, `MEDULLA_*`, …) are rejected.
- `.env` — **secrets**: env-only, never templated, never persisted.
- `IMAGE` / `DOCKERFILE` vars — docker image selection (see [Docker](#docker)).

## Examples

### Full pipeline

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
      Branch: {{var:BRANCH}}. Write the plan to plan.md and emit the signal named planned.
    max_attempts: 2
    fallback: {agent: {harness: claude-code, model: sonnet}}
    on_signal:
      planned: panel
      __failed__: __exit_fail__

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
- **Silent model, post as the voice** — some models do the work without saying a word; post verifies the artifact AND emits the routing signal (post-override):
  ```yaml
  post: 'grep -q done-marker out.md && echo "<signal:done>verified</signal:done>"'
  ```
- **DLQ (repair node)** — failed pool inputs are structured data; re-enqueue only them:
  ```yaml
  retry-failed:
    inputs: {shell: "jq -c 'select(.ok|not) | .input' $MEDULLA_MANIFEST_APPLY"}
    max_parallel: 2
    shell: 'bash scripts/fix_one.sh "$MEDULLA_INPUT_ID"'
    on_signal: {__done__: report, __empty__: report}
  ```
- **Error catch-all** — handlers are ordinary nodes routed before the terminal; the payload arrives as `$MEDULLA_LAST_*` (shell) or `{{last.*}}` (prompts):
  ```yaml
  defaults:
    on_signal: {__failed__: notify, __default__: notify}   # root supervisor
  nodes:
    notify:
      shell: 'curl -s "$HOOK" -d "text=[medulla] $MEDULLA_LAST_NODE: $MEDULLA_LAST_MESSAGE"'
      timeout: 30
      on_signal: {__default__: __exit_fail__, __failed__: __exit_fail__}   # own dunders explicit!
  ```
- **Crawler**: producer node → pool → probe node → edge back; the source re-runs on each new node run.
- **Heterogeneous pool**: inputs carry `role`/`prompt` fields; the node prompt is the template (vars resolve), input fragments are inserted inert.
- **Entry cleanup** (crash-only): there is no `finally` — exit hooks are an illusion under `kill -9`. Clean stale state idempotently at the start of the node that needs it clean.
- **Parallel tickets**: the engine guarantees vars isolation; file/git isolation is the body's job (`git worktree` per input), disjointness is the producer's contract. Run one medulla run per workdir at a time (the workdir itself — artifacts, `opencode.json` — is shared).

## Docker

`medulla --docker -w <dir>` re-runs the pipeline inside its image; `scripts/docker.py` owns mounts and credential forwarding, `--build` forces a rebuild, `--mount <dir>` / `--mount-rw <dir>` add extra mounts under `/workspace/<name>`.

Image resolution: `MEDULLA_IMAGE` env → `--var IMAGE` → `vars.IMAGE` (a ready tag: pulled, never built) → otherwise **build** from `--var DOCKERFILE` → `vars.DOCKERFILE` → the packaged default (all four harnesses). Built tags are per-pipeline and content-addressed (`medulla-<name>:<sha of Dockerfile>`) — pipelines never share a tag by accident, and editing a Dockerfile rebuilds automatically.

---

## Technical reference

### Pipeline fields

| Field | Default | Meaning |
|---|---|---|
| `version` | required | must be `"2"` |
| `start` | required | first node |
| `vars` | `{}` | initial variables |
| `timeout` | 86400 | wall-clock deadline for the whole run; `0` = unlimited. Every child timeout is clamped to the remaining budget |
| `defaults` | — | policy defaults for actions: `timeout`, `max_attempts`, `ignore_exit_code`, `fallback`, `on_signal` (per-key merge). Flat scalars only — never merged deep |
| `keep_runs` | 20 | auto-prune of run history on start |

### Node fields

Action (exactly one of `shell` / `agent`):

| Field | Meaning |
|---|---|
| `shell` | shell command; its config *is* the command. `prompt` here is a validation error |
| `agent` | `{harness, model, effort, args}` — one entity, one block. Scalar shortcut: `agent: codex`. `args` is a raw CLI escape hatch — non-portable across harnesses |
| `prompt` | agent input (not config); every scalar action field is a template. The engine appends the signal protocol automatically — name signals in words; literal tags are allowed (see Signals) |
| `timeout` | per **attempt**, seconds |
| `max_attempts` | attempts per runner, default 1. Primary gets N, then fallback gets N |
| `fallback` | alternate agent action after primary attempts are exhausted. Agent-only; a fallback has no fallback |
| `ignore_exit_code` | rc != 0 doesn't classify the body as failed; outcome comes from signals. **Forbidden in pool nodes** — `min_success` owns that role |
| `pre` / `post` | shell hooks around the body (see [Hooks](#hooks-pre-and-post)); in pools both run per input |

Pool (presence of `inputs` turns the action into a pool):

| Field | Meaning |
|---|---|
| `inputs` | YAML list = data (scalars or objects, one kind per pool, arrays forbidden), or `{shell: "cmd", timeout: 60}` = source. A bare string is a validation error. Hard cap: 10 000 |
| `max_parallel` | `1` (default, sequential) \| N \| `all` |
| `min_success` | `all` (default) \| N ≥ 1. Input ok = rc 0 + no timeout + post didn't veto. No short-circuiting ever |

### Classification rules

- A known signal emitted before a non-zero exit **wins** over the exit code.
- Retryable attempt outcomes: non-zero exit, timeout (rc 124), post veto, and **agent silence** (rc 0, no known signal — the most common agent flake). Silence retries on the **primary only** and never triggers fallback (another model drops the tag just as often; blind fallback duplicates side effects); exhausted, it classifies `__default__`. Shell silence is deterministic and not retried. **In pools silence at rc 0 is the normal ok outcome** — pool bodies aren't expected to signal; `post` is their truth channel.
- Pool bodies' signals never route — they're recorded in the manifest (law of layers: inputs produce data, joins produce transitions).
- Fold law: var signals apply **only at `max_parallel: 1`** (which includes every decision node), in input order, from the successful attempt only. At `max_parallel > 1` they land in the manifest row.

### The reserved namespace — all 8 names

Left side: **a bare name is a signal your code emits; a dunder is an engine key.** Right side: **terminals are `__exit_*`.**

| Name | Emitted by | When | Built-in route |
|---|---|---|---|
| `__done__` | pool join | successes ≥ `min_success` | none — **must be routed explicitly** |
| `__failed__` | engine | decision: body died after attempts+fallback. Pool: join below threshold | `__exit_fail__` |
| `__empty__` | engine | zero inputs (source rc 0 with no output, or an empty static list); bodies never run, an empty manifest is still created | `__exit_fail__` |
| `__default__` | matcher | body exited 0 with no known signal | `__exit_fail__` |

Channel words `var` and `update` never route (using them as `on_signal` keys is a validation error). Terminals: `__exit_ok__` (exit 0), `__exit_fail__` (exit 2; the routing signal's message becomes the error message). User nodes may not be named `__*__` or `on/off/yes/no/true/false` (YAML 1.1 traps); node names must be env/filesystem-safe (`[A-Za-z][A-Za-z0-9_-]*`).

### Render model

Phase 1 — file inclusion; phase 2 — one simultaneous, **inert** pass of var/input substitution. **Files are code, values are data**: mustache inside included files resolves fully; mustache inside var/input *values* stays literal (injection-safe by construction). A field rendering empty counts as absent — for optional agent fields only; empty `shell`/`prompt`/`harness` is an error. Rendering happens **once per node run**; retries reuse the same text. A render error on a decision node is `E_RENDER` (broken template); on one pool input it fails that input only (manifest `reason: render`).

### Errors & exit codes

Two failure classes. The test: *fixable by changing data/prompts/retrying?* → workflow failure, the graph decides. *Requires editing the pipeline/environment?* → engine crash.

| Exit | Class | Meaning |
|---|---|---|
| 0 | — | `__exit_ok__` |
| 1 | engine crash | fix the pipeline; retrying is pointless |
| 2 | workflow failure | the graph routed to `__exit_fail__` |
| 130 | interrupt | SIGINT/SIGTERM: children killed first, outcome `interrupted`, resumable |

Crash codes: `E_VALIDATION` (schema/XOR/unknown target/name traps/bare keys in pool routing/defaults-inherited self-edges), `E_RENDER`, `E_DEADLINE` (pipeline timeout), `E_INPUTS` (source exited non-zero or emitted mixed-kind/array elements — a broken producer is not an empty queue), `E_INPUTS_LIMIT`, `E_HARNESS` (**only** "binary missing/unresolvable" — an agent process dying is class B, retryable), `E_INTERNAL`. Class A is never routable in-graph: the graph itself is what's broken.

Error handling in the graph: node-level edge → `defaults.on_signal` catch-all → built-ins (a three-tier supervision chain; see the catch-all example above). The validator rejects a defaults-inherited edge pointing at its own node; explicit self-loops stay legal. A pool's `__failed__` message is pre-aggregated (`"2/5 inputs ok (min_success=3); rc!=0 x2, timeout x1"`). Every run ends with an atomic `outcome.json`:

```json
{"outcome": "failed", "exit_code": 2,
 "error": {"code": "SIGNAL_FAIL", "message": "no repo access: git clone rc=128",
           "node": "plan", "step": 12, "signal": "blocked"},
 "steps": 12, "duration_s": 4180}
```

A fixed delay separates attempts and the fallback switch (`MEDULLA_RETRY_DELAY_S`, default 2s).

### Lifecycle

**Boot** — parse CLI → load yaml (`version: "2"` or a migration error) → validate everything (`E_VALIDATION` before any run dir exists) → create `runs/<ts>-<run_id>/` (per-run flock: one writer across processes), snapshot the config, seed vars, prune old runs → set the deadline.

**Node loop** — per node: deadline check → materialize inputs (no key → phantom input; list → snapshot; source → render, execute, sniff) → run the pool (`max_parallel` workers; per input: render once → pre → attempts+fallback with post per attempt) → classify → resolve the edge → append the journal row → next node or terminal.

**Finish** — atomic `outcome.json`; crashes write it with the `E_*` code; SIGINT/SIGTERM kill all live children first, then write `interrupted`.

**Resume** — `--resume` picks the latest run without `outcome.json`, or with outcome `interrupted`/`crashed` (deliberate: the #1 resume trigger is the `E_DEADLINE` crash). The **snapshot** config is reloaded (a run's config is immutable), vars and journal position restored; an interrupted pool continues from the manifest done-mask (identity `(index, key)`; sources never re-execute); an interrupted decision node re-runs whole (body idempotence is the author's concern). The deadline is fresh per invocation.

### Layout

A pipeline is a self-contained directory — contract, code, history:

```
.medulla/pipelines/<name>/
  pipeline.yaml            # the contract
  prompts/  scripts/       # its code
  .env  .gitignore         # secrets (env-only) / seeded by init (.env, runs/)
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
      004-apply/input-0002/              # per-input namespace (prompt, attempts, hooks)
      004-apply/manifest.jsonl           # {index, key, input, ok, reason, signal, message,
                                         #  rc, timed_out, attempts, fallback, harness, model,
                                         #  vars, updates, duration_s, log}
```

Retention: keep the newest `keep_runs` finished runs; unfinished dirs younger than the timeout are never pruned. History browsing needs no CLI: `ls runs/`, `cat outcome.json`.

### Execution details

Shell bodies and hooks run via `$SHELL -lc` (login shell — your PATH applies). Each child gets its own process group; on timeout the whole group is SIGTERMed, then SIGKILLed. Attempt logs stream to `steps/.../attempt-N-<tag>.txt` as they arrive (`tail -f` works mid-run).

### Harness notes

Signal filtering: claude-code/codex scan **assistant text** mined from their JSON streams (tool output can never route); opencode/agy have no structured output — signals must start a line, and never quote signal syntax in prompts. opencode's output is merged from stderr (that's where it talks) and ANSI-stripped. `effort` maps to: claude `--effort`, codex `model_reasoning_effort`, opencode `reasoningEffort` (config), agy model-name suffix. agy refuses to run in a workspace it doesn't trust (fail-fast instead of hanging; skipped in Docker).

Adapters also configure the CLIs themselves (not your API — listed for debugging): claude gets `API_TIMEOUT_MS` and a stripped `ANTHROPIC_API_KEY` (the OAuth account must win); codex gets `-c stream_idle_timeout_ms` and prefers the `cx` token-refreshing wrapper; opencode gets its config via `OPENCODE_CONFIG_CONTENT` (permission allow, provider timeout, per-model reasoningEffort — no opencode.json is written); agy gets `--print-timeout`. All inner timeouts are sized from the step timeout + 300s slack so the engine always kills first.

Development: `live-tests/` in the repo holds 20 battle pipelines that run the real CLIs (adapters, pools, fallback, interrupt, resume) — `live-tests/run-all.sh` before release pushes. Unit suite: `cd cli-agent && pytest`.

### Migrating from v1

| v1 | v2 |
|---|---|
| `stages` / `starting` | `nodes` / `start` |
| `runner:` / `llm:` / `executor`+`command` | action fields directly on the node: `agent: {...}` xor `shell` |
| `loop:` + `list:` + `fetch:` + `parallel: true` | `inputs:` on the node + `max_parallel` |
| `done: __next_item__` + `loop_done` | pool joins route `__done__`/`__failed__`/`__empty__` via `min_success` |
| `max_iterations` / `reset_iterations` / `on_max` | removed — budget gate pattern (vars) |
| hardcoded `max_rounds=500` | pipeline `timeout` (wall-clock deadline) |
| `round_timeout` / `fallback_runner` | `defaults: {timeout, fallback}` |
| `__exit__` | `__exit_ok__` / `__exit_fail__` |
| `ignore_rc` | `ignore_exit_code` (decision nodes only) |
| `{{__item__}}` / `{{__list_item__}}` | `{{input}}` family + `MEDULLA_INPUT_*` env |
| `MEDULLA_TASK_ID` | `MEDULLA_RUN_ID` |
| `.medulla/vars.<task>.yaml` | `.medulla/pipelines/<name>/runs/<id>/` |
| `--stage` | `--node` |
| `install-skill` | `init <name> --skill` |
| gemini executor | removed (use `agy`) |
| exit codes 0/1/2/3 ad-hoc | 0 ok / 1 engine crash / 2 workflow fail / 130 interrupt |

### Reserved (designed, not implemented — in priority order)

`stall_timeout` (no-stdout watchdog for hung agents; partially covered by per-harness flags) · manifest attempt states (`claimed|started|completed`) + per-node resume policy `rerun|skip|probe` · `resume:` (continue a node's harness session — phase 2) · cost telemetry + pre-guard budgets (phase 2) · `cancel_rest` (race joins) · `on_input_fail: abort` (fail-fast pools) · `format:` on sources (override sniffing) · `finally` (best-effort only, if reality ever demands it) · agent-block defaults.
