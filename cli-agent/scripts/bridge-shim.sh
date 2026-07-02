#!/usr/bin/env bash
# bridge-shim.sh — generic bridge shim for host-delegated tools
# Symlink as /usr/local/bin/<tool> → bridge-shim; detects tool name from $0.

BRIDGE="/tmp/medulla-bridge"
TOOL="$(basename "$0")"

# version check always works (for env banner / preflight)
case "${1:-}" in
  --version|-version)
    echo "$TOOL-shim (host-bridge)"
    exit 0 ;;
esac

if [ ! -d "$BRIDGE" ]; then
  echo "error: $TOOL requires host-builder bridge (not available in container)" >&2
  exit 127
fi

# Unique request ID to avoid race conditions
REQ_ID="$$.$RANDOM"
RESP_FILE="$BRIDGE/response.$REQ_ID"
EXIT_FILE="$BRIDGE/exit_code.$REQ_ID"

# Pass cwd so host-builder runs from the right directory
echo "id:$REQ_ID cwd:$(pwd) $TOOL $*" > "$BRIDGE/request"
while [ ! -f "$EXIT_FILE" ]; do
  sleep 0.2
done
cat "$RESP_FILE" 2>/dev/null
EXIT=$(cat "$EXIT_FILE" 2>/dev/null)
rm -f "$RESP_FILE" "$EXIT_FILE"
exit "${EXIT:-1}"
