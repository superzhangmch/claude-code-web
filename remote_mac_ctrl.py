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


def logical_screen_size() -> tuple[int, int]:
    main_id = Quartz.CGMainDisplayID()
    bounds = Quartz.CGDisplayBounds(main_id)
    return int(bounds.size.width), int(bounds.size.height)


def capture_screenshot(max_dim: int = 1280, quality: int = 50) -> bytes:
    """Capture main display, downscale via sips, return JPEG bytes.
    Display must already be awake (use /run wake first if needed)."""
    raw = CACHE_DIR / "raw.jpg"
    small = CACHE_DIR / "small.jpg"
    with _capture_lock:
        # -m main only, -x silent, -C include cursor
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-m", "-C", "-t", "jpg", str(raw)],
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


def act_unlock(password: str, wake: bool = True) -> None:
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

class ScrollBody(BaseModel):
    dx: int = 0
    dy: int = 0

class UnlockBody(BaseModel):
    password: str
    wake: bool = True


# ── API router ─────────────────────────────────────────────────────────────

api_router = APIRouter()


@api_router.get("/screenshot")
def screenshot(q: int = 50, w: int = 1280):
    q = max(10, min(95, q))
    w = max(320, min(2560, w))
    try:
        jpeg = capture_screenshot(max_dim=w, quality=q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"capture failed: {e}")
    sw, sh = logical_screen_size()
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "X-Screen-W": str(sw),
            "X-Screen-H": str(sh),
            "Cache-Control": "no-store",
        },
    )


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
    sw, sh = logical_screen_size()
    if body.xf is not None and body.yf is not None:
        x, y = body.xf * sw, body.yf * sh
    elif body.x is not None and body.y is not None:
        x, y = body.x, body.y
    else:
        raise HTTPException(status_code=400, detail="missing xf/yf or x/y")
    click_at(x, y, button=body.button, double=body.double)
    return {"ok": True, "x": x, "y": y}


@api_router.post("/scroll")
def do_scroll(body: ScrollBody):
    scroll_by(body.dy, body.dx)
    return {"ok": True}


@api_router.post("/unlock")
def do_unlock(body: UnlockBody):
    if not body.password:
        raise HTTPException(status_code=400, detail="missing password")
    act_unlock(body.password, wake=body.wake)
    return {"ok": True}
