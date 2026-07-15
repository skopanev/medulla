# md-tests — live battle tests for medulla v2 (no docker)

Each pipeline exercises ONE engine feature against real CLIs. Cheap tasks,
cheap models. Run from this dir:  medulla -w .medulla/pipelines/<name>

| test | feature under fire | expects |
|---|---|---|
| t1-claude   | claude adapter, stream-json filter, post truth channel | exit 0 |
| t2-codex    | codex adapter: stdin prompt, JSONL filter, cx wrapper   | exit 0 |
| t3-opencode | opencode adapter, line-start signal heuristic           | exit 0 |
| t4-agy      | agy adapter + trust preflight (E_HARNESS if untrusted)  | 0 or E_HARNESS |
| t5-panel    | live pool: 2 harnesses, manifest, min_success, synth    | exit 0 |
| t6-fallback | broken primary model -> live fallback switch            | exit 0 |
| t7-resume   | deadline mid-pool -> resume from done-mask (shell)      | 1 then 0 |
| t8-foreach-dynamic | JSONL source, object inputs, min_success under real failure | exit 0 |
| t9-fold     | fold law: max_parallel 1 var accumulator across inputs  | exit 0 |
| t10-array-source | shell source returning a single JSON array           | exit 0 |
| t11-chain   | live data flow: signal body -> {{last.message}} -> live agent B | exit 0 |
| t12-preguard | pre-guard skips a live agent (cache pattern)           | exit 0 |
| t13-retry-post | post vetoes attempt 1 of a live agent -> live re-run  | exit 0 |
| t14-interrupt | (script) SIGINT mid-agent: 130, no orphans, resumable | PASS |
| t15-timeout-live | step timeout kills a live agent: rc 124, clean group | exit 0 |
| t16-concurrent | (script) two simultaneous runs of one pipeline        | PASS |
| t17-dirty-data | hostile bytes through the env channel                 | exit 0 |
| t18-empty-live | dynamic source returns nothing -> __empty__ route     | exit 0 |
| t19-stress-pool | 20 inputs x 8 workers: manifest under concurrency    | exit 0 |
| t20-hetero  | per-input missions (inert {{input.ask}} fragments)      | exit 0 |

Harness-holding lore (test-harness scars, not engine bugs): the nested
Claude harness DETACHES long commands (hold an agent with many short
sequential tool calls, never one long sleep); $! after a subshell is the
wrapper's pid (use exec); backgrounded jobs of non-interactive shells
inherit SIGINT=SIG_IGN (signal from python, not from bash).
