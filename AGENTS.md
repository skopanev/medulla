# AGENTS.md


- **How to work (not what to build): `~/Projects/contract/general-contract.md` is the law, `contract.md` is this repo's delta** — `$TRUNK`/`$CHECKS`, class-X here, env traps. This file stays the architecture law; the pair does not restate it.

## Versioning

- **Any behavioral change bumps the version** (4.0.0 -> 4.0.1) in pyproject.toml,
  in the same commit. pipx/pip silently skip same-version git installs — the
  container self-heal and user upgrades depend on the bump.

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

<!-- NTK -->

### Tickets

**CRITICAL:** ALL task management MUST use `ntk` CLI. NO other tools. NO Notion API. NO exceptions.

Drafting depth depends on the task. For trivial one-liners ("just add XXX", "update README", "bump version") — DO NOT scour the codebase, take the task as-is. For substantive tickets — draft the description independently using codebase context. NEVER ask the user to fill in details that can be inferred from the codebase or the internet. Only ask if the info genuinely cannot be found.

**Description Format (`-d`):**
```
## Summary
Business-level what/why. Max 2 sentences.

## Expected Outcome
The concrete result/value delivered when this is done.

## Details
- Implementation specifics, affected files/modules, technical approach
- Reference code by symbol — function/class/block name or a unique snippet — NEVER by line number. Line numbers go stale as files shift.
- Edge cases, constraints, dependencies

## Acceptance Criteria
- [ ] Independently verifiable checklist item
- [ ] Independently verifiable checklist item. NO vague "works correctly". Define "correct".
```

**Always pass `-d` (and `-A`) via a heredoc:**
```
ntk create "title" -p med -d "$(cat <<'EOF'
## Summary
...
EOF
)"
```

**Ticket Rules:**
- **Trigger:** Run `ntk create` ONLY when the user explicitly asks to file a ticket. Fixing bugs, reviewing code, answering questions — NOT a trigger.
- **Initiative:** Expand user one-liners into full tickets using codebase context — but only when the task warrants it (see above).
- **Clarity:** NEVER create vague tickets. Ask questions FIRST if ACs cannot be written.
- **Closing:** Before running `ntk close` (or moving to `done`) you MUST append a comment via `ntk update <id> -A "..." --force` describing WHAT WAS DONE — how it was fixed, key files/decisions. No comment, no close.

Project is auto-set via `.ntkrc`. Outside a repo pass `-P <name>` (list via `ntk projects`).

No `.ntkrc` and user named a project? Match it against `ntk projects` (case-insensitive, fuzzy), then pass `-P <matched-name>`.

**Workspaces (databases):** Repo → one DB via `workspace` in `.ntkrc` (else default). Workspaces NEVER mix; don't switch it yourself. Override per-command with `-W <name>`; `-W all` reads all (`ls` only). List: `ntk workspaces`.

**Vocabulary:** Statuses, types and priorities are per-database. `ntk help` prints the ones this workspace actually has; `ntk schema` shows them live. Never assume a fixed list.

**Commands:**
- `ntk ls [-s status,status] [-a initials,initials] [-t tags] [--since YYYY-MM-DD] [--progress]` — List (comma-separated for multiple statuses/assignees; `--since` filters by creation date). `--progress` replaces the table with a closed/total ratio + per-status breakdown for the filtered set (respects `-P`/`-t`/`-a`; works with `-W all`).
- `ntk show <id> [id...]` — View (pass multiple ids space- or comma-separated)
- `ntk start <id>` — Mark `in_progress`
- `ntk close <id>` — Mark `done`
- `ntk rm <id> [id...] [-y]` — Move ticket(s) to the Notion trash (restorable there). Notion has no permanent-delete API. Prefer `ntk close` for finished work; `rm` is for tickets that should never have existed.
- `ntk next [-a initials] [-P project]` — Pick next
- `ntk deps <id> | -t <tag>[,tag] [-P proj]` — Show dependency tree for one ticket (with `N/M done`, `[ready]`/`(waiting on N)`) or a forest for all tickets carrying the tag(s); external blockers shown as `↗`
- `ntk users` — List assignees
- `ntk schema` — Show the live database fields, options and status groups (also refreshes the cached schema)
- `--reload-schema` (global, before the command) — refetch the cached schema after the Notion database changes
- `ntk projects` — List projects from Notion (refreshes global config)
- `ntk workspaces` — List configured workspaces (databases); marks the default and the active one
- `-W <name>` (global, before the command) — run against a specific workspace; `-W all` reads across all (`ls` only)
- `ntk create <title> [-p priority] [-a initials] [-s status] [-T type] [-t tags] [-d text] [-i file] [-P project] [--deps tid,tid] [--due YYYY-MM-DD]`. `-i <path>` attaches a file/image (repeatable or comma-separated, ≤50MB each).
- `ntk update <id> [id...]` — Modify (same flags as create + `-d text` to REPLACE body + `-A text` to append + `--title text`). By default only not-yet-started tickets may be updated — every status in the schema's "To-do" status group (`open`, plus whatever else that group holds in this workspace); pass `--force` to intentionally update a started or finished one. Multiple ids: same flags applied to each; if any id doesn't resolve or any ticket is protected, nothing is updated. `--deps` accepts `tid,tid` (replace), `+tid,-tid` (add/remove), or `""` (clear); `--due` accepts a date or `""` to clear.

### Reviewing Agent Work

Trigger: user asks "what's done?" / "let's check it" / similar.

Process **one ticket at a time** — never batch. Start with the first ticket in `ntk ls -s to_test -t agent-done`, finish it (GO or NO-GO), then move on.

Queue >1 ticket on separate branches? Review **ONLY in a worktree**: `git worktree add ../<repo>-review <base>`. Drop it when done.

Agent branches are stale — base could change during run. Reconcile overlaps yourself. Escalate only if blocked.

1. `ntk show <id>` — read request + agent log.
2. `git fetch origin && git diff --stat origin/<base>..origin/ntk/<id>` for the default path (base = project's main branch).
   - If the agent note says `Agent: ветка <base-override>@<commit>`, this was a `base:<branch>` task. Review that commit/branch directly; do not expect `origin/ntk/<id>`.
3. Summarize: what changed, flag anything off-topic or junk (files unrelated to the ticket).
4. Give your own short, concise verdict — one sentence — then ask the user: **GO / NO-GO?**
   - **GO:** sync base (`git switch <base> && git pull --rebase`), `git checkout origin/ntk/<id> -- <task files only>` (skip junk). If file already modified — edit by hand, no `checkout`. Commit + push, `ntk update <id> -s done -A "merged: ..." --force`, `git push origin --delete ntk/<id>` (only after merge push confirmed).
   - **GO for `base:<branch>` task:** do not merge/delete `origin/ntk/<id>`. The commit is already on the shared branch; after review, mark the ticket `done` with a comment like `reviewed: <branch>@<commit>`.
   - **NO-GO:** show the issues, discuss. Nothing else.

<!-- /NTK -->
