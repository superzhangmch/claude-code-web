#!/usr/bin/env python3
"""Lightweight regression tests for cc_web / iterm_bridge — the paths touched by
the code-review fixes (pid-start cache & verify_binding, screen delta
reconstruction, pending-confirm menu detection, claude-cmd matching, fs
confinement, brief/medium System truncation).

No pytest needed:  python3 tests/test_review_fixes.py   (exit 0 = all pass)
"""
import importlib.util, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# cc_web imports its terminal bridge by name (tmux_bridge on Linux, iterm_bridge on
# macOS). _load() below only pre-registers iterm_bridge, so on Linux the cc_web
# import died with ModuleNotFoundError — the suite simply never ran there.
sys.path.insert(0, ROOT)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

ib = _load("iterm_bridge")
cc = _load("cc_web")

_fails = []
def check(name, cond):
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        _fails.append(name)


# ---- pid-start cache & verify_binding (the time/_time bug lived here) ----
def test_pid_cache():
    pid = os.getpid()
    real = ib._pid_start_time(pid)
    check("pid_start_time returns >0 for a live pid", real > 0)
    check("_pid_start_cached stable & == real", cc._pid_start_cached(pid) == real == cc._pid_start_cached(pid))
    check("alive + correct start -> True", cc._pid_alive_with_start(pid, real) is True)
    check("alive + wrong start -> False (pid-reuse guard)", cc._pid_alive_with_start(pid, real + 999) is False)
    check("dead pid -> False", cc._pid_alive_with_start(999999, 123.0) is False)
    cc._PID_START_CACHE[pid] = (42.0, time.monotonic())     # poison within TTL
    check("TTL: cached value is actually reused", cc._pid_start_cached(pid) == 42.0)
    cc._PID_START_CACHE.pop(pid, None)


# ---- screen delta reconstruction (exactness) ----
def test_screen_delta():
    prev = "\n".join(f"line-{i}" for i in range(20))
    r0 = cc._screen_delta("t/d", prev, "")            # baseline
    check("first delta send is a full", "full" in r0)
    v1 = r0["ver"]
    check("unchanged -> same", cc._screen_delta("t/d", prev, v1).get("same") is True)
    # scroll up by 3 (drop 3 from top, add 3 at bottom); fresh baseline then diff
    cur = "\n".join([f"line-{i}" for i in range(3, 20)] + ["new-a", "new-b", "new-c"])
    cc._SCREEN_DELTA_CACHE.pop("t/d", None)
    base = cc._screen_delta("t/d", prev, "")["ver"]
    d = cc._screen_delta("t/d", cur, base)
    check("scrolled frame -> delta (not full)", "changed" in d and "full" not in d)
    # reconstruct like the client does
    pl = prev.split("\n"); k = d["scroll"]; n = d["n"]
    lines = [(pl[i + k] if 0 <= i + k < len(pl) else "") for i in range(n)]
    for i, v in d["changed"]:
        if 0 <= i < n:
            lines[i] = v if isinstance(v, str) else v["c"] * v["n"]
    check("delta reconstruction is EXACT", "\n".join(lines) == cur)
    # unrelated frame -> falls back to full
    other = "\n".join(f"zzz-{i}" for i in range(20))
    cc._SCREEN_DELTA_CACHE.pop("t/d", None)
    b2 = cc._screen_delta("t/d", prev, "")["ver"]
    check("no matching run -> full send", "full" in cc._screen_delta("t/d", other, b2))


# ---- pending-confirm menu detection ----
def test_pending_confirm():
    menu = "Do you want to proceed?\n❯ 1. Yes, allow\n  2. No, deny\n"
    r = cc._detect_pending_confirm_from_screen(menu)
    check("menu detected", isinstance(r, dict) and len(r.get("choices", [])) == 2)
    check("menu question captured", r and r.get("question", "").endswith("?"))
    prose = "Here is the plan:\n1. First we scan\n2. Then we build\n3. Then ship\n"
    check("plain prose numbered list is NOT a menu", cc._detect_pending_confirm_from_screen(prose) is None)
    check("empty screen -> None", cc._detect_pending_confirm_from_screen("") is None)


# ---- claude-cmd matching + tty normalization ----
def test_bridge_helpers():
    for cmd, exp in [("claude", True), ("claude --resume abc", True),
                     ("node /opt/homebrew/bin/claude --x", True),
                     ("tail -f /var/log/claude", False), ("cd /work/claude", False),
                     ("vim ~/claude", False), ("python x.py --out claude", False)]:
        check(f"_is_claude_cmd({cmd!r})=={exp}", ib._is_claude_cmd(cmd) is exp)
    check("_norm_tty /dev/ttys005", ib._norm_tty("/dev/ttys005") == "s005")
    check("_norm_tty ttys005", ib._norm_tty("ttys005") == "s005")
    check("_norm_tty s005", ib._norm_tty("s005") == "s005")


