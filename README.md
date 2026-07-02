# medulla

**medulla is a state machine for AI agents.** You describe work as a YAML pipeline — a set of stages — and medulla runs each stage on an agent (Claude, Codex, Gemini, or OpenCode). Stages emit signals; signals decide the next stage. Built in: retry, per-stage fallback to another model, and crash recovery.

Use it to chain agents into a repeatable workflow instead of driving them by hand.

This repository contains:

- `cli-agent/` — the runner, executors, scripts, and the `spar` workflow

## Setup

```bash
# Install from GitHub
pip install git+https://github.com/skopanev/medulla.git

# Install a bundled skill — copies the workflow AND provisions the runtime
# (.medulla/medulla + .medulla/scripts are symlinked to the live install)
medulla install-skill spar

# Run any workflow
medulla -w <workflow>
medulla -w <workflow>
medulla -w <workflow> --var KEY=VALUE

# Run in Docker with mounted repos
medulla --docker -w <workflow> --build \
  --mount ../keys \
  --mount-rw ../backend

# Host-builder (separate terminal - bridges java, gradle, maestro, xcodebuild to host)
.medulla/scripts/host-builder.sh
```

## CLI Reference

`medulla` supports two invocation styles (the `medulla` command comes from pip; from a source checkout use the repo-root `./medulla` launcher).

- Workflow mode: `medulla -w <workflow> [options]`
- Direct pipeline mode: `medulla <run|validate|graph> <pipeline.yaml> [options]`

### All CLI keys

- `-w`, `--workflow <name>` - resolve `.medulla/workflows/<name>/pipeline.yaml` and run that installed workflow.
- `run <pipeline.yaml>` - run a pipeline by explicit file path.
- `validate <pipeline.yaml>` - validate a pipeline and exit without running stages.
- `graph <pipeline.yaml>` - generate and open a graph for a pipeline file.
- `--var KEY=VALUE` - override a pipeline variable; repeatable; values split on the first `=` only.
- `--docker` - run through the Docker wrapper instead of directly on the host.
- `--mount PATH` - Docker mode only; mount an existing extra directory **read-only** at `/workspace/<dirname>`.
- `--mount-rw PATH` - Docker mode only; mount an existing extra directory **read-write** at `/workspace/<dirname>`.
- `--host` - auto-start `.medulla/scripts/host-builder.sh`, the host bridge for native tools such as `xcodebuild`, `cargo`, `gradle`, `java`, and `maestro`.
- `--stage STAGE` - skip to a specific stage (bypass earlier rounds; requires vars from a prior run or `--var`).
- `--dry-run` - load/resolve the pipeline without executing stage commands.
- `--verbose` - enable verbose logging and executor diagnostics.
- `--build` - Docker mode only; force image rebuild via `scripts/docker.py` with `--no-cache`.
- `graph` generates a graph file for the pipeline.
- If neither `-w` nor a valid `<command> <pipeline>` pair is provided, `medulla` exits with usage help.

### Common command patterns

```bash
# Run an installed workflow
medulla -w <workflow>

# Run a workflow with variables
medulla -w <workflow> \
  --var KEY=VALUE \
  --var OTHER_KEY=OTHER_VALUE

# Dry-run a workflow locally
medulla -w <workflow> --dry-run --verbose

# Validate a raw pipeline file
medulla validate path/to/pipeline.yaml

# Generate a graph for a raw pipeline file
medulla graph path/to/pipeline.yaml

# Run in Docker with extra mounts
medulla -w <workflow> --docker --build \
  --mount ../readonly-dir \
  --mount-rw ../writable-dir
```

### Docker wrapper details

When `--docker` is used, the wrapper additionally supports these behaviors:

- Image name comes from `MEDULLA_IMAGE` or defaults to `medulla:latest`.
- Dockerfile resolution prefers `workflows/<workflow-root>/Dockerfile`, then falls back to the root `Dockerfile`.
- Local config mounts are added automatically when present: `~/.claude`, `~/.codex`, `~/.gemini`, opencode config/auth, `~/.gitconfig`, and the host-builder bridge at `/tmp/medulla-bridge`.

Forwarded environment variables:

