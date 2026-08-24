#!/usr/bin/env python3
"""The report's day must NEVER come from the board's brief_date (2026-08-24).

WHAT WENT WRONG
---------------
`gen_daily_report.py` resolved the reported day as:

    day = a.day or st.get("brief_date") or S._today()

`brief_date` answers "has the board been rolled?" — NOT "which day does this
report cover?". The comment above the `--day` argument said exactly that, and
`--day` was added on 2026-07-30 to decouple the two. But the DEFAULT was left
pointing at `brief_date`, so the questions stayed coupled in the only place it
mattered: the path taken when nobody passes `--day`.

The board's `brief_date` has read **2026-07-29** ever since the day-roll last
ran (once, ever — `last_swept_at` froze 2026-07-31). So on 2026-08-24, any run
without an explicit `--day` produced a report **dated and scoped to July 29** —
twenty-six days stale. These reports go to FD and to Rafael.

This is the failure mode where a stale watermark silently outranks today's
date. It never raises an error; it just quietly reports on the wrong day, and
the reader has no way to tell.

WHAT THIS LOCKS
---------------
1. The day-resolution expression does not reference `brief_date` at all. Not as
   a fallback, not as a last resort. If the caller does not say which day it
   wants, the answer is TODAY.
2. `--day` still wins when passed, so an intentional backfill of an older day
   keeps working.
3. The `--day` help text does not promise a `brief_date` default, because a CLI
   that documents itself wrongly is its own defect.

WHY IT IS WRITTEN AGAINST THE SOURCE, NOT BY CALLING main()
-----------------------------------------------------------
The resolution is a single inline expression inside `main()`, and `main()`
writes an HTML file to disk. Testing it by invocation would mean either
refactoring a live generator or producing real files as a side effect of the
suite. Reading the assignment out of the AST is exact, has no side effects, and
fails loudly on the pre-fix source — which is the whole point of a regression
lock.

Run:  cd ~/rfs_pm && python3 -m pytest tests -q
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "gen_daily_report.py"


def _day_assignment():
    """Return the AST value node for the `day = ...` assignment inside main()."""
    tree = ast.parse(SRC.read_text())

    main = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main is not None, "gen_daily_report.py has no main() — did it move?"

    for node in ast.walk(main):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "day":
                    return node.value

    pytest.fail("no `day = ...` assignment found in main() — did it get renamed?")


def test_day_resolution_never_reads_brief_date():
    """The pre-fix source had `st.get("brief_date")` here. It must not come back."""
    value = _day_assignment()

    strings = [
        n.value for n in ast.walk(value)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert "brief_date" not in strings, (
        "day resolution reads brief_date again. A board watermark is not a "
        "report date: brief_date has been 2026-07-29 since the only day-roll "
        "that ever ran, so this silently back-dates reports sent to FD and "
        "Rafael. If no --day is given, the answer is today."
    )

    # Belt and braces: catch an attribute-style read too (st.brief_date).
    attrs = [n.attr for n in ast.walk(value) if isinstance(n, ast.Attribute)]
    assert "brief_date" not in attrs, "brief_date reached the day via attribute access"


def test_day_resolution_still_prefers_an_explicit_day():
    """Backfilling an older day with --day must keep working."""
    value = _day_assignment()

    # Expect `a.day or S._today()` — an `or` whose first operand is the CLI arg.
    assert isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or), (
        "day resolution is no longer an `or` chain; re-check that --day still "
        "takes precedence over today"
    )

    first = value.values[0]
    assert isinstance(first, ast.Attribute) and first.attr == "day", (
        "the FIRST operand of the day chain is not the --day argument, so an "
        "explicit --day may no longer win"
    )


def test_day_help_text_does_not_promise_brief_date():
    """A CLI that documents a default it no longer has is its own defect."""
    tree = ast.parse(SRC.read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if "--day" not in flags:
            continue

        help_kw = next((k.value for k in node.keywords if k.arg == "help"), None)
        assert help_kw is not None, "--day lost its help text"

        help_text = " ".join(
            n.value for n in ast.walk(help_kw)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        assert "defaults to the board's brief_date" not in help_text, (
            "--day still advertises a brief_date default it no longer has"
        )
        assert "TODAY" in help_text.upper(), (
            "--day help should state that the default is today"
        )
        return

    pytest.fail("no add_argument('--day', ...) found — did the flag get renamed?")
