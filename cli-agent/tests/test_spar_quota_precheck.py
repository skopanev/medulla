"""The panel's pre hook: what happens when the check itself cannot run.

It used to fail OPEN. Live: the catalog fetch was cut off mid-download, jq reported a
parse error into a stream nobody reads, the empty result took a skip branch, and a
panelist whose quota was exhausted was admitted to the body — where opencode swallowed
the 429 and hung for 1819 seconds across two attempts. The silence watchdog was blamed
and retuned for it. Every test here is one of the steps that used to end in `exit 0`.
"""
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/quota_precheck.sh"

CATALOG = '{"alibaba-token-plan": {"api": "https://provider.example/v1"}}'

FAKE_CURL = r"""#!/usr/bin/env bash
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
url="${@: -1}"
case "$url" in
  *models.dev*)
    case "${FAKE_CATALOG:-ok}" in
      ok)        printf '%s' "$CATALOG_JSON" > "$out"; exit 0 ;;
      truncated) printf '{"alibaba-token-plan": {"api": "https://prov' > "$out"; exit 0 ;;
      html)      printf '<html>502 Bad Gateway</html>' > "$out"; exit 0 ;;
      missing)   printf '{"some-other-provider": {"api": "https://x"}}' > "$out"; exit 0 ;;
      unreachable) exit 22 ;;
    esac ;;
  *)
    printf '%s' "${PING_BODY:-}" > "$out"
    printf '%s' "${PING_CODE-200}"
    exit 0 ;;
esac
"""


def _run(tmp_path, *, catalog="ok", ping_code="200", ping_body="",
         harness="opencode", model="alibaba-token-plan/qwen3.8-max", auth=CATALOG):
    """Run the hook with curl faked and HOME/TMPDIR pointed at a scratch dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    if auth is not None:
        auth_file = tmp_path / ".local/share/opencode/auth.json"
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text('{"alibaba-token-plan": {"key": "sk-test", "type": "api"}}')
    env = {**os.environ,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "HOME": str(tmp_path),
           "TMPDIR": str(tmp_path),
           "CATALOG_JSON": CATALOG,
           "FAKE_CATALOG": catalog,
           "PING_CODE": ping_code,
           "PING_BODY": ping_body,
           "MEDULLA_INPUT_SLUG": "qwen",
           "MEDULLA_INPUT_HARNESS": harness,
           "MEDULLA_INPUT_MODEL": model}
    res = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True,
                         env=env, check=False)
    return res.returncode, res.stderr.strip()


def test_a_truncated_catalog_stops_the_panelist(tmp_path):
    """THE one. 4.4 MB behind a 15s ceiling: the body stopped at 2.6 MB, jq said
    'Unfinished string at EOF', and the panelist ran anyway."""
    rc, err = _run(tmp_path, catalog="truncated")
    assert rc == 1
    assert "unusable" in err and "not valid JSON" in err


def test_a_200_that_is_not_json_stops_the_panelist(tmp_path):
    """An error page arrives with a success code and a plausible size."""
    rc, err = _run(tmp_path, catalog="html")
    assert rc == 1 and "unusable" in err


def test_an_unreachable_catalog_stops_the_panelist(tmp_path):
    rc, err = _run(tmp_path, catalog="unreachable")
    assert rc == 1 and "could not fetch the model catalog" in err


def test_a_catalog_without_this_provider_stops_the_panelist(tmp_path):
    rc, err = _run(tmp_path, catalog="missing")
    assert rc == 1 and "no base url for alibaba-token-plan" in err


def test_an_exhausted_quota_stops_the_panelist_with_the_providers_own_words(tmp_path):
    """attempts=0 and the reason in the manifest — the case the hook exists for."""
    rc, err = _run(tmp_path, ping_code="429",
                   ping_body='{"error": {"message": "quota has been exhausted"}}')
    assert rc == 1
    assert "HTTP 429" in err and "quota has been exhausted" in err


def test_a_provider_that_answers_nothing_stops_the_panelist(tmp_path):
    rc, err = _run(tmp_path, ping_code="")
    assert rc == 1 and "did not answer" in err


def test_missing_credentials_stop_the_panelist(tmp_path):
    """opencode could not have authenticated either — better said here, at 8 seconds."""
    rc, err = _run(tmp_path, auth=None)
    assert rc == 1 and "could not authenticate" in err


def test_a_live_provider_admits_the_panelist(tmp_path):
    rc, err = _run(tmp_path)
    assert rc == 0, err


@pytest.mark.parametrize("harness,model", [("claude-code", "claude-sonnet-5"),
                                           ("opencode", "bare-model-name")])
def test_the_two_honest_passes(tmp_path, harness, model):
    """Not opencode, or a model name carrying no provider: there is nothing to ask.
    These are the only exits that may skip the check."""
    rc, err = _run(tmp_path, harness=harness, model=model, auth=None)
    assert rc == 0, err


def test_the_catalog_is_fetched_once_per_container(tmp_path):
    """Five panelists behind one 440 KB download, not five. The second run finds the
    cache the first one left; make the network fail to prove it is not touched."""
    rc, err = _run(tmp_path)
    assert rc == 0, err
    assert (tmp_path / "medulla-models-catalog.json").is_file()
    rc, err = _run(tmp_path, catalog="unreachable")
    assert rc == 0, err
