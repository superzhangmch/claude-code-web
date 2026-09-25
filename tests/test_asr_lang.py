#!/usr/bin/env python3
"""Which language the recogniser expects — settable, and actually sent.

All three engines take a language, and none of it reached them:

  * Soniox (the per-word realtime one) had `language_hints: ["zh","en"]` hard-coded
    server-side, overridable only by a query param no client sent.
  * OpenAI realtime accepted `?lang=` → transcription.language. Nobody sent it, so it
    detected.
  * the batch /v1/audio/transcriptions call sent model + file + prompt and no language
    at all — detection again, on clips a few seconds long, which is exactly where a
    Chinese sentence comes back as English.

So: one setting in the voice menu, remembered, and passed to whichever engine is in use —
`langs` for Soniox, `lang` for the other two.

The OPTIONS come from the conf (`asr_langs=zh|en|zh+en`, `asr_lang=zh+en` for the
default), not from the code: which languages the person at this machine speaks is not
something the page can know, and hard-coding four buttons made that guess twice. One
option is one or more codes joined by `+`. No asr_langs line at all → no row and no
language sent, which is exactly the old behaviour.

    python3 tests/test_asr_lang.py      # exit 0 = pass
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_fails = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        _fails.append(name)


def main():
    home = tempfile.mkdtemp(prefix="ccweb-lang-")
    os.makedirs(os.path.join(home, ".claude"))
    # A real conf line, because _asr_configs() reads the conf file (re-read per call so
    # it can be edited without a restart) rather than any in-memory dict.
    open(os.path.join(home, ".claude", "cc_web.conf"), "w").write(
        "token=t\nasr=w|http://x|k|whisper-1|W\n"
        "asr_langs=zh|en|zh+en\nasr_lang=zh+en\n")
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    idx = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()

    print("=== the batch engine is told the language ===")
    # Driven for real: the endpoint shells out to curl, so the -F flags it builds are the
    # observable behaviour. Anything less would be asserting on a string in a file.
    import asyncio

    calls = []

    class FakeReq:
        headers = {"content-type": "audio/webm"}

        async def body(self):
            return b"RIFFfake"

    class Done:
        returncode = 0
        stdout = json.dumps({"text": "你好"})
        stderr = ""

    real_run = cc_web.subprocess.run
    cc_web.subprocess.run = lambda cmd, **kw: (calls.append(cmd), Done())[1]
    try:
        if not cc_web._asr_configs():
            print("SKIP: no asr config shape available"); cc_web.subprocess.run = real_run; return 0
        asyncio.run(cc_web.post_asr(FakeReq(), which="w", lang="zh"))
        flags = calls[-1]
        check("`language=zh` goes on the request", "language=zh" in flags, str(flags[-4:]))
        calls.clear()
        asyncio.run(cc_web.post_asr(FakeReq(), which="w", lang="auto"))
        check("...and `auto` sends no language, so the backend detects",
              not any(f.startswith("language=") for f in calls[-1]), str(calls[-1][-4:]))
        calls.clear()
        asyncio.run(cc_web.post_asr(FakeReq(), which="w"))
        check("...as does no setting at all (old clients keep working)",
              not any(f.startswith("language=") for f in calls[-1]), str(calls[-1][-4:]))
    finally:
        cc_web.subprocess.run = real_run

    print("=== Soniox: hints when asked, silence on auto ===")
    # The hints block is built from the query param; `auto` must drop the key entirely,
    # because sending an empty list would still be a list and zh+en is the server default
    # that `auto` exists to escape.
    i = src.index("_lq = (q.get(\"langs\")")
    blk = src[i:i + 900]
    check("?langs=auto parses to an empty list",
          '[] if _lq == "auto"' in blk, blk[:120])
    check("...and the default is still zh+en, the mix actually spoken",
          'q.get("langs") or "zh,en"' in blk)
    conf_i = src.index('conf = {"api_key": sx["key"]')
    conf_blk = src[conf_i:conf_i + 400]
    check("...and the hints key is then left out of the config entirely",
          "if langs:" in conf_blk
          and '"language_hints"' not in conf_blk.split("if langs:")[0]
          and '"language_hints"' in conf_blk.split("if langs:")[1], conf_blk[:200])

    print("=== the options come from the conf ===")
    import asyncio as _aio
    cfg = _aio.run(cc_web.get_asr_configs())
    check("the list is what the conf lists, in that order",
          cfg["langs"] == ["zh", "en", "zh+en"], str(cfg.get("langs")))
    check("...and the configured default is the one selected",
          cfg["lang_default"] == "zh+en", str(cfg.get("lang_default")))
    # A typo in asr_lang must not leave the menu with nothing current.
    conf_path = os.path.join(home, ".claude", "cc_web.conf")
    open(conf_path, "a").write("\n")
    txt = open(conf_path).read().replace("asr_lang=zh+en", "asr_lang=klingon")
    open(conf_path, "w").write(txt)
    cc_web._CONF_CACHE = None if hasattr(cc_web, "_CONF_CACHE") else None
    cfg2 = _aio.run(cc_web.get_asr_configs())
    check("...a default that is not in the list falls back to the first option",
          cfg2["lang_default"] == "zh", str(cfg2.get("lang_default")))
    open(conf_path, "w").write(txt.replace("asr_lang=klingon", "asr_lang=zh+en"))
    # …and with no asr_langs line the row and the parameter both disappear.
    open(conf_path, "w").write("token=t\nasr=w|http://x|k|whisper-1|W\n")
    cfg3 = _aio.run(cc_web.get_asr_configs())
    check("no asr_langs line → nothing offered, nothing sent",
          cfg3["langs"] == [] and cfg3["lang_default"] == "", str(cfg3.get("langs")))

    print("=== the browser sends it, to whichever engine is running ===")
    check("there is one remembered setting", 'localStorage.getItem("cc_asr_lang")' in idx
          and 'localStorage.setItem("cc_asr_lang"' in idx)
    check("...and a remembered choice the conf no longer offers reverts to the default",
          "if (!asrLangs.includes(asrLang)) asrLang = asrLangDefault;" in idx)
    stream = idx[idx.index("openStream() {"):][:1400]
    check("the streaming URL carries BOTH spellings (Soniox langs, OpenAI lang)",
          'qs.set("langs", asrLang.split("+").join(","))' in stream
          and 'qs.set("lang", asrLang.split("+")[0])' in stream, "not in openStream")
    batch = idx[idx.index("async transcribeClip(blob)"):][:900]
    check("the batch POST carries one ISO code — zh for the zh+en option",
          'qs.set("lang", asrLang.split("+")[0])' in batch, "not in transcribeClip")

    print("=== the menu row ===")
    menu = idx[idx.index('const box = line("lang");'):][:1200]
    check("one button per configured option, its label being the option",
          "for (const v of asrLangs)" in menu and "b.textContent = v;" in menu, menu[:140])
    # Not a third row: a `lang` chip in the mode row swaps what the second row lists,
    # reusing the two-level shape that was already there (mode → its models). Rows are
    # what runs out in a menu you operate with a thumb.
    # Tab semantics, checked on the two halves that make it one: a `lang` chip that
    # selects the pane, and mode chips that select it back (without the second half,
    # pressing `⚡ live` left you looking at the language list).
    check("...reached by a chip in the voice row, not by a row of its own",
          'asrPane = "lang"; renderAsrMenu();' in idx
          and 'if (asrPane === "lang" && asrLangs.length) {' in idx
          and 'asrRt = on; asrPane = "model";' in idx)
    check("...and nothing at all when the conf offers none",
          "if (asrLangs.length) {" in idx)
    check("...the current one is marked, like every other row",
          'classList.add("current")' in menu)
    # It IS the second row when chosen — same `line()` helper, same section, so the
    # languages are found where the rest of the voice settings are.
    check("...and it is a `line()` in the voice section, like the model list",
          idx.count('const box = line("lang");') == 1
          and idx.index('line("lang")') < idx.index('const box = line("model");'))

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
