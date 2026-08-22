#!/bin/bash
# Selective copy of credentials from read-only mounts into container home.

if [ -d /mnt/claude ]; then
    mkdir -p "$HOME/.claude"
    for f in .credentials.json settings.json settings.local.json CLAUDE.md; do
        [ -e "/mnt/claude/$f" ] && cp -L "/mnt/claude/$f" "$HOME/.claude/$f" 2>/dev/null || true
    done
    for d in commands skills hooks agents plugins; do
        [ -d "/mnt/claude/$d" ] && cp -rL "/mnt/claude/$d" "$HOME/.claude/" 2>/dev/null || true
    done
fi

# Export env vars from Claude settings (e.g. ZHIPU_API_KEY)
if [ -f "$HOME/.claude/settings.json" ] && command -v jq >/dev/null 2>&1; then
    while IFS='=' read -r key val; do
        [ -n "$key" ] && export "$key=$val"
    done < <(jq -r '.env // {} | to_entries[] | "\(.key)=\(.value)"' "$HOME/.claude/settings.json" 2>/dev/null)
fi

# Codex credentials (skip personal config — workflow controls model/prompt via CLI)
#
# Token-broker guard: when this container has a broker config
# ($HOME/.config/hltm-broker/config.json), codex auth comes from the broker —
# either because `codex` here is a shim into the broker wrapper, or because the
# workflow calls the wrapper directly. Copying the host's /mnt/codex/auth.json
# into a writable $HOME/.codex/auth.json would let a plain codex refresh THAT
# token, and OpenAI would invalidate the host's live session (token poisoning).
# So: broker configured -> do NOT copy. No broker -> copy as before so bare
# codex works in the default image.
if [ -d /mnt/codex ]; then
    if [ -f "$HOME/.config/hltm-broker/config.json" ]; then
        : # broker configured: its wrapper owns auth, never copy the host token
    else
        mkdir -p "$HOME/.codex"
        [ -f /mnt/codex/auth.json ] && cp -L /mnt/codex/auth.json "$HOME/.codex/auth.json" 2>/dev/null || true
    fi
fi

# OpenCode auth
if [ -f /mnt/opencode-auth.json ]; then
    mkdir -p "$HOME/.local/share/opencode"
    cp /mnt/opencode-auth.json "$HOME/.local/share/opencode/auth.json"
    chmod 600 "$HOME/.local/share/opencode/auth.json"
fi

# Gemini CLI credentials
if [ -d /mnt/gemini ]; then
    mkdir -p "$HOME/.gemini"
    for f in oauth_creds.json settings.json google_accounts.json installation_id state.json trusted_hooks.json; do
        [ -e "/mnt/gemini/$f" ] && cp -L "/mnt/gemini/$f" "$HOME/.gemini/$f" 2>/dev/null || true
    done
    [ -e "$HOME/.gemini/oauth_creds.json" ] && chmod 600 "$HOME/.gemini/oauth_creds.json"
    # agy (antigravity CLI) config
    if [ -d /mnt/gemini/antigravity-cli ]; then
        cp -rL /mnt/gemini/antigravity-cli "$HOME/.gemini/antigravity-cli" 2>/dev/null || true
    fi
fi

# gemini CLI needs GEMINI_API_KEY; fall back to GOOGLE_API_KEY if not set
if [ -z "${GEMINI_API_KEY:-}" ] && [ -n "${GOOGLE_API_KEY:-}" ]; then
    export GEMINI_API_KEY="$GOOGLE_API_KEY"
fi

# host-builder bridge check
BRIDGE="${MEDULLA_BRIDGE:-/tmp/medulla-bridge}"
if [ -d "$BRIDGE" ]; then
    PING_ID="ping.$$"
    echo "id:$PING_ID ping" > "$BRIDGE/request"
    sleep 1
    if [ -f "$BRIDGE/exit_code.$PING_ID" ]; then
        rm -f "$BRIDGE/response.$PING_ID" "$BRIDGE/exit_code.$PING_ID"
        echo "✓ host-builder connected" >&2
    else
        echo "⚠ host-builder not responding (bridge mounted but no listener)" >&2
    fi
