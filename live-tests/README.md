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