- `ANTHROPIC_API_KEY` - Anthropic credential.
- `OPENAI_API_KEY` - OpenAI credential.
- `ZHIPU_API_KEY` - Zhipu credential.
- `GEMINI_API_KEY` - Gemini credential.
- `GOOGLE_API_KEY` - Google API credential.
- `GOOGLE_CLOUD_PROJECT` - Google Cloud project id.
- `GOOGLE_APPLICATION_CREDENTIALS` - path to Google credentials.

For custom pipeline inputs, use `--var KEY=VALUE`.

### Companion commands

| Command                          | Description                                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `medulla init`                | Provision the `.medulla/` runtime (symlinks `medulla/` + `scripts/` to the live install, creates `snapshot/`). `install-skill` runs this for you. |
| `medulla upgrade`             | Self-upgrade by reinstalling the latest from GitHub.                                                                                 |
| `medulla-graph -w <workflow>`       | Render a workflow pipeline as a graph.                                                                                                |

## Workflow Structure

A workflow is a directory with a `pipeline.yaml` entry point plus any prompts/scripts it references.

Typical layout:

```text
workflows/<workflow>/
├── pipeline.yaml
├── prompts/
└── scripts/
└── ... any other fodlers you need
```

Minimal pipeline shape:

```yaml
version: "0.1"
starting: first-stage
round_timeout: 1500 # global timeout per stage

vars: # global variables available to all stages
  KEY: default

fallback_runner: # global fallback runner if a stage fails
  llm: claude-code
  model: sonnet

stages:
  first-stage:
    runner:
      llm: claude-code
      model: sonnet
      prompt: |
        Do the task.
    on_signal:
      completed: __exit__ # transition to exit stage
      failed: stage-name # transition to a named stage
      default: __exit__
```

Main top-level fields:

- `version` - pipeline format/version label.
- `starting` - name of the first stage.
- `round_timeout` - default timeout budget for stage runs.
- `vars` - default pipeline variables.
- `fallback_runner` - optional pipeline-wide fallback runner if a stage runner fails.
- `stages` - map of stage definitions.

### Stage shape

Each stage is a named block under `stages`.

```yaml
some-stage:
  max_iterations: 3
  runner:
    shell: ./scripts/do-work.sh
    timeout: 60
  fallback_runner:
    llm: opencode
    model: openai/gpt-5.4
  on_signal:
    ready: next-stage
    failed: __exit__
    default: __exit__
```

Stage fields:

- `runner` - required; defines how the stage executes.
- `finally` - optional cleanup runner; executes after every stage exit (success, failure, timeout, on_max) before transitioning.
- `fallback_runner` - optional stage-local fallback.
- `on_signal` - required; transition table keyed by emitted signal name.
- `max_iterations` - optional safety cap for repeated returns to the same stage.

### Runner types

A stage runner must define exactly one of these:

- `shell` - run a shell command.
- `llm` - run an LLM executor.
- `loop` - iterate over a list and run an inner runner for each item.

Shell runner:

```yaml
runner:
  shell: |
    python3 scripts/check.py
  timeout: 60
```

LLM runner:

```yaml
runner:
  llm: claude-code
  model: sonnet
  prompt: |
    {{file:prompts/task.md}}
  timeout: 1800
```

Loop runner:

```yaml
runner:
  loop:
    list: "python3 scripts/list_items.py"
    fetch: "cat {{__list_item__}}"
    runner:
      llm: claude-code
      model: sonnet
      prompt: |
        Item: {{__item__}}
```

Loop fields:

- `list` - shell command that produces items to iterate.
- `fetch` - optional shell command that loads the current item contents into `{{__item__}}`.
- `runner` - inner shell/llm runner executed for each item.

Loop stages must map one signal to `__next_item__` and must define `loop_done` in `on_signal`.

### Variables and templating

Variables come from three places:

- `vars` in `pipeline.yaml`
- CLI overrides from `--var KEY=VALUE`
- runtime `<signal:var>` emissions from stages

Templates supported inside prompts and shell commands:

- `{{var:KEY}}` - insert variable value.
- `{{var:KEY:-default}}` - insert variable with default fallback.
- `{{file:path/to/file}}` - inline file contents.
- `{{__list_item__}}` - current loop item identifier/path.
- `{{__item__}}` - fetched content for the current loop item.

Variable signals look like this:

```xml
<signal:var key="TARGET_DIR">backend</signal:var>
```

