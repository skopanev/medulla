"""Why did this panelist produce nothing — in one line a human can act on.

A harness can exit non-zero with an EMPTY stderr, because it reports failures as
JSON events on stdout. Measured live: opencode printed

    {"type":"error", ..., "error":{"name":"APIError","data":{
        "message":"Weekly/Monthly Limit Exhausted. Your limit will reset at ...",
        "statusCode":429, "responseHeaders":{...}, ...}}}

and the manifest row said `stderr:` followed by nothing. Three panels were escalated
as unexplained because the reason sat in a file nobody knew to open.

SAFE means safe: the message and the status code, and nothing else. That JSON also
carries response headers with `set-cookie` and request ids — session material that
has no business in a manifest a whole fleet reads.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MAX = 200


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:MAX]


def provider_error(log: Path) -> str:
    """The last error the harness reported, or "" if it reported none."""
    try:
        raw = log.read_text(errors="replace")
    except OSError:
        return ""
    found = ""
    for line in raw.splitlines():
        start = line.find("{")
        if start < 0 or '"error"' not in line:
            continue
        try:
            event = json.loads(line[start:])
        except ValueError:
            continue
        err = event.get("error")
        if not isinstance(err, dict):
            continue
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        # ONLY these two fields. Never `data` wholesale: it carries response headers,
        # cookies and request ids.
        message = data.get("message") or err.get("message") or err.get("name") or ""
        code = data.get("statusCode") or data.get("code") or ""
        if not message:
            continue
        found = _clean(f"provider said: {message}" + (f" (HTTP {code})" if code else ""))
    return found


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1]:
        raise SystemExit(0)
    out = provider_error(Path(sys.argv[1]))
    if out:
        print(out)
    raise SystemExit(0)
