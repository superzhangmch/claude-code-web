"""Phone-as-remote control for the host Mac.

Mount from cc_web.py:

    from remote_mac import api_router as remote_api
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path

    app.include_router(remote_api, prefix="/remote/api",
                       dependencies=[Depends(require_token)])
    app.mount("/remote",
              StaticFiles(directory=Path(__file__).parent / "remote_mac_static",
                          html=True),
              name="remote_mac_static")

The HTML lives in `remote_mac_static/`; the API lives at `/remote/api/*` and
is gated by cc_web's existing Bearer token auth.

Only the endpoints actually used by the current phone UI are kept here.
OCR / B&W edge / Dock-listing endpoints from earlier prototypes are gone.
"""
from __future__ import annotations

import subprocess
import threading
import time as _time
from pathlib import Path
from typing import Optional

import Quartz
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

# ── Paths & shared state ───────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "remote_mac_ctrl_static"
CACHE_DIR = STATIC_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Serialize screencapture/sips runs so concurrent requests don't fight.
_capture_lock = threading.Lock()


# ── Key codes & modifier names for AppleScript System Events ───────────────

KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "delete": 51, "backspace": 51, "forwarddelete": 117,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
MODIFIERS = {
    "cmd": "command down", "command": "command down",
    "ctrl": "control down", "control": "control down",
    "opt": "option down", "option": "option down", "alt": "option down",
    "shift": "shift down",
}


# ── macOS primitives ───────────────────────────────────────────────────────

def run_applescript(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "applescript failed")
    return r.stdout.strip()


def resolve_display(target: str = "main") -> tuple[int, float, float, float, float]:
    """Map a phone-facing target name to a concrete display.

    target = 'main' | 'external' | numeric CGDirectDisplayID
    Returns (display_id, origin_x, origin_y, width_pt, height_pt). 'external'
    falls back to main if no second display is connected — keeps the radio
    in the phone UI usable even when nothing is plugged in."""
    main_id = Quartz.CGMainDisplayID()
    did = main_id
    if target == "external":
        err, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
        for d in ids or ():
            if d != main_id:
                did = d
                break
    elif target and target != "main":
        try:
            did = int(target)
        except (TypeError, ValueError):
            did = main_id
    b = Quartz.CGDisplayBounds(did)
    return did, float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height)


def logical_screen_size(target: str = "main") -> tuple[int, int]:
    _, _, _, w, h = resolve_display(target)
    return int(w), int(h)


def capture_screenshot(max_dim: int = 1280, quality: int = 50,
                       display_target: str = "main") -> bytes:
    """Capture a specific display (default main), downscale via sips, return JPEG.
    Display must already be awake (use /run wake first if needed)."""
    did, *_ = resolve_display(display_target)
    # screencapture -D wants a 1-based INDEX into the active display list, not
    # a CGDirectDisplayID. Compute it from the same list resolve_display uses
    # so the two stay in sync.
    err, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
    sc_index = next((i + 1 for i, d in enumerate(ids or ()) if d == did), 1)
    raw = CACHE_DIR / "raw.jpg"
    small = CACHE_DIR / "small.jpg"
    with _capture_lock:
        # -D <n> picks a specific display by 1-based ordinal.
        # -x silent, -C include cursor.
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-D", str(sc_index), "-C",
             "-t", "jpg", str(raw)],
            check=True, timeout=5, capture_output=True,
        )
        subprocess.run(
            ["/usr/bin/sips", "-Z", str(max_dim),
             "-s", "format", "jpeg",
             "-s", "formatOptions", str(quality),
             str(raw), "--out", str(small)],
            check=True, timeout=5, capture_output=True,
        )
        return small.read_bytes()


def click_at(x: float, y: float, button: str = "left", double: bool = False) -> None:
    if button == "right":
        down, up, btn = (Quartz.kCGEventRightMouseDown,
                         Quartz.kCGEventRightMouseUp,
                         Quartz.kCGMouseButtonRight)
    else:
        down, up, btn = (Quartz.kCGEventLeftMouseDown,
                         Quartz.kCGEventLeftMouseUp,
                         Quartz.kCGMouseButtonLeft)
    pos = (float(x), float(y))
    e_down = Quartz.CGEventCreateMouseEvent(None, down, pos, btn)
    if double:
        Quartz.CGEventSetIntegerValueField(e_down, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_down)
    e_up = Quartz.CGEventCreateMouseEvent(None, up, pos, btn)
    if double:
        Quartz.CGEventSetIntegerValueField(e_up, Quartz.kCGMouseEventClickState, 2)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_up)


def drag_from_to(x1: float, y1: float, x2: float, y2: float,
                 steps: int = 12) -> None:
    """Left-button drag from (x1,y1) to (x2,y2).

    Sends intermediate `LeftMouseDragged` frames — many receivers (Finder
    item drag, Sketch shape drag, lasso-select) only register as a drag
    when they see motion deltas, not just down→up at a different location.
    Coords are global event-space (caller adds display origin)."""
    pos1 = (float(x1), float(y1))
    pos2 = (float(x2), float(y2))
    btn = Quartz.kCGMouseButtonLeft
    e_down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, pos1, btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_down)
    _time.sleep(0.05)
    steps = max(2, steps)
    for i in range(1, steps + 1):
        t = i / steps
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        e_mid = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, (float(x), float(y)), btn)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_mid)
        _time.sleep(0.012)
    e_up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, pos2, btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_up)


