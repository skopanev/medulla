"""Verdicts a model actually wrote, that the parser used to throw away.

Every case here comes from the panel archive, not from imagination. A full-store
sweep over 2219 delivered artifacts found 50 of them failed for the fence alone —
each one a live artifact with findings, recorded as "no verdict" and dropped from
the delivered count a gate reads.
"""
from test_spar_parsing import collect

FINDINGS = "## FINDINGS\n- (R) HIGH — a claim — f.py:1 — why — FIX: how\n\n"


def test_a_fenced_verdict_is_still_a_verdict(tmp_path):
    """The prompt used to show the example INSIDE triple backticks, so a panelist
    copied the fence too — and repeated the heading inside it. The first non-empty
    line under the heading was ``` and the whole artifact was failed."""
    body = FINDINGS + "## VERDICT\n\n```\n## VERDICT\nGO — 3, 5 | it ships\n```\n"
    _, data, _ = collect(tmp_path, [("glm5", body)], min_decided=1)
    assert data["panelists"][0]["verdict"] == "GO"
    assert data["quorum"]["decided"] == 1


def test_emphasis_is_decoration_not_a_different_word(tmp_path):
    body = FINDINGS + "## VERDICT\n**NO-GO**. the cache leaks across tenants\n"
    _, data, _ = collect(tmp_path, [("gemini", body)], min_decided=1)
    assert data["panelists"][0]["verdict"] == "NO-GO"


def test_a_reasoning_section_does_not_outrank_the_verdict(tmp_path):
    """`## Verdict reasoning` sits ABOVE `## VERDICT` in real artifacts. A prefix
    match handed back its first prose line and the real verdict below was never read."""
    body = (FINDINGS + "## Verdict reasoning\nAll three findings are inert today.\n\n"
            "## VERDICT\nGO — nothing here blocks the change\n")
    _, data, _ = collect(tmp_path, [("sonnet", body)], min_decided=1)
    assert data["panelists"][0]["verdict"] == "GO"
    assert "inert today" not in data["panelists"][0]["reason"]


def test_prefix_headings_still_work_when_there_is_no_exact_one(tmp_path):
    """Exact-wins must not cost the leniency that was added for `## FINDINGS (12)`."""
    body = ("## FINDINGS (2 of them)\n- (R) MED — a claim — f.py:2 — why — FIX: how\n\n"
            "## VERDICT — round 2\nNO-GO — 1 — it regresses the loader\n")
    _, data, _ = collect(tmp_path, [("gpt5", body)], min_decided=1)
    assert data["panelists"][0]["verdict"] == "NO-GO"
    assert len(data["findings"]) == 1


def test_a_fenced_verdict_counts_toward_quorum(tmp_path):
    """The defect that reached the field: four artifacts on disk, delivered read 3."""
    plain = FINDINGS + "## VERDICT\nGO — it ships\n"
    fenced = FINDINGS + "## VERDICT\n\n```\n## VERDICT\nNO-GO — 1 — it does not\n```\n"
    _, data, _ = collect(tmp_path, [("a", plain), ("b", plain), ("c", plain),
                                    ("d", fenced)])
    assert data["quorum"]["decided"] == 4
    assert data["counts"]["NO-GO"] == 1


# ── the 4-of-5 shape reported from the field ─────────────────────────────────

def test_a_finding_without_a_bullet_is_still_a_finding(tmp_path):
    """Gemini wrote its finding starting straight at `(R) HIGH — ...`. Requiring a
    bullet dropped it, which then made its own `NO-GO — 1` cite a finding the parser
    did not have — turning a supported objection into an unsupported one."""
    body = ("### FINDINGS\n(R) HIGH — inverted policy text passes — a.test.ts:250 — "
            "why — FIX: assert equality\n\n### VERDICT\nNO-GO — 1\n")
    _, data, _ = collect(tmp_path, [("gemini", body)], min_decided=1)
    p = data["panelists"][0]
    assert p["findings"] == 1
    assert p["cites"] == ["F1"], "the citation must resolve"
    assert data["unsupported_no_go"] == []


def test_delivered_and_decided_come_from_the_same_artifacts(tmp_path):
    """The field defect: artifacts=4, quorum.delivered=3, decided=4. `delivered` was
    the engine's manifest (a post hook had vetoed a complete file); `decided` was the
    files on disk. One source now — the manifest number is kept only as a witness."""
    ok = "## FINDINGS\n- (R) LOW — a — f.py:1 — w — FIX: h\n\n## VERDICT\nGO — ships\n"
    _, data, _ = collect(tmp_path, [("a", ok), ("b", ok), ("c", ok), ("d", ok)],
                         expected=5, delivered=3)
    assert data["quorum"]["delivered"] == 4
    assert data["quorum"]["manifest_delivered"] == 3


def test_an_unreadable_objection_does_not_vote_but_still_blocks(tmp_path):
    """The contract: out of the arithmetic, into blocking — and blocking alone holds
    the round. Counting it as a NO-GO overstated the vote; dropping it outright would
    have gone CLEAR with a non-empty blocking list, which is fail-open."""
    go = "## FINDINGS\n- (R) LOW — a — f.py:1 — w — FIX: h\n\n## VERDICT\nGO — ships\n"
    mute = ("## FINDINGS\n- (R) LOW — b — f.py:2 — w — FIX: h\n\n"
            "## VERDICT\nNO-GO — it breaks three callers\n")   # prose, not a citation
    _, data, _ = collect(tmp_path, [("a", go), ("b", go), ("c", go), ("d", mute)])
    assert data["counts"]["NO-GO"] == 0, "an unreadable objection is not a vote"
    assert data["counts"]["unsupported"] == 1
    assert data["quorum"]["decided"] == 3
    assert "UNREAD-d" in data["blocking"]
    assert data["state"] == "REVIEW_REQUIRED", "blocking alone must hold the round"


def test_prose_carrying_a_confidence_mark_is_not_a_finding(tmp_path):
    """`(R) Reproduced directly:` is a sentence, not a finding. Accepting every
    bulletless line that starts with (R) invented one across the archive — the
    severity is what makes it a finding, so the severity is what is required."""
    body = ("## FINDINGS\n(R) Reproduced directly:\n"
            "(R) HIGH — a real one — f.py:1 — why — FIX: how\n\n"
            "## VERDICT\nNO-GO — 1\n")
    _, data, _ = collect(tmp_path, [("sonnet", body)], min_decided=1)
    assert data["panelists"][0]["findings"] == 1
    assert "a real one" in data["findings"][0]["text"]
