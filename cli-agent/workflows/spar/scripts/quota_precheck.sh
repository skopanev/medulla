#!/usr/bin/env bash
# Does this panelist's provider still answer? Asked BEFORE the body, because opencode
# swallows an HTTP 429 and then hangs until something kills it — half an hour of a
# panel's wall-clock spent on a model that was refused in the first second.
#
# FAIL-CLOSED. Every failure here stops the panelist with a reason in the manifest.
# It used to fail OPEN: any step it could not complete ended in `exit 0` and the
# panelist ran anyway. Live, that cost an hour and then misled the fix — jq choked on a
# catalog fetch that had been cut off mid-download, the empty result took the skip
# branch, a panelist with an exhausted quota was admitted to the body, hung, and the
# silence watchdog was blamed and retuned for it. A check that cannot run is not a pass.
#
# rc=1 in pre is a SOFT fail (signal __failed__, attempts=0): this panelist sits the
# round out and the others still deliver — that is what min_success is for. The risk it
# buys is real and deliberate: if the catalog is unreachable, every opencode panelist
# sits out. That is the intended trade — a round that is short two panelists says so,
# while a round that admitted them blind reads like a full panel and is not one.
set -uo pipefail

slug="${MEDULLA_INPUT_SLUG:-panelist}"

out()  { echo "$slug sits this round out: $*" >&2; exit 1; }

# MEDULLA_INPUT_HARNESS first: in a pool MEDULLA_HARNESS still holds the unrendered
# "{{input.harness}}" (the engine builds the tag before rendering).
harness="${MEDULLA_INPUT_HARNESS:-${MEDULLA_HARNESS:-}}"
# Not opencode, or a model name that carries no provider: there is nothing to ask.
# These two are the only honest passes in this script.
[ "$harness" = "opencode" ] || exit 0
model="${MEDULLA_INPUT_MODEL:-}"
case "$model" in */*) ;; *) exit 0 ;; esac
provider="${model%%/*}"; model_id="${model#*/}"

command -v jq >/dev/null 2>&1 || out "jq is missing — the quota check cannot run"

auth="$HOME/.local/share/opencode/auth.json"
[ -r "$auth" ] || out "no readable $auth — opencode could not authenticate either"
key=$(jq -r --arg p "$provider" '.[$p].key // empty' "$auth" 2>/dev/null) \
  || out "$auth is not valid JSON"
[ -n "$key" ] || out "no credential for $provider in $auth"

# ── the base url ────────────────────────────────────────────────────────────────
# It lives in one field of a catalog that is 4.4 MB uncompressed. Fetching that per
# panelist is what broke: a 15s ceiling cut the body off at 2.6 MB and jq reported a
# parse error nobody read. So: prefer a local copy, ask for gzip (440 KB), validate the
# transport AND the JSON before reading a field out of it, and keep what arrived so the
# next panelist in this container does not fetch it again.
CATALOG_CACHE="${TMPDIR:-/tmp}/medulla-models-catalog.json"
CATALOG_MAX_AGE_S=21600          # 6h: base urls are not weather

catalog=""
for candidate in "$HOME/.cache/opencode/models.json" "$CATALOG_CACHE"; do
    [ -s "$candidate" ] || continue
    age=$(( $(date +%s) - $(stat -f %m "$candidate" 2>/dev/null \
                            || stat -c %Y "$candidate" 2>/dev/null || echo 0) ))
    [ "$age" -le "$CATALOG_MAX_AGE_S" ] || continue
    jq -e . "$candidate" >/dev/null 2>&1 || continue    # a half-written cache is not a cache
    catalog="$candidate"; break
done

if [ -z "$catalog" ]; then
    tmp=$(mktemp) || out "cannot write a temp file for the model catalog"
    # --fail: an HTML error page is not a catalog. --retry: one flaky fetch is not an
    # outage. -m 25: the hook's whole budget is 60s and the quota ping still needs its
    # share. Transport first, then shape — a 200 that arrived truncated is still garbage.
    if ! curl -sS --compressed --fail --retry 1 --retry-max-time 25 -m 25 \
              -o "$tmp" https://models.dev/api.json 2>/dev/null; then
        rm -f "$tmp"; out "could not fetch the model catalog (network or models.dev)"
    fi
    if ! jq -e . "$tmp" >/dev/null 2>&1; then
        size=$(wc -c <"$tmp" 2>/dev/null | tr -d ' ')
        rm -f "$tmp"
        out "the model catalog arrived unusable (${size:-0} bytes, not valid JSON)"
    fi
    cp "$tmp" "$CATALOG_CACHE" 2>/dev/null || true
    catalog="$tmp"
fi

api=$(jq -r --arg p "$provider" '.[$p].api // empty' "$catalog" 2>/dev/null)
[ -n "$api" ] || out "no base url for $provider in the model catalog"

# ── does it answer? ─────────────────────────────────────────────────────────────
body=$(jq -nc --arg m "$model_id" \
       '{model:$m,messages:[{role:"user",content:"ping"}],max_tokens:1}')
ans=$(mktemp) || out "cannot write a temp file for the quota probe"
code=$(curl -sS -m 20 -o "$ans" -w '%{http_code}' \
       -H 'Content-Type: application/json' -H "Authorization: Bearer $key" \
       -d "$body" "${api%/}/chat/completions" 2>/dev/null)
if [ "$code" = 200 ]; then rm -f "$ans"; exit 0; fi
why=$(jq -r '.error.message // empty' "$ans" 2>/dev/null); rm -f "$ans"
[ -n "$code" ] || out "$provider did not answer the quota probe at all"
out "$provider answered HTTP $code${why:+ - $why}"