def scroll_by(dy: int, dx: int = 0) -> None:
    e = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def quartz_post_keycode(keycode: int) -> None:
    e_d = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_d)
    e_u = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e_u)


def quartz_type_unicode(text: str, per_char_delay: float = 0.008) -> None:
    """HID-level keystroke injection. Works through the lock screen if
    Input-Monitoring/Accessibility is granted to the host process."""
    for ch in text:
        ed = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(ed, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ed)
        eu = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventKeyboardSetUnicodeString(eu, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, eu)
        if per_char_delay > 0:
            _time.sleep(per_char_delay)


def wake_display() -> None:
    subprocess.Popen(["/usr/bin/caffeinate", "-u", "-t", "1"])


def act_wake() -> str:
    wake_display()
    return "woken"


def act_show_dock() -> str:
    """Park the cursor at the bottom-center to reveal an auto-hidden Dock."""
    sw, sh = logical_screen_size()
    e = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (sw / 2, sh - 1),
        Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    _time.sleep(0.4)
    return "ok"


def lock_screen() -> None:
    """Lock screen via the Ctrl+Cmd+Q shortcut, routed through AppleScript
    System Events so modifier press/release is handled cleanly.

    The earlier all-Quartz version (CGEventSetFlags on the Q event) left
    the system in a state where it thought Ctrl+Cmd were still being held
    when the password chars were posted next — so the password got eaten
    as if it were a stream of cmd-shortcuts. AppleScript releases the
    modifiers automatically when the keystroke finishes.

    No-op when already at the lock screen (System Events can't reach
    loginwindow). Subsequent quartz_type_unicode runs at HID level which
    does reach the lock screen."""
    run_applescript(
        'tell application "System Events" to keystroke "q" '
        'using {control down, command down}'
    )


def act_unlock(password: str, wake: bool = True, lock_first: bool = True) -> None:
    """Phone-driven unlock. With `lock_first` (default), trigger Ctrl+Cmd+Q
    first so the typed password always lands on the lock screen rather than
    in whatever app happened to have focus. Cheap insurance — Ctrl+Cmd+Q on
    an already-locked Mac is a no-op."""
    if lock_first:
        lock_screen()
        _time.sleep(1.5)   # let lock screen UI come up + animation settle
    if wake:
        wake_display()
        _time.sleep(0.6)
    quartz_type_unicode(password)
    _time.sleep(0.08)
    quartz_post_keycode(36)   # Return


def act_key(target: str) -> None:
    """Send a keyboard shortcut like 'cmd+t' or 'ctrl+cmd+q' via System Events."""
    parts = [p.strip().lower() for p in target.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key spec")
    key, mods = parts[-1], parts[:-1]
    mod_clause = ""
    if mods:
        mod_clause = " using {" + ", ".join(MODIFIERS[m] for m in mods) + "}"
    if key in KEY_CODES:
        run_applescript(
            f'tell application "System Events" to key code {KEY_CODES[key]}{mod_clause}'
        )
    elif len(key) == 1:
        run_applescript(
            f'tell application "System Events" to keystroke "{key}"{mod_clause}'
        )
    else:
        raise ValueError(f"unknown key: {key}")


DISPATCH = {
    "wake": lambda _t: act_wake(),
    "show_dock": lambda _t: act_show_dock(),
}


# ── Request bodies ─────────────────────────────────────────────────────────

class RunBody(BaseModel):
    type: str
    target: str = ""

class TypeBody(BaseModel):
    text: str

class KeyBody(BaseModel):
    key: str

class ClickBody(BaseModel):
    xf: Optional[float] = None
    yf: Optional[float] = None
    x: Optional[float] = None
    y: Optional[float] = None
    button: str = "left"
    double: bool = False
    display: str = "main"

class ScrollBody(BaseModel):
    dx: int = 0
    dy: int = 0

class DragBody(BaseModel):
    x1f: Optional[float] = None
    y1f: Optional[float] = None
    x2f: Optional[float] = None
    y2f: Optional[float] = None
    display: str = "main"

class UnlockBody(BaseModel):
    password: str
    wake: bool = True
    lock_first: bool = True


# ── API router ─────────────────────────────────────────────────────────────

api_router = APIRouter()


@api_router.get("/screenshot")
def screenshot(q: int = 50, w: int = 1280, display: str = "main"):
    q = max(10, min(95, q))
    w = max(320, min(2560, w))
    try:
        jpeg = capture_screenshot(max_dim=w, quality=q, display_target=display)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"capture failed: {e}")
    did, ox, oy, dw, dh = resolve_display(display)
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "X-Screen-W": str(int(dw)),
            "X-Screen-H": str(int(dh)),
            "X-Display-Id": str(did),
            "X-Display-Origin-X": str(int(ox)),
            "X-Display-Origin-Y": str(int(oy)),
            "Cache-Control": "no-store",
        },
    )


