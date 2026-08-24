"""The prompt must state the lane vocabulary, sourced from pm_state.LANES.

2026-08-24. The first real supervised sweep ran 14m45s, retrieved 320 Slack
messages and 158 Gmail threads, and produced 30 items with 19 drafts. Every
single one was discarded. `pm_ingest` validates `lane` against pm_state.LANES
and the rejection is all-or-nothing, and the model had used:

    money x10 · client x7 · ops x7 · process x3 · compliance x2 · risk x1

Zero of thirty were real lanes. It could not have known them —
`grep -c "urgent" pm_sweep_run.py` returned **0**. The prompt asked for a
`lane` and never said what one was.

This is the 2026-07-30 failure in the next field over. That one discarded 19
good items because the contract said "MUST carry a draft" and left the SHAPE to
be guessed; the fix spelled out the draft object and stopped there, leaving the
lane vocabulary just as implicit. Fixing one field of a contract does not fix
the field beside it.

Rendered from S.LANES rather than typed, because a literal list would already
be stale: `routine` was added the same day. And the topical names the model
invented were reasonable — a lane is WHO ACTS NEXT, which is not guessable from
the word "lane" — so the prompt has to say so.
"""
import pm_state as S
import pm_sweep_run as R


def test_every_lane_appears_in_the_prompt():
    """The whole vocabulary, not a sample."""
    _, _, prompt = R.build_prompt({"items": []})
    for lane in S.LANES:
        assert lane in prompt, (
            "lane %r is valid in pm_state but absent from the prompt — the model "
            "cannot use a lane it is never shown" % lane)


def test_lane_help_covers_pm_state_exactly():
    """Guards the drift that made this bug possible: a lane added to pm_state
    with nothing added here. `routine` was added 2026-08-24 and would have been
    exactly this case."""
    assert set(S.LANES) == set(R.LANE_HELP), (
        "LANE_HELP and pm_state.LANES disagree: only-in-LANES=%s "
        "only-in-HELP=%s" % (set(S.LANES) - set(R.LANE_HELP),
                             set(R.LANE_HELP) - set(S.LANES)))


def test_lanes_text_raises_rather_than_silently_omitting():
    """A missing description must fail at build time, not ship a prompt that
    quietly lacks a lane."""
    saved = R.LANE_HELP.copy()
    try:
        R.LANE_HELP.pop(S.LANES[0])
        try:
            R._lanes_text()
        except AssertionError:
            pass
        else:
            raise AssertionError(
                "_lanes_text() silently omitted a lane with no LANE_HELP entry")
    finally:
        R.LANE_HELP.clear()
        R.LANE_HELP.update(saved)


def test_the_invented_lanes_are_named_as_wrong():
    """The six lanes the model actually invented are called out by name, so the
    prompt corrects the specific wrong instinct rather than only asserting a
    rule. A rule without the counter-example is what failed the first time."""
    _, _, prompt = R.build_prompt({"items": []})
    for invented in ("money", "client", "ops", "risk"):
        assert invented in prompt, (
            "the prompt should name %r as an invalid topical lane — it is what "
            "the 2026-08-24 run actually produced" % invented)
    assert "WHO ACTS NEXT" in prompt, (
        "the prompt must say what a lane IS, not just list the legal values — "
        "the model's topical guess was reasonable without that sentence")


def test_one_bad_lane_rejects_everything_is_stated():
    """The cost of a single bad value has to be in the prompt, because it is
    what turns a small formatting slip into 30 discarded items."""
    _, _, prompt = R.build_prompt({"items": []})
    low = prompt.lower()
    assert "entire briefing" in low or "all-or-nothing" in low, (
        "the prompt must state that ONE bad lane rejects the whole briefing")


def test_routine_is_fenced_off_from_the_sweep():
    """pm_routine.py owns the `routine` lane and resets it every morning,
    touching only ids it minted (`rt_`). A sweep card parked there is wiped
    daily, which looks like the board losing her work."""
    _, _, prompt = R.build_prompt({"items": []})
    i = prompt.index("routine")
    assert "DO NOT USE" in prompt[i:i + 200], (
        "the prompt must tell the sweep not to write into the `routine` lane")
