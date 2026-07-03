#!/usr/bin/env bash
# host-builder.sh — runs on Mac, executes build commands for Docker agent
# Usage: ./host-builder.sh /path/to/project
set -uo pipefail

BRIDGE="${MEDULLA_BRIDGE:-${TMPDIR:-/tmp}/medulla-bridge}"
WORKSPACE="${1:-.}"

C_RESET='\033[0m' C_DIM='\033[2m' C_GREEN='\033[32m' C_RED='\033[31m' C_CYAN='\033[36m' C_YELLOW='\033[33m'

# Android/Java environment for gradle builds
export JAVA_HOME="$(/usr/libexec/java_home -v 21 2>/dev/null || echo "${JAVA_HOME:-}")"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"

mkdir -p "$BRIDGE"

# Kill previous host-builder if running
LOCKFILE="$BRIDGE/host-builder.pid"
if [ -f "$LOCKFILE" ]; then
  OLD_PID=$(cat "$LOCKFILE" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null
    printf "${C_YELLOW}killed previous host-builder pid=%s${C_RESET}\n" "$OLD_PID"
    sleep 0.5
  fi
fi
echo $$ > "$LOCKFILE"

# Clean bridge state
rm -f "$BRIDGE/request" "$BRIDGE/response" "$BRIDGE/exit_code"
# Clean ID-based response/exit_code files
find "$BRIDGE" -name "response.*" -o -name "exit_code.*" 2>/dev/null | xargs rm -f 2>/dev/null

printf "${C_CYAN}host-builder${C_RESET} listening\n"
printf "${C_DIM}workspace${C_RESET}  %s\n" "$(cd "$WORKSPACE" && pwd)"
printf "${C_DIM}bridge${C_RESET}     %s\n\n" "$BRIDGE"

BG_PIDS=()

cleanup() {
  for pid in "${BG_PIDS[@]}"; do
    kill -9 "$pid" 2>/dev/null && printf "$(date +%H:%M:%S) ${C_YELLOW}⊘${C_RESET} cleanup killed pid=%s\n" "$pid"
  done
  rm -rf "$BRIDGE"
  printf "\n${C_YELLOW}stopped${C_RESET}\n"
  exit 0
}
trap cleanup INT TERM HUP

run_cmd() {
  local full="$1"
  local run_dir="$WORKSPACE"

  # cwd: prefix — run from specified directory
  if [[ "$full" == cwd:* ]]; then
    local cwd_and_rest="${full#cwd:}"
    run_dir="${cwd_and_rest%% *}"
    full="${cwd_and_rest#* }"
    # Mounts are siblings of workspace — always resolve as ../name
    local sibling="$WORKSPACE/../$(basename "$run_dir")"
    if [ -d "$sibling" ]; then
      run_dir="$(cd "$sibling" && pwd)"
    fi
  fi

  local cmd="${full%% *}"
  local args="${full#* }"
  [ "$args" = "$full" ] && args=""
  # Guard against empty commands
  if [ -z "$cmd" ]; then
    echo "error: empty command" >&2
    return 1
  fi
  # If command exists as ./cmd in run_dir, use local version
  if [ -x "$run_dir/$cmd" ] && [ ! -d "$run_dir/$cmd" ]; then
    cmd="./$cmd"
  fi

  case "$cmd" in
    ping)
      echo "pong" ;;
    *)
      (cd "$run_dir" && eval "$cmd $args") 2>&1 ;;
  esac
}

while true; do
  if [ -f "$BRIDGE/request" ]; then
    RAW=$(cat "$BRIDGE/request")
    rm -f "$BRIDGE/request"

    # Extract request ID if present (format: "id:<ID> <CMD>")
    REQ_ID=""
    CMD="$RAW"
    if [[ "$RAW" == id:* ]]; then
      REQ_ID="${RAW%% *}"
      REQ_ID="${REQ_ID#id:}"
      CMD="${RAW#* }"
    fi

    # rewrite Docker paths to relative
    CMD="${CMD///workspace\//}"
    CMD="${CMD///workspace/\.}"

    printf "$(date +%H:%M:%S) ${C_GREEN}→${C_RESET} %s\n" "$CMD"

    # Determine response/exit_code filenames (with or without ID)
    if [ -n "$REQ_ID" ]; then
      RESP_FILE="$BRIDGE/response.$REQ_ID"
      EXIT_FILE="$BRIDGE/exit_code.$REQ_ID"
    else
      RESP_FILE="$BRIDGE/response"
      EXIT_FILE="$BRIDGE/exit_code"
    fi

    # bg: prefix — run in background, return PID
    if [[ "$CMD" == bg:* ]]; then
      BG_CMD="${CMD#bg:}"
      cd "$WORKSPACE" && bash -c "$BG_CMD" > "$BRIDGE/bg.log" 2>&1 &
      BG_PID=$!
      BG_PIDS+=("$BG_PID")
      printf "$(date +%H:%M:%S) ${C_CYAN}⟳${C_RESET} background pid=%d\n" "$BG_PID"
      echo "$BG_PID" > "$RESP_FILE"
      echo "0" > "$EXIT_FILE"

    # kill: prefix — graceful shutdown, then force after 5s
    elif [[ "$CMD" == kill:* ]]; then
      KILL_PID="${CMD#kill:}"
      if kill "$KILL_PID" 2>/dev/null; then
        # Wait up to 5s for graceful shutdown
        for _i in 1 2 3 4 5; do
          kill -0 "$KILL_PID" 2>/dev/null || break
          sleep 1
        done
        # Force only if still alive
        if kill -0 "$KILL_PID" 2>/dev/null; then
          kill -9 "$KILL_PID" 2>/dev/null
          printf "$(date +%H:%M:%S) ${C_YELLOW}⊘${C_RESET} force-killed pid=%s\n" "$KILL_PID"
        else
          printf "$(date +%H:%M:%S) ${C_YELLOW}⊘${C_RESET} killed pid=%s\n" "$KILL_PID"
        fi
        echo "killed $KILL_PID" > "$RESP_FILE"
      else
        echo "process $KILL_PID not found" > "$RESP_FILE"
      fi
      echo "0" > "$EXIT_FILE"

    # normal command
    else
      OUTPUT=$(run_cmd "$CMD")
      EXIT=$?

      # replace host paths with Docker paths so agent sees /workspace
      ABS_WORKSPACE="$(cd "$WORKSPACE" && pwd)"
      echo "${OUTPUT//$ABS_WORKSPACE//workspace}" > "$RESP_FILE"
      echo "$EXIT" > "$EXIT_FILE"
    fi

    if [ -f "$EXIT_FILE" ]; then
      EXIT=$(cat "$EXIT_FILE")
      if [ "$EXIT" -eq 0 ]; then
        printf "$(date +%H:%M:%S) ${C_GREEN}✓${C_RESET} exit %d\n" "$EXIT"
      else
        printf "$(date +%H:%M:%S) ${C_RED}✗${C_RESET} exit %d\n" "$EXIT"
      fi
    fi
  fi
  sleep 0.2
done