@api_router.get("/cursor_strip")
def cursor_strip(
    w: int = 700,
    h: int = 110,
    q: int = 50,
    cx: Optional[float] = None,
    cy: Optional[float] = None,
    ax: float = 0.5,
    ay: float = 0.5,
    display: str = "main",
):
    """Capture a thin horizontal strip around a point.

    Caller passes the focus point as `cx`, `cy` in display points — typically
    the click-marker position from the phone UI (where the user just pointed
    on the screenshot). Falls back to the actual mouse pointer position if
    no point is supplied. Mac's system mouse only moves on an explicit Click,
    so reading the system cursor here would land in the WRONG place whenever
    the user moved the marker via cursor-pad but didn't Click before typing.

    `ax` / `ay` control where the focus point sits inside the strip, as a
    fraction (0 = left/top edge, 1 = right/bottom edge). Defaults to 0.5,
    0.5 — focus point at the center. Pass e.g. ax=0.75 to bias the strip
    toward showing text to the left of the cursor (typed-text history)."""
    w = max(60, min(2000, w))
    h = max(20, min(800, h))
    q = max(10, min(95, q))
    ax = max(0.0, min(1.0, ax))
    ay = max(0.0, min(1.0, ay))
    try:
        _did, ox, oy, _dw, _dh = resolve_display(display)
        if cx is None or cy is None:
            loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            # CGEventGetLocation is already in global coords.
            gx, gy = float(loc.x), float(loc.y)
        else:
            # Caller-supplied cx/cy are display-local (matches /screenshot's
            # X-Screen-W/H frame); add display origin to reach global coords.
            gx, gy = float(cx) + ox, float(cy) + oy
        x = int(gx - w * ax)
        y = int(gy - h * ay)
        out = CACHE_DIR / "strip.jpg"
        with _capture_lock:
            subprocess.run(
                ["/usr/sbin/screencapture", "-x",
                 "-R", f"{x},{y},{w},{h}",
                 "-t", "jpg", str(out)],
                check=True, timeout=5, capture_output=True,
            )
            # Downscale to roughly 1 px/pt + re-encode at target quality.
            # Same idea as the main /screenshot — keeps bytes small.
            subprocess.run(
                ["/usr/bin/sips", "-Z", str(w),
                 "-s", "format", "jpeg",
                 "-s", "formatOptions", str(q),
                 str(out), "--out", str(out)],
                check=True, timeout=5, capture_output=True,
            )
        return Response(
            content=out.read_bytes(),
            media_type="image/jpeg",
            headers={
                "X-Cursor-X": str(int(gx)),
                "X-Cursor-Y": str(int(gy)),
                "Cache-Control": "no-store",
            },
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"capture failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.post("/run")
def run_action(body: RunBody):
    fn = DISPATCH.get(body.type)
    if not fn:
        raise HTTPException(status_code=400, detail=f"unknown action type: {body.type}")
    try:
        result = fn(body.target) or "ok"
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "result": result}


@api_router.post("/type")
def type_text(body: TypeBody):
    quartz_type_unicode(body.text)
    return {"ok": True}


@api_router.post("/key")
def send_key(body: KeyBody):
    try:
        act_key(body.key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@api_router.post("/click")
def do_click(body: ClickBody):
    did, ox, oy, dw, dh = resolve_display(body.display)
    if body.xf is not None and body.yf is not None:
        # Fractions are relative to the selected display's bounds; add the
        # display's origin to land at the right place in global event coords
        # (matters when external monitor sits at negative x/y from main).
        x, y = body.xf * dw + ox, body.yf * dh + oy
    elif body.x is not None and body.y is not None:
        # Treat x/y as display-local for consistency with the fraction path.
        x, y = body.x + ox, body.y + oy
    else:
        raise HTTPException(status_code=400, detail="missing xf/yf or x/y")
    click_at(x, y, button=body.button, double=body.double)
    return {"ok": True, "x": x, "y": y}


@api_router.post("/drag")
def do_drag(body: DragBody):
    if any(v is None for v in (body.x1f, body.y1f, body.x2f, body.y2f)):
        raise HTTPException(status_code=400, detail="missing x1f/y1f/x2f/y2f")
    did, ox, oy, dw, dh = resolve_display(body.display)
    x1 = body.x1f * dw + ox
    y1 = body.y1f * dh + oy
    x2 = body.x2f * dw + ox
    y2 = body.y2f * dh + oy
    drag_from_to(x1, y1, x2, y2)
    return {"ok": True, "from": [x1, y1], "to": [x2, y2]}


@api_router.post("/scroll")
def do_scroll(body: ScrollBody):
    scroll_by(body.dy, body.dx)
    return {"ok": True}


@api_router.post("/unlock")
def do_unlock(body: UnlockBody):
    if not body.password:
        raise HTTPException(status_code=400, detail="missing password")
    act_unlock(body.password, wake=body.wake, lock_first=body.lock_first)
    return {"ok": True}