# ---- fs confinement ----
def test_fs_allowed():
    from pathlib import Path
    home = Path.home().resolve()
    check("home itself allowed", cc._fs_allowed(home))
    check("under home allowed", cc._fs_allowed(home / "Desktop"))
    check("/etc/passwd blocked", not cc._fs_allowed(Path("/etc/passwd")))
    check("/ blocked", not cc._fs_allowed(Path("/")))


# ---- brief/medium System-message truncation ----
def test_filter_entries():
    watcher = "WATCHER tick (handle autonomously, do NOT wait for a reply). then reschedule. " + "Z" * 600
    entries = [
        {"type": "user", "isMeta": True, "promptSource": "system", "sessionId": "s", "_idx": 1,
         "message": {"role": "user", "content": watcher}},
        {"type": "user", "origin": "web", "sessionId": "s", "_idx": 2,
         "message": {"role": "user", "content": "真实用户消息" + "x" * 400}},
        {"type": "user", "toolUseResult": {"x": 1}, "sessionId": "s", "_idx": 3,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "R" * 300}]}},
    ]
    def text_of(r):
        c = r["message"]["content"]
        if isinstance(c, str): return c
        return " ".join((b.get("text") or b.get("content") or "") for b in c if isinstance(b, dict))
    for mode in ("brief", "medium"):
        out = {r["_idx"]: r for r in cc._filter_entries(entries, mode)}
        check(f"[{mode}] watcher System truncated", 1 in out and out[1].get("_system") and len(text_of(out[1])) < 300)
        check(f"[{mode}] real user msg NOT truncated", 2 in out and not out[2].get("_system") and len(text_of(out[2])) > 300)
    med = {r["_idx"]: r for r in cc._filter_entries(entries, "medium")}
    check("[medium] tool_result kept (not collapsed away)", 3 in med and "R" in text_of(med[3]))


def test_cpu_view_cross_platform():
    """The CPU/memory view was macOS-only by accident, in three stacked ways: the battery
    was read with `pmset`, the ONLY way in was tapping that battery, and the sampler's
    `ps` flags were macOS-specific. So a Linux laptop showed no battery, had no button,
    and would have drawn an empty chart even if it had one."""
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    check("battery has a Linux reader, not only pmset",
          "_read_battery_linux" in src and "power_supply" in src)
    check("...picked by platform rather than assumed",
          'sys.platform == "darwin" else _read_battery_linux' in src)
    # `-r` means "sort by CPU" on macOS; Linux ps rejects the combination outright and
    # returned zero rows, which is why the chart was empty rather than broken-looking.
    sampler = src.split("_sample_top_cpu_processes")[1][:1200]
    check("the sampler branches instead of using macOS-only ps flags everywhere",
          "--sort=-pcpu" in sampler and "-Arwwo" in sampler and 'sys.platform == "darwin"' in sampler)
    check("the system-process uid floor is per-platform (macOS 501+, Linux 1000+)",
          "floor = 500 if sys.platform" in src and "else 1000" in src)

    # And it actually produces rows on the machine running this suite.
    top = cc._sample_top_cpu_processes(3)
    check("sampling returns rows here", len(top) > 0)
    check("...with every column the chart reads",
          all({"pid", "uid", "cpu", "command", "is_system"} <= set(t) for t in top))
    b = cc._get_battery()
    check("battery reads as a dict or a clean None (never a crash)",
          b is None or {"pct", "state", "on_ac", "charging"} <= set(b))

    # A cloud VM and a desktop have no battery at all, so the way in must not be the
    # battery. Both triggers go through one function.
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    check("there is an entry point that is not the battery", 'id="picker-cpu"' in html)
    check("...and both triggers open the same modal", html.count("openCpuModal") >= 3)
    # No battery must not mean no way in: the slot shows a chip instead. Hiding it was
    # the original bug, so a test that only checked "renders when b exists" would miss it.
    r = html.split("function renderBattery")[1][:900]
    check("with no battery the slot stays visible and shows a chip",
          'cpu-ico' in r and 'batteryEl.style.display = ""' in r.split("return;")[0])
    check("...and the CSS draws that chip rather than setting a glyph",
          "#battery .cpu-ico {" in html and "border: 1px solid currentColor" in html)


if __name__ == "__main__":
    for t in (test_pid_cache, test_screen_delta, test_pending_confirm,
              test_bridge_helpers, test_fs_allowed, test_filter_entries,
              test_cpu_view_cross_platform):
        print(f"\n== {t.__name__} ==")
        t()
    print(f"\n{'ALL PASS' if not _fails else 'FAILURES: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
