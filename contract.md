# medulla — project contract

**Read `~/Projects/contract/general-contract.md` first. It is the law.**
Project-agnostic: when to panel, what blocks, what escalates, what lands. It
lives in its own repo (`git.otion.us:healthium/contract`) — clone it there if
the path is missing.

This file is the delta for this repo (`~/Projects/medulla`). **Only what differs.**

Two rules keep it that way:

- **Nothing here restates the agnostic contract.** If a rule applies to any
  project, it belongs there, not here. A copy is a second source of truth and it
  will drift.
- **Where the two disagree, the agnostic contract wins and this file is the bug.**

## Config

| Var | Value |
|---|---|
| `$TRUNK` | `main` |
| `$INSTALL` | `./install.sh` — pipx |
| `$BUILD` | none — pure Python |
| `$CHECKS` | `ruff check` (config in `ruff.toml`) · the suite under `live-tests/` — **confirm the exact invocation with `$APPROVER` before relying on it; it is not stated in `AGENTS.md`** |
| `$APPROVER` | Sergey (repo owner) |
| `$MAX_LOC` | 250 lines per source file |

## File size — 250 lines

A source file over `$MAX_LOC` is a defect here. Past that nobody holds the whole file,
review degrades to the diff, and the next change lands beside the code it contradicts
instead of replacing it.

- **It binds when you TOUCH the file.** Adding to one already over the line means
  splitting it, or carving out the part you came for — not "one more function, it was
  already big".
- **Split by what the code IS.** Four files named `part1..4` obey the number and lose
  the point.
- **A file you did not touch is not your ticket.** Write it down; do not drive by.

Known debt, none of it introduced by this rule (measured 2026-08-23):
`engine.py` 1212 · `scripts/docker.py` 1020 · `harness.py` 619 · `contract.py` 419 ·
`rundir.py` 273. Only `cli.py` (247) is under. Each of these is class-X, so a split is
a panel-reviewed change, not a drive-by — which is exactly why they are listed rather
than quietly carried.

## class-X here

Beyond the standard set: **anything under `cli-agent/medulla/v2/`**. This is the
engine every project on the host runs through — `docker.py`, `rundir.py`,
`harness.py`, `cli.py`. A regression here does not fail loudly in one project;
it fails quietly in all of them at once, and the symptom reads as the harness
misbehaving. One such change shipped and read for a day as a provider quota
problem.

Also: **the bundled `workflows/spar/`**. It is the machine-wide definition every
repo falls back to.

## Env traps

- **Every project image installs the engine from git HEAD.** A change landed
  here reaches containers on their next image build, not on their next run, and
  a cached `pipx install` layer can hold an old version indefinitely. Verify
  with `medulla --version` **inside** the image.
- **`AGENTS.md` is binding on how work is verified here:** lint, build and test
  run in mechanical shell stages, never inside an LLM prompt. An agent that
  reports a green check it ran conversationally has not run it.
