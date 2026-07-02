# AGENTS.md

## Git

- **NEVER add "Co-Authored-By"** to commits
- **NEVER `git add, commit, push`** unless the user explicitly asks.

## Tooling

- **Use dedicated tools first.** Use `Read` instead of `cat`/`head`/`tail`, `Write`/`Edit` instead of shell text rewriting, `Glob` instead of `find`/`ls`, and `Grep` instead of shell `grep`/`rg` when a dedicated tool exists.
- **Use Bash only for real shell execution.** Reserve shell for commands that actually need terminal execution, such as git, builds, tests, package managers, and scripts.

## Core principles

- **LLM writes code, shell verifies.** LLMs CANNOT be trusted to run exact commands — they pipe, modify, redirect, skip. All lint/build/test MUST run in mechanical shell stages, NEVER in LLM prompts. The LLM's job is to write code. The shell's job is to verify it compiles, lints, and passes tests.
- **Build systems, not adhoc fixes.** Prefer durable workflow/platform improvements over one-off patches.

## Docker architecture

- **Image = public packages/executables only.** The docker image contains system deps (node, python, claude, codex, opencode, etc.). Rebuild (`--build`) only when adding/updating these.
- **Runtime is symlinked, not copied.** `install-skill`/`init` symlink `.medulla/medulla` and `.medulla/scripts` to the live install. Docker mounts the workspace at `/workspace`. Code changes take effect on `medulla upgrade`, never requiring `--build`.
- **Never suggest `--build` for code changes.** Only `medulla upgrade` is needed to pick up changes to scripts, prompts, or workflows.

## Prompt authoring

- **Action verbs on every step.** Every execution step in a prompt must start with an explicit verb:
  - `Run` — for CLI tools (`Run tk show ...`, `Run tk-tag ...`)
  - `Execute` — for git/shell commands (`Execute git checkout ...`)
  - `Emit` — for signals (`Emit <signal:ready>...</signal:ready>`)
- **No Signal Output sections.** Do not list available signals at the top of prompts. Signals appear only inline at their exact execution point.
- **No signal examples with rendered vars.** Never put `{{var:...}}` inside signal examples outside of execution steps — rendered values cause models to copy-paste and stop.
