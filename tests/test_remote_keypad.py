#!/usr/bin/env python3
"""The desktop remote's key column: shaped like a keyboard, not a list of 24 buttons.

Everything was one flat wrapping list, which in the side column ran straight down the
edge of the screenshot: nothing to aim at, and it spent the height the screenshot wants.
Keys that belong together are now small grids — the arrows in their inverted-T (↑ above
↓, ← and → beside it), paging above jump-to-end, the rest in two columns.

Geometry, in a real browser, off the real file: "like a keyboard" is a claim about where
things are, and the arrow cluster is placed by grid-area — exactly the kind of rule that
breaks silently if a button is added to the group without one.

Also here: the pointer over the screenshot. That surface is a thing you click (the click
lands on the mac at that spot), so it wears a hand; `grabbing` while dragging stays.

    python3 tests/test_remote_keypad.py      # exit 0 = pass  (skips without firefox)
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "remote_pc_static", "index.html")

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    src = open(PAGE, encoding="utf-8").read()

    print("=== the screenshot is a click target, so it wears a hand ===")
    i = src.index("#capture {")
    blk = src[i:i + 400]
    check("the capture layer's cursor is a pointer", "cursor: pointer;" in blk, blk[-90:])
    check("...and a crosshair is not what it uses", "cursor: crosshair" not in blk)
    check("...while a drag still says grabbing", "#capture.dragging { cursor: grabbing; }" in src)

    if not (shutil.which("geckodriver") and shutil.which("firefox")):
        print("SKIP (geometry): needs geckodriver + firefox")
        print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
        return 1 if _fails else 0

    from test_ui_smoke import Driver              # the same tiny WebDriver client (its
    # call() surfaces a JS error's message, which is the difference between five seconds
    # and five minutes of hunting)

    port = 4599
    gecko = subprocess.Popen(["geckodriver", "--port", str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    drv = None
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        drv = Driver(port, width=1200, height=800)
        # file:// — the /remote_pc mount only exists on a mac (Quartz), and the layout
        # under test is CSS, which does not need the backend.
        drv.go("file://" + PAGE)
        drv.wait("!!document.getElementById('keybar')")

        for side in (False, True):
            label = "side column" if side else "horizontal bar"
            g = drv.js("""
              document.body.classList.%s('side');
              const R = (s) => document.querySelector(s).getBoundingClientRect();
              const up = R('#keybar .kgrid.nav .k-up'), dn = R('#keybar .kgrid.nav .k-down');
              const lf = R('#keybar .kgrid.nav .k-left'), rt = R('#keybar .kgrid.nav .k-right');
              const mid = (r) => (r.left + r.right) / 2;
              const groups = [...document.querySelectorAll('#keybar .kgrid')];
              return {
                upOverDown: up.bottom <= dn.top + 1,
                upCentred: Math.abs(mid(up) - mid(dn)) <= 1,
                sameRow: Math.abs(lf.top - dn.top) <= 1 && Math.abs(rt.top - dn.top) <= 1,
                leftOfDown: lf.right <= dn.left + 1,
                rightOfDown: rt.left >= dn.right - 1,
                nothingBesideUp: true,
                groups: groups.length,
                // every key still reachable: nothing ended up at 0×0 or off-screen
                hidden: [...document.querySelectorAll('#keybar button')]
                          .filter(b => { const r = b.getBoundingClientRect();
                                         return r.width < 8 || r.height < 8; }).length,
                cols: groups.map(g => getComputedStyle(g).gridTemplateColumns.split(' ').length),
                // Fixed-width cells clip a label that outgrows them — and a clipped key
                // is a key you cannot read.
                clipped: [...document.querySelectorAll('#keybar .kgrid button')]
                           .filter(b => b.scrollWidth > b.clientWidth + 1)
                           .map(b => b.textContent.trim()),
              };
            """ % ("add" if side else "remove"))
            check(f"[{label}] ↑ sits above ↓",
                  g["upOverDown"] is True and g["upCentred"] is True, str(g))
            check(f"[{label}] ...with ← ↓ → on one row under it",
                  g["sameRow"] is True and g["leftOfDown"] is True and g["rightOfDown"] is True,
                  str(g))
            check(f"[{label}] ...and no key is collapsed or invisible",
                  g["hidden"] == 0, f'{g["hidden"]} of them')
            check(f"[{label}] ...six groups, none of them a single column",
                  g["groups"] == 6 and all(c >= 2 for c in g["cols"]),
                  f'{g["groups"]} groups, cols={g["cols"]}')
            check(f"[{label}] ...and no label clipped by its cell",
                  g["clipped"] == [], str(g["clipped"]))

        # The point of grouping: the strip gets SHORTER, which is the height the
        # screenshot was being denied.
        # …and the keys that are one family are actually together. "Organised" is not a
        # property of the container; it is where home is relative to PgUp. home/end and
        # PgUp/PgDn belong to the same block on every keyboard, and they were three
        # groups apart — home/end had been filed with the delete keys because they are
        # sent as Ctrl+A / Ctrl+E, which is how they work, not what they are for.
        p = drv.js("""
          document.body.classList.add('side');
          const by = (t) => [...document.querySelectorAll('#keybar button')]
                              .find(b => b.textContent.trim().startsWith(t));
          const R = (t) => by(t).getBoundingClientRect();
          const row = (a, b) => Math.abs(R(a).top - R(b).top) <= 1;
          const under = (a, b) => R(a).top >= R(b).bottom - 1;
          const leftOf = (a, b) => R(a).right <= R(b).left + 1;
          return {
            // Paging flanks ↑, the way a compact keyboard has it (fn+↑ / fn+↓):
            //     PgUp  ↑  PgDn
            //       ←   ↓   →
            pagingWithArrows: by('PgUp').closest('.kgrid') === by('↑').closest('.kgrid')
                           && by('PgDn').closest('.kgrid') === by('↑').closest('.kgrid'),
            pgUpLeftOfUp: row('PgUp', '↑') && leftOf('PgUp', '↑'),
            pgDnRightOfUp: row('PgDn', '↑') && leftOf('↑', 'PgDn'),
            arrowsUnderPaging: under('←', 'PgUp') && under('→', 'PgDn'),
            // home/end directly under the cluster — same family, one group apart at most
            homePair: row('⌃A', '⌃E') && leftOf('⌃A', '⌃E') && under('⌃A', '↓'),
            endsUnderHome: under('⤒', '⌃A') && row('⤒', '⤓'),
            editPair: row('⏎', 'Esc'),
            copyPair: row('⌘C', '⌘V'),
            // Window management with the system keys, not with the scroll keys.
            winWithSystem: by('all win').closest('.kgrid') === by('fullScr').closest('.kgrid'),
            // An odd key out fills the row instead of leaving a hole beside it.
            wideKeys: [...document.querySelectorAll('#keybar .k-wide')].map(b => b.textContent.trim()),
            wakeWide: Math.round(by('Wake').getBoundingClientRect().width)
                   === Math.round(by('⏎').getBoundingClientRect().width
                                + by('Esc').getBoundingClientRect().width + 3),
            loadPairsWithType: row('↻ Load', '⌨ type') && leftOf('↻ Load', '⌨ type'),
          };
        """)
        check("PgUp / PgDn sit either side of ↑, in the arrow cluster",
              p["pagingWithArrows"] and p["pgUpLeftOfUp"] and p["pgDnRightOfUp"], str(p))
        check("...with ← ↓ → on the row below them", p["arrowsUnderPaging"] is True, str(p))
        check("...and home/end right under the cluster, the same family",
              p["homePair"] is True, str(p))
        check("...then ⤒ / ⤓ under those", p["endsUnderHome"] is True, str(p))
        check("...⏎ beside Esc, ⌘C beside ⌘V",
              p["editPair"] is True and p["copyPair"] is True, str(p))
        check("all win / app win moved to the system group, where they belong",
              p["winWithSystem"] is True, str(p))
        check("an odd key out spans the row (no lone button with a hole beside it)",
              p["wideKeys"] == ["Wake"] and p["wakeWide"] is True, str(p["wideKeys"]))
        check("...and Load sits beside ⌨ type at the top, a pair like the rest",
              p["loadPairsWithType"] is True, str(p))
        print("=== the keys are the right-hand column in every state ===")
        # They used to move there only when the window was wider than the shot's aspect
        # × 1.1. A 1000×900 window fails that and got the horizontal strip back — six
        # groups in a ragged row across the top, which is what prompted "错了".
        always = drv.js("""
          const img = document.getElementById('screen');
          const fit = document.getElementById('fitChk');
          const out = {};
          // before any screenshot has arrived
          img.removeAttribute('src'); applyFit();
          out.beforeShot = document.body.classList.contains('side');
          return new Promise(res => {
            img.onload = () => {
              fit.checked = true; applyFit();
              out.loaded = document.body.classList.contains('side');
              fit.checked = false; applyFit();              // 1:1 pixels
              out.fitOff = document.body.classList.contains('side');
              fit.checked = true; applyFit();
              const k = document.getElementById('keybar').getBoundingClientRect();
              const r = img.getBoundingClientRect();
              const main = img.closest('main');
              const mr = main.getBoundingClientRect();
              // Against the stage COLUMN, not the image rect: the image can be laid out
              // wider than the column and clipped/panned inside it, so its rect says
              // nothing about where the keys are.
              out.right = Math.round(k.left) >= Math.round(mr.right) - 1;
              // 上下顶齐 on the desktop too: min(width, height) left a band of empty
              // stage above and below once the keys took a column.
              // clientHeight, not the rect: a horizontal scrollbar (this browser gives
              // them space) takes a dozen px, and the shot is re-fitted to what is left.
              out.flush = Math.abs(main.clientHeight - Math.round(r.height)) <= 2;
              out.keysAtWindowEdge = window.innerWidth - Math.round(k.right) < 20;
              out.loadKeys = [...document.querySelectorAll('.js-load')].length;
              out.win = [window.innerWidth, window.innerHeight];
              res(out);
            };
            img.src = 'data:image/svg+xml;utf8,' + encodeURIComponent(
              '<svg xmlns="http://www.w3.org/2000/svg" width="1470" height="956"><rect width="1470" height="956" fill="#222"/></svg>');
          });
        """)
        check("a window barely wider than tall still puts them on the right",
              always["loaded"] is True and always["right"] is True, str(always))
        check("...before the first screenshot has even arrived",
              always["beforeShot"] is True, str(always))
        check("...and with fit off, at 1:1 pixels", always["fitOff"] is True, str(always))
        # Nothing to reclaim in a desktop window, so no button and no way to reach the
        # collapsed state.
        check("...and the phone's collapse toggle is not on the desktop at all",
              drv.js("return getComputedStyle(document.getElementById('keysToggle')).display") == "none")
        check("...flush to the window's right edge, the shot flush top to bottom",
              always["flush"] is True and always["keysAtWindowEdge"] is True, str(always))
        # Refreshing is the thing you do most, and with the keys in a column beside the
        # shot that column is where your hand already is.
        check("Load is in the key column as well as the header, one handler for both",
              always["loadKeys"] == 2
              and src.count("document.querySelectorAll('.js-load')") == 1, str(always))

        print("=== every control in the header is the same box ===")
        # Three shapes in one row read as three unrelated toolbars: the back link was
        # 4/9 padding at 75% opacity, the buttons 5/12, the selects whatever the browser
        # gave them.
        hdr = drv.js("""
          // Visible ones only: the phone's collapse toggle is display:none here by
          // design, and a 0px box would count as a fourth shape.
          const els = [...document.querySelectorAll('header button, header a.backlink, header select')]
                        .filter(e => getComputedStyle(e).display !== 'none');
          const box = (e) => { const c = getComputedStyle(e), r = e.getBoundingClientRect();
            return [Math.round(r.height), c.borderTopWidth, c.borderTopLeftRadius, c.fontSize].join('|'); };
          const shapes = {};
          els.forEach(e => { const b = box(e); shapes[b] = (shapes[b] || 0) + 1; });
          return { n: els.length, shapes,
                   opacities: [...new Set(els.map(e => getComputedStyle(e).opacity))],
                   primaryFilled: getComputedStyle(document.getElementById('btn-load')).backgroundColor
                               !== getComputedStyle(document.querySelector('header a.backlink')).backgroundColor };
        """)
        check("one height / border / radius / font size across the header",
              len(hdr["shapes"]) == 1, f'{hdr["n"]} controls, shapes={hdr["shapes"]}')
        check("...none of them faded out relative to the others",
              hdr["opacities"] == ["1"], str(hdr["opacities"]))
        check("...and only the FILL marks the primary action",
              hdr["primaryFilled"] is True)

        print("=== embedded in cc-web's overlay: the ← must go ===")
        # Inside the iframe that overlay uses, ← would navigate the FRAME to the session
        # list — a session list inside the control window. The overlay's ✕ is the exit
        # there. Driven by loading the page the way the parent loads it.
        drv.go("file://" + PAGE + "?embed=1")
        drv.wait("!!document.querySelector('.backlink')")
        # The DOM effect, not the flag: a page-level `const` is not a window property,
        # so the driver's sandbox cannot see it — and the effect is the behaviour anyway.
        emb = drv.js("""
          const b = document.querySelector('.backlink');
          return { backHidden: getComputedStyle(b).display === 'none',
                   keysStillThere: !!document.getElementById('keybar'),
                   shotStillThere: !!document.getElementById('screen') };
        """)
        check("it hides its own back link when embedded", emb["backHidden"] is True, str(emb))
        check("...while everything else stays",
              emb["keysStillThere"] is True and emb["shotStillThere"] is True, str(emb))
        drv.go("file://" + PAGE)
        drv.wait("!!document.querySelector('.backlink')")
        back = drv.js("""
          return getComputedStyle(document.querySelector('.backlink')).display !== 'none';
        """)
        check("...and opened directly it keeps the link, as before", back is True, str(back))

        print("=== a phone opening the DESKTOP page ===")
        # The desktop page is reachable from a phone (the ⚙ menu links both), and it used
        # to rearrange itself there: the side decision was made on aspect alone, a narrow
        # window has no leftover width, so the keys went back on top and the shot was left
        # a strip. Asked for instead: the same shape, scrolled sideways.
        drv.quit()
        drv = Driver(port, width=420, height=860)
        drv.go("file://" + PAGE)
        drv.wait("!!document.getElementById('keybar')")
        # A shot of the real mac aspect, injected as if it had loaded.
        n = drv.js("""
          const img = document.getElementById('screen');
          return new Promise(res => {
            img.onload = () => {
              applyFit();
              const sw = document.getElementById('stagewrap'), hdr = document.querySelector('header');
              const main = img.closest('main');
              const r = img.getBoundingClientRect(), k = document.getElementById('keybar').getBoundingClientRect();
              res({ narrow: document.body.classList.contains('narrow'),
                    side: document.body.classList.contains('side'),
                    imgH: Math.round(r.height), imgW: Math.round(r.width),
                    stageH: main.clientHeight,
                    keysLeft: Math.round(k.right) <= Math.round(main.getBoundingClientRect().left) + 1,
                    keysAtScrollZero: Math.round(k.left) < 40,
                    keysW: Math.round(k.width),
                    scrolls: main.scrollWidth > main.clientWidth + 8,
                    hdrOneLine: hdr.scrollWidth > hdr.clientWidth + 8
                             && getComputedStyle(hdr).flexWrap === 'nowrap',
                    hdrH: Math.round(hdr.getBoundingClientRect().height) });
            };
            img.src = 'data:image/svg+xml;utf8,' + encodeURIComponent(
              '<svg xmlns="http://www.w3.org/2000/svg" width="1470" height="956"><rect width="1470" height="956" fill="#222"/></svg>');
          });
        """)
        check("a narrow window still gets the side layout", n["narrow"] and n["side"], str(n))
        # 上下顶齐: the shot is bound by the height, and what does not fit is panned.
        check("...the shot fills the height", abs(n["stageH"] - n["imgH"]) <= 2,
              f'img {n["imgW"]}×{n["imgH"]} in a {n["stageH"]}px stage')
        # LEFT here, the opposite side from the desktop and asked for: at scroll 0 the
        # right-hand column was off-screen, so nothing on the page said a keyboard
        # existed — you had to guess that it scrolls sideways.
        check("...the keys are the column on the LEFT, visible without scrolling",
              n["keysLeft"] is True and n["keysAtScrollZero"] is True
              and 150 <= n["keysW"] <= 240,
              f'keys {n["keysW"]}px wide, left={n["keysLeft"]}, x={n["keysAtScrollZero"]}')
        check("...and the shot pans sideways inside the stage, so they never scroll away",
              n["scrolls"] is True, str(n))
        # A four-line wrapped header on a phone costs the shot a quarter of its height.
        check("...with the header one scrollable line, not four wrapped ones",
              n["hdrOneLine"] is True and n["hdrH"] <= 90, f'{n["hdrH"]}px tall')

        print("=== …and the column can be got out of the way ===")
        # 190px of a 390px phone is ~40% of the screen spent on keys you are not pressing
        # while you read the mac. Pressed for real, twice, because the toggle has to work
        # in both directions and the shot has to be re-fitted each time (the stage
        # changes width, and the shot is fitted to the stage).
        t = drv.js("""
          const btn = document.getElementById('keysToggle');
          const img = document.getElementById('screen'), kb = document.getElementById('keybar');
          const main = img.closest('main');
          const vis = (e) => getComputedStyle(e).display !== 'none';
          const shot = () => ({ keys: vis(kb), stage: main.clientWidth,
                                imgH: Math.round(img.getBoundingClientRect().height),
                                label: btn.textContent.trim(),
                                stored: localStorage.getItem('cc_remote_pc_keys') });
          const before = shot();
          btn.click();  const off = shot();
          btn.click();  const on = shot();
          return { toggleVisible: vis(btn), before, off, on };
        """)
        check("the toggle is there on a phone", t["toggleVisible"] is True, str(t))
        check("...one press hides the column and the stage takes its width",
              t["off"]["keys"] is False and t["off"]["stage"] > t["before"]["stage"] + 100,
              f'{t["before"]["stage"]} → {t["off"]["stage"]}px of stage')
        check("...the shot stays flush top to bottom across the change",
              abs(t["off"]["imgH"] - t["before"]["imgH"]) <= 2,
              f'{t["before"]["imgH"]} → {t["off"]["imgH"]}px')
        check("...the arrow says which way it will go next",
              t["before"]["label"] == "⌨ ◀" and t["off"]["label"] == "⌨ ▶", str([t["before"]["label"], t["off"]["label"]]))
        check("...a second press brings it back", t["on"]["keys"] is True
              and t["on"]["stage"] == t["before"]["stage"], str([t["before"]["stage"], t["on"]["stage"]]))
        check("...and the choice is remembered",
              t["off"]["stored"] == "0" and t["on"]["stored"] == "1", str([t["off"]["stored"], t["on"]["stored"]]))
    finally:
        if drv:
            drv.quit()
        gecko.terminate()

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
