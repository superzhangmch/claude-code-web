#!/usr/bin/env python3
"""The tabs viewer's key row: every button must arrive as exactly the bytes it prints.

That row exists because a phone keyboard cannot send Esc, arrows, ^C — or shift+tab,
which is how claude cycles its permission mode ("auto mode on (shift+tab to cycle)").
Typing the sequence into the text box beside the row is not a substitute: that path
treats what you type as TEXT, so the escape would arrive as the characters `^[[Z`.

So each key is `data-raw` + `raw: true`, and the server must pass it through untouched —
no trailing CR, no bracketed paste, no re-encoding. This drives the real endpoint with
the bytes parsed out of the real markup, so a key added to the row without being sendable
(or a raw path that starts "helping") fails here.

    python3 tests/test_key_row.py       # exit 0 = pass
"""
import asyncio
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "index.html")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def _dec(v):
    """Decode the attribute the way a BROWSER does.

    Not html.unescape: it follows HTML5's "invalid code point" list and returns the empty
    string for &#27; / &#3; / &#26; — exactly the control characters this row is made of.
    Browsers emit them (a parse error, per spec, but the character is still used), which
    is why Esc and ^C have always worked. Decoded here, that difference would have read as
    "five keys send nothing".
    """
    v = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), v)
    v = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), v)
    return (v.replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&quot;", '"'))


def keys_from_markup():
    """[(label, bytes)] for the data-raw buttons on the row, in screen order."""
    src = open(INDEX, encoding="utf-8").read()
    m = re.search(r'<div id="tabs-keys".*?</div>', src, re.S)
    if not m:
        return None
    out = []
    for b in re.finditer(r'<button class="tk" data-raw="([^"]*)"[^>]*>([^<]*)</button>', m.group(0)):
        out.append((_dec(b.group(2)).strip(), _dec(b.group(1))))
    return out


def main():
    keys = keys_from_markup()
    if not keys:
        print("  FAIL  could not find the #tabs-keys row in static/index.html"); return 1

    print("=== the row itself ===")
    labels = [k[0] for k in keys]
    by_label = dict(keys)
    check("the row still has its keys", len(keys) >= 10, str(len(keys)))
    # CSI Z. Not \t with a modifier flag and not ESC-TAB: back-tab is its own sequence,
    # and it is what claude's TUI matches on.
    check("⇧Tab is on the row", "⇧Tab" in by_label, str(labels))
    check("...as CSI Z, the standard back-tab sequence",
          by_label.get("⇧Tab") == "\x1b[Z", repr(by_label.get("⇧Tab")))
    check("...next to Tab, where you would look for it",
          "Tab" in labels and "⇧Tab" in labels
          and labels.index("⇧Tab") == labels.index("Tab") + 1, str(labels[:5]))
    # A key that sends nothing is a button that looks broken rather than one that is.
    check("no key sends an empty string", all(v for _, v in keys),
          str([k for k, v in keys if not v]))

    home = tempfile.mkdtemp(prefix="ccweb-keys-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    sys.path.insert(0, ROOT)
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    sent = []

    class FakeBridge:
        async def ensure_connected(self):
            return True

        async def send_text_to(self, sid, text):
            sent.append((sid, text))
            return True

    cc_web.bridge = FakeBridge()

    async def send(text, raw=False, press_enter=False):
        sent.clear()
        await cc_web.post_iterm_input(cc_web.ItermInputPayload(
            iterm_session_id="w0t0p0", text=text, raw=raw, press_enter=press_enter))
        return sent[0][1] if sent else None

    async def go():
        print("=== every key reaches the tab as itself ===")
        for label, raw in keys:
            got = await send(raw, raw=True)
            check(f"{label} → {raw!r}", got == raw, repr(got))

        print("=== ...and the raw path adds nothing of its own ===")
        # The two things the typed path does, neither of which may happen to a keystroke:
        # a trailing CR would submit whatever is in the input box, and the paste brackets
        # would be read as text by anything that has them off.
        check("press_enter is ignored for a raw key (no CR appended)",
              await send("\x1b[Z", raw=True, press_enter=True) == "\x1b[Z")
        check("a raw multi-line string is not bracket-pasted",
              await send("a\nb", raw=True) == "a\nb")

        print("=== the typed path is unchanged ===")
        check("typed text gets its CR when asked",
              await send("jobs", press_enter=True) == "jobs\r")
        check("...and none when not", await send("jobs") == "jobs")
        check("multi-line typed text is bracket-pasted",
              await send("line1\nline2") == "\x1b[200~line1\nline2\x1b[201~")

    asyncio.run(go())
    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
