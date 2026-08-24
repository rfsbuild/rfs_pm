"""The source registry is a CONTROL, so it gets tested like one.

Written 2026-08-24, after `#financial` (C0BL875L30U) was found missing from
`sweep_sources.json`. That channel was created 2026-07-27 — **two days before
the registry was written on 2026-07-29** — so this was never staleness. It was
an omission, and it survived 28 days because nothing in the suite could see it.

What it cost: `#financial` is where Rafael's payment authorisations land, where
Alice posts payment requests, and where payroll-rate questions get asked. On
2026-08-24 alone it held a live "pay 1.5x for Saturday hours" proposal the day
before payroll, a $7,486.50 countertop awaiting authorisation, a $250 off-hour
inspection, and an invoice Hadassa did not recognise. None of it had reached the
board, the forecast, or a session note.

The registry's own `_why` field already records this exact failure once
(#99-concord, 2026-07-29, six missed task assignments). Twice is a pattern, and
a pattern gets a gate rather than another note.

The sweep contract's step 1 ("LIST channels live, diff against the registry")
is a self-heal, but it depends on the model performing it on every run. The
registry is the part that is structural. This test guards the structural part.
"""
import json
import os

import pytest

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "sweep_sources.json")


@pytest.fixture(scope="module")
def reg():
    with open(REGISTRY) as f:
        return json.load(f)


def _slack_sources(reg):
    return [c for group in reg["slack"].values() for c in group]


# The channels that carry money. Losing any of these is the defect this file
# exists for, so each is pinned by ID — a rename must not silently drop it.
MUST_CARRY = {
    "C0BL875L30U": "#financial",      # payment authorisations + payroll questions
    "C0BL01ACTNX": "#rba",            # Andersen extra-labor approvals
    "C0BK8J7BRJ9": "#all-rfsbuilders",  # Rafael's broadcast asks
}


@pytest.mark.parametrize("cid,name", sorted(MUST_CARRY.items()))
def test_money_channels_are_registered(reg, cid, name):
    """FAILS on the pre-2026-08-24 registry, which had no #financial."""
    ids = {c["id"] for c in _slack_sources(reg)}
    assert cid in ids, (
        "%s (%s) is missing from sweep_sources.json — the sweep will not read "
        "it. This is the #financial defect of 2026-08-24 recurring." % (name, cid)
    )


def test_registered_name_matches_the_pinned_id(reg):
    """A row keeping the id but drifting to another channel's name is worse
    than a missing row: it reads as covered."""
    by_id = {c["id"]: c["name"] for c in _slack_sources(reg)}
    for cid, name in MUST_CARRY.items():
        assert by_id.get(cid) == name, (
            "id %s is registered as %r, expected %r" % (cid, by_id.get(cid), name))


def test_every_slack_source_is_well_formed(reg):
    """An id that is not a channel id cannot be read, and a sweep cannot tell
    the difference between 'read it and found nothing' and 'never resolved'."""
    for c in _slack_sources(reg):
        assert c.get("id", "").startswith("C"), "bad channel id: %r" % (c,)
        assert c.get("name"), "source has no name: %r" % (c,)
        assert c.get("note"), "source %s has no note explaining why it is swept" % c["name"]


def test_no_duplicate_ids(reg):
    """A duplicated source is a channel read twice per sweep, 11x a day."""
    ids = [c["id"] for c in _slack_sources(reg)]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, "duplicate channel ids in the registry: %s" % dupes


def test_receipts_is_documented_as_a_non_text_source(reg):
    """#receipts is image-only (verified 2026-08-24: 12 messages, every one an
    image from Rafael with empty text). It must stay OUT of the Slack sweep
    groups — 11 text reads a day that structurally cannot yield a card — while
    staying documented in `other` so it is not rediscovered as 'missing'."""
    slack_ids = {c["id"] for c in _slack_sources(reg)}
    assert "C0BMJNXG82K" not in slack_ids, (
        "#receipts is image-only; adding it to the Slack sweep groups spends 11 "
        "model runs a day on a channel with no text to read")
    blob = json.dumps(reg["other"])
    assert "C0BMJNXG82K" in blob, (
        "#receipts must remain documented in `other`, with its image-only limit "
        "stated, so the gap stays visible instead of looking like an oversight")


