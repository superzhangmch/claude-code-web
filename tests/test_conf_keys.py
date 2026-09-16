#!/usr/bin/env python3
"""Every key the config documents must actually reach the server.

_load_conf keeps a key only if it already has an entry in its defaults dict:

    elif k in cfg:
        cfg[k] = v

So a key can be written in config.example, read back with cfg.get(...) at the point of
use, and be dropped in between — with no error anywhere, because cfg.get() returns None
and every call site has a sensible fallback. The setting simply does nothing.

That is not hypothetical. Found on 2026-09-05, both live on four hosts:

  * all six `whip_*` keys — so the tuning numbers ran on their hardcoded defaults no
    matter what the file said, and `whip_triage_model` fell back to `model`, meaning the
    cheap tier of a deliberately two-tier design was paying for the expensive model on
    every pass. Noticed only because a debug line printed the wrong model name.
  * `snapshot_every_min` — documented as "set to 0 to turn the timer off". Setting it to
    0 did not turn the timer off.

Neither was caught by a test that looked for the key's NAME in cc_web.py: the name was
there, at the call site, doing nothing. This checks the round trip instead — write a
conf, load it, see the value — and does it for the whole documented surface, so the next
key someone adds cannot join them quietly.

    python3 tests/test_conf_keys.py      # exit 0 = pass
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Parsed by their own `elif` branch into a different shape (list / dict), so they are
# absent from the defaults dict by design.
STRUCTURED = {"cwd", "asr", "openai_realtime", "soniox"}
# Documented in the same file on purpose but explicitly NOT the server's: the bundled
# skills read cc_web.conf too, and config.example says so where these appear.
SKILL_ONLY = {"hosts"}

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-conf-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    doc_path = os.path.join(ROOT, "config.example", "cc_web.conf")
    doc = open(doc_path, encoding="utf-8").read()
    # Both the live lines and the commented-out examples: a key is documented either way,
    # and the commented ones are exactly the settings people uncomment and expect to work.
    keys = sorted({m.group(1) for m in re.finditer(r"(?m)^#?([a-z_][a-z0-9_]*)=", doc)})
    check("the documented surface is not empty (the regex still matches)", len(keys) > 8, str(len(keys)))

    print("=== every documented key is known to the parser ===")
    cfg = cc_web._load_conf()
    for k in keys:
        if k in STRUCTURED or k in SKILL_ONLY:
            continue
        check(f"{k} survives parsing", k in cfg,
              "" if k in cfg else "documented but dropped — add it to _load_conf's defaults")

    print("=== ...and its value makes the round trip, not just its name ===")
    # A scalar sample per key. The point is not the value but that what comes out is what
    # went in: the two bugs this file exists for both passed a name-only check.
    probe = {"token": "tok", "api_base": "https://x/", "api_key": "k", "model": "m1",
             "claude_config": "/tmp/x.json", "icon": "tp", "name": "x13",
             "snapshot_every_min": "0",
             "whip_quiet_seconds": "42", "whip_max_nudges": "9",
             "whip_backoff_seconds": "11,22,33", "whip_interval_seconds": "7",
             "whip_triage_model": "cheap"}
    conf = os.path.join(home, ".claude", "cc_web.conf")
    with open(conf, "w", encoding="utf-8") as fh:
        for k, v in probe.items():
            fh.write(f"{k}={v}\n")
    cfg = cc_web._load_conf()
    for k, v in probe.items():
        if k == "token":
            continue        # read once at startup, not from this dict
        check(f"{k} = {v}", str(cfg.get(k)) == v, repr(cfg.get(k)))
    # The one with a documented behaviour that a dropped key silently reverses.
    check("snapshot_every_min=0 can actually turn the timer off",
          float(cfg.get("snapshot_every_min") or 30) == 0.0,
          str(cfg.get("snapshot_every_min")))
    # Anything undocumented is still ignored — the parser must not become a place where
    # typos land silently in a config people edit by hand.
    with open(conf, "a", encoding="utf-8") as fh:
        fh.write("whip_quiet_second=99\n")          # a plausible typo
    check("an unknown key is ignored rather than half-applied",
          "whip_quiet_second" not in cc_web._load_conf())

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
