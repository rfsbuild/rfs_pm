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


def test_registry_records_when_it_was_last_refreshed(reg):
    """The 28-day drift was invisible partly because nothing forced this field
    to move when sources changed."""
    assert reg.get("updated"), "registry has no `updated` date"
    assert reg["updated"] >= "2026-08-24", (
        "registry `updated` is %s — older than the #financial fix, so either the "
        "fix was reverted or a later edit forgot to stamp it" % reg["updated"])