# ── the CLOSED WORLD, and why this is the load-bearing test in this file ──
# Every other test here checks that something PRESENT is correct. None of them
# can see a channel that EXISTS IN SLACK and is in no list at all — which is the
# actual defect of 2026-08-24: `#financial` was absent, and the two tests above
# would have passed a registry that was missing it plus three job channels.
#
# The miss happened because a SET was proved with keyword searches
# (slack_search_channels "financial", then "receipts"). That can only return
# names already guessed, so it found 2 of 7. The registry's own contract already
# names the correct method — LIST every channel, then SUBTRACT the registry —
# and `pm_sweep.py --check-new` implements it. Nobody ran it for 24 days.
#
# So this fixes the DIRECTION of the check: every channel known to exist must be
# in exactly one of two lists — swept, or consciously excluded with a reason. A
# newly discovered channel then has nowhere to sit silently; it fails this test
# until a human decides which list it belongs in.
#
# HOW TO REFRESH (the step that was skipped):
#   python3 pm_sweep.py --check-new "$(comma-separated live channel ids)"
# Feed it a real listing, not a search. Anything it reports as unknown goes into
# EXCLUDED with a note or into sweep_sources.json.
EXCLUDED = {
    "C0BKDR6EN81": "#new-channel — workspace default from setup day (2026-07-23), never used",
    "C0BKDQBLJU9": "#social — workspace default from setup day (2026-07-23), non-work",
    "C0BMJNXG82K": "#receipts — image-only; verified 2026-08-24 that every message "
                   "is a photo from Rafael with empty text, so a TEXT sweep yields "
                   "nothing 11x a day. Documented in `other` instead.",
}

# Enumerated 2026-08-24 by listing channels and subtracting the registry — the
# method the 07-30 and 07-31 sweeps used, and the one my keyword search replaced.
KNOWN_CHANNEL_UNIVERSE = 20


def test_every_known_channel_is_either_swept_or_excluded(reg):
    """The two-directional gate. A channel in neither list is the 2026-08-24
    defect, and it must fail rather than pass quietly."""
    registered = {c["id"] for c in _slack_sources(reg)}
    overlap = registered & set(EXCLUDED)
    assert not overlap, (
        "these ids are both swept and excluded — one list is wrong: %s" % overlap)
    total = len(registered) + len(EXCLUDED)
    assert total == KNOWN_CHANNEL_UNIVERSE, (
        "the channel universe was %d at the 2026-08-24 reconcile; it is now %d "
        "(%d registered + %d excluded). Either a source was added/dropped without "
        "updating KNOWN_CHANNEL_UNIVERSE, or a channel is unaccounted for. Re-run "
        "`python3 pm_sweep.py --check-new <live ids>` against a REAL channel "
        "listing — not a keyword search, which is what missed 5 of 7 last time."
        % (KNOWN_CHANNEL_UNIVERSE, total, len(registered), len(EXCLUDED)))


def test_exclusions_each_carry_a_reason(reg):
    """An exclusion with no reason is indistinguishable from an oversight, and
    becomes permanent because nobody can tell it was a decision."""
    for cid, reason in EXCLUDED.items():
        assert cid.startswith("C"), "bad excluded id: %r" % cid
        assert len(reason) > 30, (
            "exclusion %s has no real reason — say WHY it is not swept, or the "
            "next reader cannot tell a decision from a miss" % cid)


def test_registry_records_when_it_was_last_refreshed(reg):
    """The 28-day drift was invisible partly because nothing forced this field
    to move when sources changed."""
    assert reg.get("updated"), "registry has no `updated` date"
    assert reg["updated"] >= "2026-08-24", (
        "registry `updated` is %s — older than the #financial fix, so either the "
        "fix was reverted or a later edit forgot to stamp it" % reg["updated"])
