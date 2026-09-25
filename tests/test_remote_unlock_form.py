#!/usr/bin/env python3
"""The remote page's unlock field must be one a password manager can remember.

It used to be a bare `<input type="password" autocomplete="off">`. `autocomplete="off"`
is an instruction to the browser NOT to remember it, so the Mac password had to be
retyped on every single unlock — and openUnlockModal() cleared the field on OPEN, which
would have undone an autofill even if one had happened.

What a manager needs is mundane and specific: a real <form>, a username field to file
the credential under, `autocomplete="current-password"` on the password, and a SUBMIT
event (managers do not offer to save on a plain click handler). Where the Credential
Management API exists, storing it explicitly after a unlock that WORKED makes it
deterministic instead of heuristic.

Note what is NOT done: cc-web still stores no password anywhere. The secret ends up in
the browser's / keychain's store, with the user's consent — which is why the dialog's
own text had to change too, since it promised "never stored".

Also checked: the ← sessions link. The page is a full navigation from the session list,
and installed as a web app on a phone there is no browser Back to press.

    python3 tests/test_remote_unlock_form.py      # exit 0 = pass
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "remote_mac_ctrl_static", "index.html")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    s = open(PAGE, encoding="utf-8").read()
    m = re.search(r'<div class="modal-bg" id="unlockModal">.*?\n</div>', s, re.S)
    if not m:
        print("  FAIL  could not find the unlock modal"); return 1
    modal = m.group(0)

    print("=== the markup a password manager needs ===")
    check("the fields live in a real form", '<form id="unlockForm"' in modal)
    # The one that broke it. Kept as an explicit check because "off" is the default
    # thing to type when you are being careful, and it has exactly the opposite effect.
    check("...the password field is no longer autocomplete=off",
          'id="unlockPassword"' in modal
          and not re.search(r'id="unlockPassword"[^>]*autocomplete="off"', modal),
          re.search(r'<input[^>]*id="unlockPassword"[^>]*>', modal).group(0)[:110])
    check("...it is marked as the current password",
          re.search(r'id="unlockPassword"[^>]*autocomplete="current-password"', modal) is not None)
    check("...and there is a username field to file it under",
          re.search(r'id="unlockUser"[^>]*autocomplete="username"', modal) is not None)
    check("...which is read-only: it names the host, it is not something to fill in",
          re.search(r'id="unlockUser"[^>]*readonly', modal) is not None)
    check("the send button submits the form",
          re.search(r'type="submit"[^>]*id="unlockSendBtn"', modal) is not None)

    print("=== and the behaviour around it ===")
    # Managers offer to save on submit; a click handler alone never triggers it.
    check("submitting the form is what sends it",
          "unlockFormEl.addEventListener('submit'" in s and "ev.preventDefault();" in s)
    # This is the subtle one: clearing on open would wipe an autofilled password.
    _open = s[s.index("function openUnlockModal()"):]
    _open = _open[:_open.index("\n  }")]
    check("opening the dialog does NOT clear the field any more",
          "unlockPasswordEl.value = ''" not in _open, _open.strip()[:80])
    _close = s[s.index("function closeUnlockModal()"):]
    _close = _close[:_close.index("\n  }")]
    check("...but closing it still does, so it does not sit in the DOM",
          "unlockPasswordEl.value = ''" in _close)
    # Only after a unlock that actually worked — offering to save a wrong password is
    # worse than not offering.
    _sub = s[s.index("async function submitUnlock()"):]
    _sub = _sub[:_sub.index("\n  }")]
    i_ok, i_store = _sub.find("unlock sent"), _sub.find("navigator.credentials.store")
    check("the explicit save happens only after a successful unlock",
          0 < i_ok < i_store, f'{i_ok} / {i_store}')
    check("...guarded, because Safari has no such API",
          "window.PasswordCredential" in _sub and "catch (e)" in _sub[i_store:i_store + 400])
    check("...filed under the host, so one browser can hold one per machine",
          "const unlockId = (location.hostname" in s)

    print("=== the dialog no longer claims the password is never stored ===")
    # It IS stored now — by the browser, with consent. The old sentence would be a lie,
    # and this file exists partly so the sentence and the behaviour stay in step.
    hint = re.search(r'<p class="hint">(.*?)</p>', modal, re.S).group(1)
    check("the 'never stored' promise is gone", "never stored" not in hint, hint[-90:])
    check("...replaced by what actually happens",
          "stores nothing" in hint and ("keychain" in hint.lower() or "BROWSER" in hint),
          hint[-120:])

    print("=== back to the session list ===")
    check("there is a real link, not a script-only button",
          re.search(r'<a class="backlink" href="/"', s) is not None)
    check("...and it is styled to be visible", ".backlink {" in s)

    print("=== the desktop control page: fill the window, follow the theme ===")
    pc = open(os.path.join(ROOT, "remote_pc_static", "index.html"), encoding="utf-8").read()
    # 1:1 pixels are right on a desktop and wrong on a tablet, where the shot sat in a
    # black frame using a fraction of the screen.
    check("there is a fit switch, on by default",
          re.search(r'id="fitChk"[^>]*checked', pc) is not None)
    check("...remembered, so a desktop can keep real pixels",
          "cc_remote_pc_fit" in pc and "localStorage.setItem(FIT_KEY" in pc)
    # The size is computed rather than left to object-fit, because the <img> box has to
    # stay exactly the painted image: clicks are converted through clientWidth/Height,
    # and letterbox margins inside the box would map onto the screen anyway.
    # Bound by the HEIGHT alone now: min(width, height) is what left a band of empty
    # stage above and below the shot once the keys took a column of the width. What does
    # not fit across is panned inside the stage instead.
    check("...the fitted size is computed from the height, not object-fit",
          "function applyFit()" in pc and "const fitTo = (h) => {" in pc
          and "Math.min(box.width / nw" not in pc
          and "object-fit" not in pc.split("body.fit")[1][:400])
    check("...and re-fitted to clientHeight when a scrollbar takes some of it",
          "if (main.scrollHeight > main.clientHeight + 1) fitTo(main.clientHeight);" in pc)
    check("...recomputed on resize and after each new shot",
          "window.addEventListener('resize', applyFit)" in pc
          and pc.count("applyFit();") >= 3)
    # The bug this would otherwise have introduced.
    check("clicks are converted through the DISPLAYED size",
          "screenImg.clientWidth || screenImg.naturalWidth" in pc)

    check("the page follows cc_web_theme like the others",
          "cc_web_theme" in pc and "body.light {" in pc and "body.pure {" in pc)
    check("...applied before paint, so there is no dark flash",
          pc.index("cc_web_theme") < pc.index("<header>"))
    # pure is for e-ink: an accent-filled button becomes black-on-black.
    check("...and pure turns the primary button into an outline",
          "body.pure header button.primary" in pc)
    check("both control pages have a way back to the sessions",
          'class="backlink" href="/"' in pc)

    print("=== the DESKTOP page has its own unlock dialog — same treatment ===")
    # Two control pages, two dialogs. The desktop one went further than autocomplete=off:
    # it INJECTED the field on open with the stated reason "so Chrome's password manager
    # doesn't see a permanent type=password" — hiding from the manager deliberately.
    check("its password field is permanent, not injected on open",
          "unlockPwdSlot" not in pc and 'id="unlockPwd"' in pc)
    check("...in a form, with a username to file it under",
          '<form id="unlockForm"' in pc
          and re.search(r'id="unlockUser"[^>]*autocomplete="username"', pc) is not None)
    check("...marked as the current password",
          re.search(r'id="unlockPwd"[^>]*autocomplete="current-password"', pc) is not None)
    check("...sent by submitting the form", "unlockFormEl.addEventListener('submit'" in pc)
    check("...saved only after a unlock that worked",
          pc.index("unlock sent") < pc.index("navigator.credentials.store"))
    _po = pc[pc.index("function openUnlockModal()"):]
    _po = _po[:_po.index("\n}")]
    check("...not cleared on open (that would undo an autofill)",
          "unlockPwdEl.value = ''" not in _po, _po.strip()[:70])
    # The old comment argued FOR hiding from the manager. It is quoted now rather than
    # deleted — what was believed, and why it changed, is the useful part — so what this
    # checks is that the quote comes with the reversal, not on its own.
    _c = pc[pc.index("<!-- Unlock modal"):]
    _c = _c[:_c.index("-->")]
    check("...and the comment records the reversal, not just the old reason",
          "doesn't see a permanent type=password" in _c and "reversed" in _c, _c[:90])

    print("=== typing on the desktop page: ONE implementation ===")
    # The page already had the thing: the `type` click-mode opens an overlay with an
    # input, Send, a Live toggle and a sharp crop of where the words are going — the
    # phone's liveText, with a picture. It was only reachable by clicking the screenshot
    # with that mode selected, so it did not look like "a place to type", and a SECOND
    # input box got built beside it. That box is gone; the button summons the overlay
    # that was always there.
    for gone in ("sayBox", "sayLive", "SAY_KEYS", "sayToMac", "sayPost"):
        check(f"...the duplicate input is gone ({gone})", gone not in pc)
    check("a key-bar button opens the existing overlay",
          'id="typeHere"' in pc and "enterTypeMode(x, y, frac)" in pc)
    check("...without needing a click on the screenshot first",
          "lastTypeAt ? lastTypeAt.x : screenImg.clientWidth / 2" in pc)
    check("...at the last place you typed, when there is one",
          "let lastTypeAt = null" in pc and "lastTypeAt = { x: wrapX, y: wrapY }" in pc)
    check("...and it says it is the same thing as the click-mode",
          "和「⌨ type after click」是同一个东西" in pc)

    print("=== the theme reaches the big surfaces, not just the variables ===")
    # This is why the first attempt looked like it had done nothing: header, the
    # screenshot stage, the key bar and the footer all hardcoded #000, so adding a light
    # theme changed almost nothing you could see.
    css = pc[pc.index("<style>"):pc.index("</style>")]
    blocks = re.findall(r"([^\n{}]*)\{([^}]*)\}", css)
    hard = [(sel.strip()[:40], h) for sel, body in blocks
            if not (sel.strip().startswith(":root") or "body.light" in sel or "body.pure" in sel)
            for h in re.findall(r"(#[0-9a-fA-F]{3,8})\b", body)]
    check("no rule outside the theme blocks hardcodes a colour", hard == [], str(hard[:6]))
    for v in ("--chrome", "--stage", "--guide"):
        check(f"...{v} is defined for all three modes",
              css.count(v + ":") >= 3, str(css.count(v + ":")))

    print("=== a wide, short window: keys and status beside the image ===")
    # The complaint: on a 21:9 window the image was a strip in the middle while three
    # rows of chrome ate the height and the space either side of it sat empty.
    check("keybar, stage and status share one grid",
          'id="stagewrap"' in pc and 'grid-template-areas: "keys" "stage" "status"' in pc)
    # Grid rather than flex for a reason worth keeping: side mode needs keys AND status
    # stacked in the left column with the stage spanning both rows.
    # RIGHT, not left: the shot is left-aligned, so the leftover is one block on the
    # right and that is where the controls go. (Built on the left first — wrong half of
    # the same decision.)
    check("...and side mode puts both in the RIGHT column, stage spanning",
          'grid-template-areas: "stage keys" "stage status"' in pc
          and "grid-template-columns: minmax(0, 1fr) var(--keycol)" in pc)
    # max-content asked the column what it wanted, and the widest thing in it is the
    # status line: "1470×956 pt · … · 145 KB" made the column 451px in a 1440px window
    # for 188px of keys. The shot lost 250px for nothing — fitted to the height it then
    # did not fit across, so it became something you had to scroll sideways.
    check("...at a fixed width, so the status line cannot widen it",
          "--keycol:" in pc and "max-width: 34vw" not in pc
          and "overflow-wrap: anywhere" in pc)
    check("...with the status stacked, not laid across",
          "body.side #stagewrap > footer {" in pc and "flex-direction: column" in pc)
    # No longer conditional. It WAS decided by the window's aspect against the shot's
    # (`winA > (nw / nh) * 1.1`), so an ordinary browser window — merely a bit wider than
    # tall — failed that test and got the horizontal strip back: six groups in a ragged
    # row across the top, eating the height the shot wants. The column costs ~200px of
    # width and returns ~150px of height in every window, so there is nothing left to
    # decide; a phone gets it too and scrolls sideways (see test_remote_keypad.py).
    check("the keys are the right-hand column unconditionally",
          "classList.add('side')" in pc and "winA >" not in pc)
    check("...set before the early return, so it holds with no shot and with fit off",
          pc.index("classList.add('side')") < pc.index("if (!fitChk.checked || !nw || !nh)"))
    check("...and the stage is still measured after the classes change its area",
          pc.index("classList.add('side')")
          < pc.index("const main = screenImg.closest('main')")
          and pc.index("classList.toggle('narrow'")
          < pc.index("const main = screenImg.closest('main')"))
    # Left, not centred: the leftover is one margin on the right, not two around it.
    check("the shot is left-aligned in fit mode",
          "justify-content: flex-start" in pc and "align-items: flex-start" in pc)

    print("=== which screen: say what the choice IS ===")
    # "Display: main / external" is the API's vocabulary. It never said what you were
    # choosing between, and it hid something worse: resolve_display("external") falls
    # back to main when nothing is plugged in, so picking it showed the main screen and
    # nothing said so — indistinguishable from a radio that does not work.
    srv2 = open(os.path.join(ROOT, "remote_mac_ctrl.py"), encoding="utf-8").read()
    check("the backend reports how many screens there are",
          "def active_display_count()" in srv2 and '"X-Display-Count"' in srv2)
    check("...and whether the one it captured was the main one",
          '"X-Display-Is-Main"' in srv2)
    for name, page in (("phone", s), ("desktop", pc)):
        # 内屏/外屏 — two characters each, and no "屏幕:" in front of them: the options
        # already say what the choice is, and header width is the scarce thing here.
        check(f"[{name}] the options are 内屏 / 外屏",
              "内屏" in page and "外屏" in page
              and not re.search(r'value="main"[^>]*>\s*main', page), name)
        # Checked on the MARKUP, not the whole file: the comment above the control
        # quotes the label it replaced, and a test that trips over its own rationale is
        # a test nobody keeps.
        _mk = re.sub(r"<!--.*?-->", "", page, flags=re.S)
        check(f"[{name}] ...with no label repeating what they say",
              "屏幕:" not in _mk and "内置" not in _mk)
        check(f"[{name}] ...with a tooltip saying it picks which screen",
              "截哪块屏幕" in page)
        check(f"[{name}] ...外接 is disabled when there is only one screen",
              "function applyDisplayCount" in page and "ext.disabled = only" in page)
        check(f"[{name}] ...and says so instead of showing the wrong screen quietly",
              "没有外屏" in page and "显示的是内屏" in page)

    print("=== the pages must not be served from a stale cache ===")
    # The failure this prevents is indistinguishable from "the deploy did not work":
    # the files on the host were byte-identical to the source and the page on the tablet
    # was days old, because these mounts sent an ETag and NO cache directive, and a
    # browser may reuse that without asking. / has always sent no-store; these did not.
    srv = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    check("there is a no-store static class", "class _NoStoreStatic(StaticFiles):" in srv)
    check('...setting Cache-Control: no-store', '"Cache-Control"] = "no-store"' in srv)
    for mount in ("/remote", "/remote_pc"):
        check(f"...used for the {mount} mount",
              f'app.mount("{mount}", _NoStoreStatic(' in srv)
    # Deliberately NOT global: /static carries marked.min.js and the icons, which are
    # big and unchanging, and making those uncacheable would cost every page load.
    check("/static keeps its caching", 'app.mount("/static", StaticFiles(' in srv)

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