That stores `TARGET_DIR=backend` and makes it available to later stages.

### Calling LLMs

LLM stages use `runner.llm` plus an optional `model`.

Examples:

- `llm: claude-code`
- `llm: claude-code` with `model: sonnet`
- `llm: opencode` with `model: openai/gpt-5.4`
- `llm: gemini`

The `prompt` field is the full rendered prompt sent to that executor.

### Signals and transitions

Stages communicate by emitting XML-like signals in stdout/stderr output:

```xml
<signal:ready>done</signal:ready>
<signal:failed>something broke</signal:failed>
<signal:update>progress message</signal:update>
```

`on_signal` maps those signals to the next action.

Simple transition:

```yaml
on_signal:
  ready: next-stage
  failed: __exit__
  default: __exit__
```

Transition with options:

```yaml
on_signal:
  approved:
    stage: next-stage
    reset_iterations: true
```

Transition with hook steps before moving on:

```yaml
on_signal:
  approved:
    - runner:
        shell: python3 scripts/post_process.py
    - stage: next-stage
```

Special targets:

- `__exit__` - stop the pipeline.
- `__next_item__` - move to the next loop item.

Special signals:

- `default` - used when nothing matched.
- `loop_done` - required for loop stages when iteration completes.
- `on_max` - optional route when `max_iterations` is exceeded.
- `update` - progress only; does not transition.
- `var` - stores variables; does not transition.

### Fallbacks

Fallbacks let the runner retry a failed stage with another runner.

- pipeline-level `fallback_runner` applies globally
- stage-level `fallback_runner` overrides it for one stage

Use this when the main executor is flaky, overloaded, or you want a second model as backup.

## Repository Structure

```text
.
└── cli-agent/
    ├── medulla/                # runner, executors, parsing, output, CLI
    ├── scripts/                # docker runner, host bridge, helpers
    └── workflows/              # pipeline definitions and prompts (spar)
```

## Docker

Each workflow has its own `Dockerfile` (resolved from `vars.DOCKERFILE` in its `pipeline.yaml`).

```bash
# First run (auto-builds if image missing)
medulla --docker -w .medulla/workflows/spar

# Force rebuild
medulla --docker -w .medulla/workflows/spar --build

# Mount sibling repos
--mount ../backend             # read-only at /workspace/backend
--mount-rw ../ai-mobile-apps   # read-write at /workspace/ai-mobile-apps
```

Code changes: `medulla upgrade` (runtime is symlinked to the live install). Never `--build` for code — prompts/scripts are mounted, not baked.

Rebuild only when Docker image packages change (node, python, AI tools).

### Host Builder

Runs on Mac, bridges tools to Docker container via `/tmp/medulla-bridge`.

```bash
.medulla/scripts/host-builder.sh
```

Protocol:

- Request ID protocol: `id:<pid.counter> <cmd>` -> response/exit_code in `response.<id>` / `exit_code.<id>` (eliminates race conditions)
- Normal: `tool args` -> wait -> response + exit_code
- Background: `bg:command` -> returns PID immediately
- Kill: `kill:<PID>` -> terminates background process
- On shutdown: kills all tracked BG_PIDS (firebase, emulators)

Bridged tools: java, gradle, xcodebuild, maestro, cargo.

## Project Files After `install-skill` / `init`

```text
project/
└── .medulla/                # runtime (symlinked to the live install)
    ├── medulla   -> <install>/medulla
    ├── scripts   -> <install>/scripts
    ├── workflows/            # installed workflows (e.g. spar)
    ├── snapshot/             # per-round artifacts
    └── vars.yaml             # pipeline state (auto-managed)
```

## Signals

Agents emit `<signal:NAME>body</signal:NAME>`. One per line. Raw XML - no backticks, no markdown.

Built-in: `var` (set variable), `update` (progress). Everything else drives stage transitions.

## Executors

| Executor      | CLI                                                                 |
| ------------- | ------------------------------------------------------------------- |
| `shell`       | `bash -lc`                                                          |
| `claude-code` | `claude --dangerously-skip-permissions --output-format stream-json` |
| `codex`       | `codex exec --json --dangerously-bypass-approvals-and-sandbox`      |
| `gemini`      | `gemini --approval-mode yolo`                                       |
| `opencode`    | `opencode run --agent build`                                        |
