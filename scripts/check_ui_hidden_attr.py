#!/usr/bin/env python3
"""Every element toggled with the `hidden` attribute must have a CSS escape hatch.

WHY (2026-09-15). The not-saved bar was given `.savefail{display:flex}`. That is
an AUTHOR rule; `[hidden]{display:none}` comes from the user-agent stylesheet and
loses to it. So `el.hidden = true` set the attribute and changed nothing: the bar
sat on screen from page load with empty text, and its own Dismiss button appeared
dead. Hadassa hit it within the hour of it shipping.

The JS test could not catch this — it asserts `el.hidden === true` against a stub
object, and a stub has no stylesheet. This is the static half of that gate: for
every id rendered with a `hidden` attribute, require a matching
`.cls[hidden]{display:none…}` rule when the element's own class sets `display`.

    python3 scripts/check_ui_hidden_attr.py        # exit 0 = clean
"""
import os
import re
import sys

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pm_ui.html")


def main():
    src = open(UI).read()
    fails = []

    # elements written with a bare `hidden` attribute
    for m in re.finditer(r'<(\w+)([^>]*\bid="([\w-]+)"[^>]*)\bhidden\b', src):
        tag, attrs, eid = m.group(1), m.group(2), m.group(3)
        classes = re.findall(r'class="([^"]+)"', attrs)
        classes = classes[0].split() if classes else []
        # find any author rule that gives one of its classes a display
        displayed = [c for c in classes
                     if re.search(r'^\.%s\{[^}]*display:' % re.escape(c), src, re.M)]
        if not displayed:
            continue
        for c in displayed:
            if not re.search(r'\.%s\[hidden\]\s*\{[^}]*display:\s*none' % re.escape(c), src):
                fails.append(
                    "#%s uses the `hidden` attribute and .%s sets `display:` — "
                    "the attribute will NOT hide it. Add `.%s[hidden]{display:none!important;}`"
                    % (eid, c, c))

    # and the inverse: a [hidden] rule with nothing using the attribute is dead weight
    for m in re.finditer(r'\.([\w-]+)\[hidden\]', src):
        c = m.group(1)
        if not re.search(r'class="[^"]*\b%s\b[^"]*"[^>]*hidden' % re.escape(c), src):
            pass   # harmless; a rule may guard an element toggled only from JS

    for f in fails:
        print("FAIL  " + f)
    if not fails:
        print("ok    every `hidden`-toggled element with a display rule has a [hidden] override")
    print("\n%d FAIL" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
