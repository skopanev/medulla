#!/usr/bin/env bash
set -euo pipefail

# medulla installer
# Usage:
#   curl -sSL https://raw.githubusercontent.com/skopanev/medulla/main/install.sh | bash
#   bash install.sh
#   MEDULLA_REPO=/path/to/local/checkout bash install.sh   # dev: editable install

# one home: ~/.medulla/ — engine/ is machinery (venv + commit stamp, safe to
# delete anytime), .env is the user's global token tier. rm -rf ~/.medulla
# removes medulla entirely; that's the contract.
INSTALL_DIR="$HOME/.medulla/engine"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LEGACY_DIR="$HOME/.medulla-engine"
REPO_URL="${MEDULLA_REPO_URL:-https://github.com/skopanev/medulla.git}"

info()  { printf '\033[1;34m=>\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$c" &>/dev/null || continue
        "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
            && { echo "$c"; return 0; }
    done
    return 1
}

PYTHON=$(find_python) || error "Python 3.10+ not found. Install it first."
info "Using $("$PYTHON" --version) ($PYTHON)"

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
[ -d "$VENV_DIR" ] || { info "Creating venv at $VENV_DIR"; "$PYTHON" -m venv "$VENV_DIR"; }

if [ -n "${MEDULLA_REPO:-}" ]; then
    info "Installing EDITABLE from $MEDULLA_REPO (dev mode: edits apply instantly)"
    "$VENV_DIR/bin/pip" install -q --upgrade -e "$MEDULLA_REPO"
    COMMIT=$(git -C "$MEDULLA_REPO" rev-parse --short HEAD 2>/dev/null || echo "local")
    SUBJECT=$(git -C "$MEDULLA_REPO" log -1 --format=%s 2>/dev/null || echo "")
else
    # clone + force-reinstall: pip silently skips git+URL when the package
    # version hasn't bumped ("already satisfied") — commits never arrived
    SRC=$(mktemp -d)
    info "Fetching $REPO_URL"
    git clone -q --depth 1 "$REPO_URL" "$SRC/medulla"
    COMMIT=$(git -C "$SRC/medulla" rev-parse --short HEAD)
    SUBJECT=$(git -C "$SRC/medulla" log -1 --format=%s)
    info "Installing commit $COMMIT: $SUBJECT"
    "$VENV_DIR/bin/pip" install -q --force-reinstall --no-deps "$SRC/medulla"
    "$VENV_DIR/bin/pip" install -q pyyaml           # deps once (skipped by --no-deps)
    rm -rf "$SRC"
fi
echo "$COMMIT $SUBJECT" > "$INSTALL_DIR/INSTALLED_COMMIT"

ln -sf "$VENV_DIR/bin/medulla" "$BIN_DIR/medulla"
info "Linked $BIN_DIR/medulla"

# migrate: the engine used to live in ~/.medulla-engine — pure machinery,
# nothing of the user's; remove after the new install has fully succeeded
[ -d "$LEGACY_DIR" ] && { rm -rf "$LEGACY_DIR"; info "Removed legacy $LEGACY_DIR (engine now lives in ~/.medulla/engine)"; }

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf '\033[1;33m!!\033[0m %s\n' "$BIN_DIR is not in PATH — add: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

info "Done: medulla @ $COMMIT ($SUBJECT)"
info "Next:"
echo "     cd your-project"
echo "     medulla init <name>        # scaffold a pipeline (or: medulla init spar)"
echo "     medulla -w .medulla/pipelines/<name>"
