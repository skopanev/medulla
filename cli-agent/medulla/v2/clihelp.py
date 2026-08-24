"""The reference text `medulla --help` prints: the env API, the signal grammar, docker.

Split from cli.py under the project's 250-line rule ($MAX_LOC). It is prose the engine
publishes as a contract — agents read it to learn what they may rely on — so keeping it
apart means editing what the engine PROMISES never touches the code that parses flags.
"""
ENV_HELP = """\
new here? `medulla help` — the launch contract, plus the workflows that resolve on
this machine, each with the exact command. This page is the reference, not the start.

environment the engine provides to bodies and hooks (agents: read this, it is the API):

  always
    MEDULLA_RUN_ID          run id (settable from outside for correlation)
    MEDULLA_RUN_DIR         this run's directory; put deliverables in $MEDULLA_RUN_DIR/artifacts/
    MEDULLA_WORKFLOW_DIR    where this workflow's own files live (prompts/, scripts/) —
                            the copy that actually resolved, at its path in THIS process
    <all workflow vars>     exported as-is, including <signal:var>-set ones
    MEDULLA_TIMEOUT_S       resolved (deadline-clamped) timeout of the current step, seconds
    MEDULLA_ATTEMPT_ID      unique attempt id: <step>.<p|f><n>  (e.g. 003.i2.p1)
    MEDULLA_HARNESS         "shell" or the harness name of the current body

  after the first transition
    MEDULLA_LAST_NODE / _SIGNAL / _MESSAGE / _RC
                            outcome of the previously completed node (pool: _RC is empty)
    MEDULLA_LAST_EVENT_JSON same as one JSON object

  after a pool node completes
    MEDULLA_MANIFEST_<NODE> path to its manifest.jsonl (dashes->underscores, uppercased);
                            rows: {index,key,input,ok,reason,signal,message,rc,timed_out,
                                   attempts,fallback,harness,model,vars,updates,signals,
                                   duration_s,log}

  inside a pool input
    MEDULLA_INPUT           the input (objects as compact JSON)
    MEDULLA_INPUT_INDEX     1-based position     MEDULLA_INPUT_COUNT  total
    MEDULLA_INPUT_KEY       stable identity <index>:<sha256[:16]> (idempotency key)
    MEDULLA_INPUT_<KEY>     each flat scalar field of an object input, uppercased

  post hook only
    MEDULLA_BODY_RC / MEDULLA_BODY_SIGNAL
                            the body attempt's exit code and its raw signal (if any)

agent nodes (fields beyond harness/model/effort/sandbox/args):
    sets: [K, ...]          vars this agent may set from stdout; empty (default) = none
    session: <name>         name a conversation: the first node with the name opens it,
                            every later one continues it. Recorded in <run>/sessions.json;
                            templated, so a pool uses "panel-{{input.slug}}"

docker (host-side, handled by scripts/docker.py before the engine starts):
    medulla --docker -w <dir> ...   run inside the workflow's image
    --build                         force a no-cache image rebuild
    --mount <dir> / --mount-rw <dir>  extra mounts under /workspace/<name>
    image resolution precedence:    MEDULLA_IMAGE env > --var IMAGE >
                                    vars.IMAGE > build from (--var DOCKERFILE >
                                    vars.DOCKERFILE > packaged default)

subcommands: init <name> [--skill] (deploy a bundled template or scaffold a new workflow; --skill registers it with Claude Code), upgrade

environment the engine reads:
    MEDULLA_RETRY_DELAY_S   pause between attempts / before fallback (default 2)
    MEDULLA_RUN_ID          pre-seed the run id
    MEDULLA_DOCKER=1        set by scripts/docker.py: container is the sandbox

signals (print on stdout, must start the line for plain-text harnesses):
    <signal:NAME>message</signal:NAME>      route the graph (decision) / record (pool)
    <signal:var key=K>value</signal:var>    set a workflow var (fold law applies)
                                           from an AGENT body: ignored and logged unless
                                           the node declares agent.sets: [K, ...]
    <signal:update>progress</signal:update> progress line, never routes
"""