else
    echo "– host-builder not mounted (cargo runs locally)" >&2
fi

# Workflow git identity (env vars override mounted read-only .gitconfig)
export GIT_AUTHOR_NAME="MEDULLA Loop"
export GIT_AUTHOR_EMAIL="loop@medulla-agent.one"
export GIT_COMMITTER_NAME="MEDULLA Loop"
export GIT_COMMITTER_EMAIL="loop@medulla-agent.one"

# agy (Antigravity CLI) auth — populate Secret Service keyring from mounted keys
# Remove /.dockerenv so agy uses KeyringTokenStorage instead of file-based fallback
sudo rm -f /.dockerenv 2>/dev/null || true

if command -v dbus-daemon >/dev/null 2>&1 && command -v gnome-keyring-daemon >/dev/null 2>&1; then
    _dbus_addr=$(dbus-daemon --session --print-address --fork 2>/dev/null)
    if [ -n "$_dbus_addr" ]; then
        export DBUS_SESSION_BUS_ADDRESS="$_dbus_addr"
        echo "" | gnome-keyring-daemon --unlock --daemonize >/dev/null 2>&1 || true
        if command -v secret-tool >/dev/null 2>&1; then
            # OAuth token: macOS stores "go-keyring-base64:<b64json>"; Linux stores raw JSON
            if [ -f /mnt/agy-token ]; then
                raw="$(cat /mnt/agy-token)"
                b64="${raw#go-keyring-base64:}"
                decoded="$(printf '%s' "$b64" | base64 -d 2>/dev/null)"
                if [ -n "$decoded" ]; then
                    printf '%s' "$decoded" | secret-tool store --label="agy-token" service gemini username antigravity 2>/dev/null || true
                fi
            fi
            # Safe Storage key: AES key for decrypting ~/.gemini/antigravity-cli/implicit/*.pb files
            if [ -f /mnt/agy-safe-key ]; then
                cat /mnt/agy-safe-key | secret-tool store --label="Antigravity Safe Storage" service "Antigravity Safe Storage" username "Antigravity Key" 2>/dev/null || true
            fi
        fi
    fi
    unset _dbus_addr
fi

# agy migration marker dir (prevents migration errors)
mkdir -p "$HOME/.gemini/config"

# Add workspace scripts to PATH (export + bashrc so child shells inherit)
export PATH="/workspace/.medulla/scripts:$PATH"
grep -q '/workspace/.medulla/scripts' ~/.bashrc 2>/dev/null || echo 'export PATH="/workspace/.medulla/scripts:$PATH"' >> ~/.bashrc

# Self-upgrade on EVERY start, deliberately. It costs a few seconds and pays them
# every time (the container is --rm, so what it installs dies with it) — bought on
# purpose, because the image cannot keep itself current: its tag is a sha of the
# DOCKERFILE while medulla is pulled from git at build time, so an untouched
# Dockerfile means a tag Docker considers fresh forever while the engine inside
# drifts. It reached TWO versions behind the host that way and no test caught it.
# This script is mounted from the host's medulla package, so `medulla upgrade` on
# the host is enough to change it — the image needs no rebuild.
#
# `medulla upgrade` over `pipx upgrade medulla`: it picks whichever method the
# install actually used, so this script need not know. Upgrade, never reinstall —
# reinstall removes-then-fetches, and a network hiccup would leave the container
# with NO medulla at all (exit 127).
# MEDULLA_UPGRADE_ON_START=0 opts out: offline boxes, or an image pinned on purpose.
if [ "${MEDULLA_UPGRADE_ON_START:-1}" != "0" ]; then
    medulla upgrade >&2 || echo "⚠ medulla upgrade skipped (offline?) — using baked version" >&2
fi

exec "$@"
