#!/usr/bin/env python3
"""A turn that is thinking + tool_use, with no text block, must not come back empty.

Reported as "为啥忽然丢消息了", with the terminal next to the browser: claude had said
"放心，PR2 合并后我没再动它…" on screen, and that round in the web was a single Bash bar
with nothing above it. Its own store says why — the whole turn was:

    assistant  thinking  '放心，PR2 合并后我没再动它…'      ← the ● in the terminal
    assistant  tool_use  Bash

no text block anywhere. brief mode dropped `thinking` on purpose (it was thinking out
loud, next to a text block that carried the answer), so when the model stopped writing
that text block, brief dropped the answer itself. Measured on the live session:

    mode=brief   {'text': 5, 'tool_use': 12}                      the sentence: absent
    mode=medium  {... 'thinking': 7}                              the sentence: present

The rule is therefore conditional, not "keep thinking": keep it when the turn has no
text of its own, stay quiet when it does.

    python3 tests/test_thinking_only_turn.py      # exit 0 = pass
"""
import os
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
    home = tempfile.mkdtemp(prefix="ccweb-think-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0

    SAID = "放心，PR2 合并后我没再动它，PR3 是基于最新 main 新开的独立分支和工作树在改。"

    def entry(content, typ="assistant"):
        return {"uuid": "u1", "type": typ, "_idx": 1, "_round": 19,
                "timestamp": "2026-10-15T20:50:00Z", "sessionId": "s",
                "message": {"content": content}}

    def kinds(trimmed):
        return [b.get("type") for b in ((trimmed or {}).get("message") or {}).get("content", [])]

    def texts(trimmed, t):
        return [b.get(t) for b in ((trimmed or {}).get("message") or {}).get("content", [])
                if b.get("type") == t]

    print("=== the turn that went missing ===")
    turn = entry([{"type": "thinking", "thinking": SAID},
                  {"type": "tool_use", "id": "a", "name": "Bash",
                   "input": {"command": "ls", "description": "Update the 3 en strings"}}])
    br = cc_web._trim_brief(turn)
    check("brief keeps the thinking when the turn has no text",
          kinds(br) == ["thinking", "tool_use"], str(kinds(br)))
    check("...with the sentence intact, not a summary of it",
          texts(br, "thinking") == [SAID], str(texts(br, "thinking"))[:80])
    check("...and the tool call still comes with it",
          any(b.get("desc") == "Update the 3 en strings"
              for b in br["message"]["content"] if b.get("type") == "tool_use"),
          str(br["message"]["content"][-1]))

    print("=== and when there IS text, thinking stays out of brief ===")
    # The old shape, and the reason brief dropped thinking in the first place: the answer
    # is in the text block, and the thinking beside it is working-out nobody asked for.
    turn = entry([{"type": "thinking", "thinking": "let me check the branch first"},
                  {"type": "text", "text": "PR2 没动过。"},
                  {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "git log"}}])
    br = cc_web._trim_brief(turn)
    check("a turn with a text block shows the text and not the thinking",
          kinds(br) == ["text", "tool_use"], str(kinds(br)))
    # Whitespace-only text is not text: an empty block must not suppress the real answer.
    turn = entry([{"type": "thinking", "thinking": SAID},
                  {"type": "text", "text": "   \n"},
                  {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}])
    check("...and an empty text block does not count as having said something",
          kinds(cc_web._trim_brief(turn)) == ["thinking", "tool_use"],
          str(kinds(cc_web._trim_brief(turn))))
    # Several thinking blocks, which is the normal shape (the first is often empty).
    turn = entry([{"type": "thinking", "thinking": ""},
                  {"type": "thinking", "thinking": SAID},
                  {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}])
    check("an empty thinking block is skipped, the real one is kept",
          texts(cc_web._trim_brief(turn), "thinking") == [SAID])

    print("=== medium/full were never the problem, and must not change ===")
    turn = entry([{"type": "thinking", "thinking": SAID},
                  {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}])
    md = cc_web._trim_medium(turn) if hasattr(cc_web, "_trim_medium") else None
    if md is None:
        print("  --    (no _trim_medium to check; brief is where the filter lives)")
    else:
        check("medium still carries the thinking", "thinking" in kinds(md), str(kinds(md)))

    print("=== 'what did it last say' answers with it too ===")
    # The same question is asked by the list row, by /api/live's anchor and by the reply
    # a peer reads back — all through _entry_text, which also only looked at text blocks.
    check("_entry_text falls back to thinking when there is no text",
          cc_web._entry_text(entry([{"type": "thinking", "thinking": SAID}])) == SAID,
          cc_web._entry_text(entry([{"type": "thinking", "thinking": SAID}]))[:40])
    check("...but prefers the text block when there is one",
          cc_web._entry_text(entry([{"type": "thinking", "thinking": "working it out"},
                                    {"type": "text", "text": "PR2 没动过。"}])) == "PR2 没动过。")
    check("...and a tool result is still not 'something it said'",
          cc_web._entry_text(entry([{"type": "tool_result", "tool_use_id": "a",
                                     "content": "ok"}], typ="user")) == "")

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
