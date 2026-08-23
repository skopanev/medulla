"""The text `medulla init` writes: a starter workflow, its README, its SKILL.md.

Split from init.py under the project's 250-line rule ($MAX_LOC). Templates are prose,
not logic — keeping them here means a change to what a new workflow SAYS never touches
the code that decides where it goes.
"""
WORKFLOW_YAML = """\
# <NAME> — describe what this workflow does in one line.
#
# Run:      medulla -w <NAME>          (a bare name resolves from any directory)
# Explore:  medulla -w <NAME> --dry-run
# Launching: medulla help    (paths, docker, where the answer lands)
# Full API:  medulla --help  (env vars, signals, docker)
version: "2"
start: hello

# vars: {BRANCH: main}          # {{var:BRANCH}} in prompts/commands
# defaults:
#   timeout: 1800
#   fallback: {agent: {harness: claude-code, model: opus}}

nodes:

  hello:
    shell: |
      echo "hello from <NAME>"
      echo "<signal:ok>it works</signal:ok>"
    timeout: 60
    on_signal: {ok: __exit_ok__}

  # An agent node (delete hello above, rename this to your liking):
  # work:
  #   agent: {harness: claude-code, model: sonnet}
  #   prompt: |
  #     {{file:prompts/task.md}}
  #     Do the thing, then emit the signal named done.
  #   post: 'test -s artifact.md'         # the truth channel: verify, don't trust
  #   max_attempts: 2
  #   on_signal: {done: __exit_ok__, __failed__: __exit_fail__}

  # A pool (fan-out over inputs; the join routes, bodies write to the manifest):
  # sweep:
  #   inputs: {shell: "ls *.md"}          # or a YAML list, or JSON/JSONL output
  #   max_parallel: 4
  #   min_success: 1
  #   shell: 'echo "processing $MEDULLA_INPUT"'
  #   on_signal: {__done__: __exit_ok__, __empty__: __exit_ok__}
"""

WORKFLOW_README = """\
# <NAME>

A medulla workflow. Edit workflow.yaml; keep prompts in prompts/.

- run:        medulla -w <NAME>            (bare name; ./.medulla wins over ~/.medulla)
- dry run:    medulla -w <NAME> --dry-run
- resume:     medulla -w <NAME> --resume
- launching:  medulla help    (which workflows resolve here, and the exact command)
- reference:  medulla --help  (all MEDULLA_* env vars and signal syntax)
- history:    runs/<ts>-<id>/  (journal, per-step logs, outcome.json)
- secrets:    put KEY=VALUE into .env here — children see them as env,
              templates and run history never do
"""

GITIGNORE = ".env\nruns/\n"

SKILL_MD = """\
---
name: <NAME>
description: |
  One paragraph: when should an agent reach for this workflow?
  Trigger phrases, use cases, what it returns.
---

Run the workflow and read its result:

    medulla -w <NAME> --var "KEY=VALUE" >&2
    # outputs land in the newest runs/<ts>-<id>/ directory
"""
