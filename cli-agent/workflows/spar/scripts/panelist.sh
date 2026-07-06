#!/usr/bin/env bash
# Panelist dispatcher for the spar workflow.
#
# Usage: panelist.sh "slug|executor|model|effort"   (model and effort may be empty)
#
# Reads $ROUND_DIR (set by the prepare stage via <signal:var>), assembles the
# panelist prompt from prompts/spar.md + question.md + the output-file
# contract, runs the executor CLI with the same flags medulla/executor.py
# uses, and emits <signal:done> only if the panelist actually wrote its
# output file. Raw CLI output goes to $ROUND_DIR/logs/<slug>.log so stray
# signal text inside model output never reaches the loop's signal parser.
set -u

ITEM="${1:?usage: panelist.sh 'slug|executor|model|effort'}"
IFS='|' read -r SLUG EXECUTOR MODEL EFFORT <<EOF
$ITEM
EOF

: "${ROUND_DIR:?ROUND_DIR not set — prepare stage must run first}"

WF_DIR=".medulla/workflows/spar"
[ -d "$WF_DIR" ] || WF_DIR="cli-agent/workflows/spar"
SPAR="$WF_DIR/prompts/spar.md"
if [ ! -f "$SPAR" ]; then
  echo "panelist[$SLUG]: preamble not found: $SPAR (cwd=$PWD)" >&2
  exit 1
fi
OUT_FILE="$ROUND_DIR/$SLUG.md"
LOG_DIR="$ROUND_DIR/logs"
mkdir -p "$LOG_DIR"
PROMPT_FILE="$LOG_DIR/$SLUG.prompt.md"
LOG_FILE="$LOG_DIR/$SLUG.log"

# Inner CLI timeout = pipeline step timeout + 5min slack, so medulla's own
# round timeout fires first and the per-tool default (agy/opencode = 5m) never
# kills a long run prematurely. medulla injects MEDULLA_TIMEOUT_S (the resolved
# `timeout:` from the pipeline); default 1h if run outside medulla.
STEP_TIMEOUT_S="${MEDULLA_TIMEOUT_S:-3600}"
INNER_S=$((STEP_TIMEOUT_S + 300))
INNER_MS=$((INNER_S * 1000))

{
  cat "$SPAR"
  echo
  cat "$ROUND_DIR/question.md"
  echo
  echo "Use your file-write tool to write your full response to this exact path:"
  echo
  echo '```'
  echo "$OUT_FILE"
  echo '```'
  echo
  echo "Do not paste the response in chat. After writing the file, print exactly"
  echo "one line and stop:"
  echo
  echo "SUMMARY: <your one-sentence summary>"
} > "$PROMPT_FILE"

rc=1
case "$EXECUTOR" in
  opencode)
    # Deliver permission=allow + provider timeout (+ per-model effort) via env,
    # NOT an opencode.json in the project tree: the on-disk file used to linger
    # after runs (gitignored but physically present) and got stale-reused. The
    # env layers on top of any real project config without touching it.
    if [ "${MODEL#*/}" != "$MODEL" ]; then
      # provider-level timeout=60m (default is 5m and kills long reasoning);
      # reasoningEffort per-model only when requested.
      if [ -n "$EFFORT" ]; then
        MOPT="\"options\":{\"reasoningEffort\":\"$EFFORT\"}"
      else
        MOPT="\"options\":{}"
      fi
      OPENCODE_CONFIG_CONTENT=$(printf '{"$schema":"https://opencode.ai/config.json","permission":"allow","provider":{"%s":{"options":{"timeout":%s},"models":{"%s":{%s}}}}}' \
        "${MODEL%%/*}" "$INNER_MS" "${MODEL#*/}" "$MOPT")
    else
      OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","permission":"allow"}'
    fi
    export OPENCODE_CONFIG_CONTENT
    opencode run --agent build ${MODEL:+-m "$MODEL"} "Execute." "@$PROMPT_FILE" >"$LOG_FILE" 2>&1
    rc=$?
    ;;
  codex)
    # Prefer the `cx` wrapper (refreshes codex token via broker) if present,
    # else fall back to plain `codex` so it still works without the wrapper.
    command -v cx >/dev/null 2>&1 && CODEX_BIN=cx || CODEX_BIN=codex
    "$CODEX_BIN" exec --json --skip-git-repo-check \
      ${MODEL:+-c model=\"$MODEL\"} \
      -c model_reasoning_effort="${EFFORT:-xhigh}" \
      -c stream_idle_timeout_ms="$INNER_MS" \
      --dangerously-bypass-approvals-and-sandbox \
      "Execute. @$PROMPT_FILE" >"$LOG_FILE" 2>&1
    rc=$?
    ;;
  claude-code)
    API_TIMEOUT_MS="$INNER_MS" \
    claude --dangerously-skip-permissions --output-format stream-json --verbose \
      ${MODEL:+--model "$MODEL"} \
      --append-system-prompt-file "$PROMPT_FILE" \
      -p "Execute." >"$LOG_FILE" 2>&1
    rc=$?
    ;;
  gemini)
    GEMINI_SYSTEM_MD="$PROMPT_FILE" gemini --approval-mode yolo \
      ${MODEL:+-m "$MODEL"} -p "Execute." >"$LOG_FILE" 2>&1
    rc=$?
    ;;
  agy)
    # --print-timeout default is 5m and silently kills long runs; tie to step timeout.
    agy --dangerously-skip-permissions ${MODEL:+--model "$MODEL"} \
      --print-timeout "${INNER_S}s" \
      --print "$(printf 'Execute.\n\n'; cat "$PROMPT_FILE")" >"$LOG_FILE" 2>&1
    rc=$?
    ;;
  *)
    echo "panelist[$SLUG]: unknown executor '$EXECUTOR'" >&2
    exit 2
    ;;
esac

if [ -s "$OUT_FILE" ]; then
  summary=$(grep -ao 'SUMMARY: [^"\\]*' "$LOG_FILE" | head -1)
  echo "<signal:done>$SLUG: ${summary:-wrote $OUT_FILE (rc=$rc)}</signal:done>"
  exit 0
fi
echo "panelist[$SLUG]: no output at $OUT_FILE (rc=$rc), log: $LOG_FILE" >&2
exit 1
