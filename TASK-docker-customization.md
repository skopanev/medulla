# Task: declarative per-workflow Docker customization (`docker:` block)

## Problem
`medulla --docker` mounts the workflow's working dir as `/workspace` and forwards creds.
Today the mount set + network mode are hardcoded in `scripts/docker.py`. Real workflows need
to customize the container's exposure — with NO way to declare it except editing engine source.

Concrete driver (a personal-OS ingest chain): when a workflow chews **untrusted input**
(incoming email), the container must NOT be able to read the vault's `secrets/` dir (API tokens,
OAuth creds), because a prompt-injection could read a secret and exfiltrate it via the workflow's
own output (which gets git-pushed). The load-bearing defense is: **shadow `secrets/` so the
container sees it empty**, while the host still reads it normally. There is no way to express this
today.

## What to build
A declarative `docker:` block in `workflow.yaml`, consumed by `scripts/docker.py`, that lets a
workflow customize its container. Minimum viable fields:

```yaml
docker:
  shadow: [secrets, .git]     # workspace-relative paths → mount empty tmpfs OVER them
                              # (host keeps real content; container sees them empty)
  network: none               # optional: container --network mode (default: current behavior)
  # (extra rw/ro mounts already exist via --mount/--mount-rw CLI; optionally fold them here too)
```

Semantics:
- **`shadow`**: for each path, after the `/workspace` bind-mount is added, append a mount that
  makes `/workspace/<path>` appear **empty** inside the container (an empty `--tmpfs` over the
  subpath is the standard technique — a later, more-specific mount shadows the bind mount).
  Host filesystem is untouched. If the path doesn't exist on host, no-op (don't fail).
- **`network`**: pass through to `docker run --network <value>` (e.g. `none` for cognition-only
  nodes that need no outbound network beyond… note: if `none`, the LLM API call must still work
  — verify whether the harness reaches the API over network; if the API needs egress, `none`
  breaks it, so document that `none` is only for shell/offline nodes, OR support an allowlist
  later. For THIS task, just plumb the flag through and document the caveat.)

## Where
- `scripts/docker.py` — `build_volumes()` builds the `-v` args; add shadow mounts there. The
  `docker run` assembly is where `--network` / `--tmpfs` get appended. Read the workflow.yaml's
  `docker:` block (the engine already parses workflow.yaml — thread the block through to docker.py).
- Schema/validation — wherever workflow.yaml is validated, accept the `docker:` block; unknown
  keys under it → clear error (fail-fast, don't silently ignore).

## Design constraints
- **Backward compatible**: no `docker:` block → today's exact behavior.
- **Declarative, git-tracked**: lives in workflow.yaml, travels with the workflow (both machines
  running it get the same container policy). NOT a CLI flag you must remember.
- **Fail-fast on misuse**: `shadow` path that escapes the workspace (absolute / `..`) → error.
- **Minimal**: don't build a full mount DSL. shadow + network is enough for the driver. Extra
  mounts can stay on the CLI for now.

## Acceptance criteria
1. A workflow with `docker: {shadow: [secrets]}` → inside the container, `ls /workspace/secrets`
   is **empty**, while on the host `secrets/` still has its files. `docker info` and the claude
   agent auth (via forwarded `CLAUDE_CODE_OAUTH_TOKEN` env) still work.
2. A workflow WITHOUT a `docker:` block → byte-identical `docker run` args to before this change.
3. `docker: {shadow: ["/etc"]}` or `["../foo"]` → validation error (path escapes workspace).
4. `docker: {network: none}` → `docker run … --network none …` present.
5. Version bump (SHA changes) so pinning picks it up.

## Out of scope (note, don't build)
- Per-node (vs per-workflow) docker policy — workflow-level is enough now.
- Egress allowlist for `network` — just plumb the raw flag.
- Harness tool-restriction (allowed-tools per node) — related least-privilege story, separate task.
