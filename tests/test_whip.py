#!/usr/bin/env python3
"""The whip: what it refuses to do.

A stopped session cannot notice it has stopped, so something outside has to. That thing
decides with an LLM — which means the safety cannot come from the model being right. It
comes from the action space (nudge / escalate / nothing, and no "approve") and from the
handful of questions that are never asked of a model at all:

  * is something OTHER than claude sitting on stdin (a password prompt)? — regex
  * is the human mid-typing? — the same call the API-error path makes
  * is this session even registered? — the human's own switch
  * backoff, attempt caps, "have I said this already?" — bookkeeping

Those are what this pins. It lives in cc_web (folded into the 3-minute loop that
already nudged sessions stuck on API errors, rather than being a second program with a
second copy of the same four guards).

    python3 tests/test_whip.py      # exit 0 = pass
"""
import datetime as _dt
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
    home = tempfile.mkdtemp(prefix="ccweb-whip-")
    os.makedirs(os.path.join(home, ".claude"))
    os.environ["HOME"] = home
    os.environ["CC_WEB_TOKEN"] = "t"
    try:
        import cc_web
    except Exception as e:                                  # pragma: no cover
        print("SKIP: cannot import cc_web:", e); return 0
    src = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()

    # Three times now, editing this file with a start→end source slice has swept away a
    # NEIGHBOURING function along with the target (_whip_last_say once, then
    # _whip_stuck_reason / _whip_rounds / _WHIP_RED_TEXT together). Every time, the file
    # still compiled — a name that only appears inside a function body is not resolved
    # until that body RUNS, so the breakage waits for the loop to fire on a real
    # session. Cheap to check, so it is checked rather than remembered.
    print("=== nothing the whip code references has gone missing ===")
    missing = sorted(n for n in set(re.findall(r"\b(_whip_\w+|_WHIP_\w+)\b", src))
                     if not hasattr(cc_web, n))
    check("every whip symbol referenced in cc_web.py is defined", not missing, str(missing))

    print("=== the one gate where being wrong is actually dangerous ===")
    # claude spawns children, and a child can be sitting on stdin. The tab is still a
    # claude tab, so the roster still lists it — but a nudge typed now becomes the
    # password. Broad on purpose, and used only to REFUSE.
    for text, want, why in (
            ("[sudo] password for zmc:", True, "sudo"),
            ("Enter passphrase for key '/home/x/.ssh/id_rsa':", True, "ssh key"),
            ("Password for 'https://github.com':", True, "git over https"),
            ("Verification code: ", True, "2FA"),
            ("Are you sure you want to continue connecting (yes/no/[fingerprint])?", True, "host key"),
            ("(END)", True, "a pager has the terminal"),
            ("--More--", True, "a pager has the terminal"),
            ("╭─ Do you want to proceed? ─╮\n│ 1. Yes  2. No │", False, "a claude menu is NOT stdin"),
            ("> ", False, "an ordinary composer"),
            ("● Bash(git status)\n  ⎿ nothing to commit", False, "ordinary transcript"),
    ):
        got = bool(cc_web._WHIP_AT_STDIN.search(text))
        check(f"{'refuses' if want else 'allows'}: {why}", got == want, repr(text[:40]))

    print("=== one model call, and all it does is classify ===")
    # The shape of this changed completely, and the reason is worth keeping: the whip
    # used to have the model DECIDE what to do (nudge / authorize / run / escalate) and
    # COMPOSE the message. That produced, on real sessions, a nudge telling a session to
    # "提交并 push" when the policy reserved push for the human — the model widening its
    # own remit in the most ordinary-looking way. So the actions it had to choose between
    # are gone, and with them the choosing: what goes into the session is now a template
    # built from the human's own Task text, and the model answers one question with one
    # of four words. A classifier cannot write a sentence into somebody's terminal.
    check("there is one prompt now, not two tiers",
          hasattr(cc_web, "_WHIP_ASK_SYS")
          and not hasattr(cc_web, "_WHIP_TRIAGE_SYS") and not hasattr(cc_web, "_WHIP_DECIDE_SYS"))
    for st in ("confirmed", "asking_human", "blocked_cmd", "stopped"):
        check(f"...it knows the state {st}", st in cc_web._WHIP_ASK_SYS)
    check("...and is told not to write the message",
          "不要写任何" in cc_web._WHIP_ASK_SYS and "由程序按模板拼" in cc_web._WHIP_ASK_SYS)
    check("the whole prompt got smaller by an order of magnitude",
          len(cc_web._WHIP_ASK_SYS) < 1500, str(len(cc_web._WHIP_ASK_SYS)))
    # The two judgements that were hard-won and must survive the rewrite.
    check("it still warns the question is often not at the end",
          "常常不在末尾" in cc_web._WHIP_ASK_SYS)
    check("...and that a command in a message is not a request to run it",
          "不等于它在等这条命令" in cc_web._WHIP_ASK_SYS)
    # The box in the UI promises "原样作为 prompt 交给看门 agent". This is that promise.
    check("the human's policy is handed over and outranks the defaults",
          "最高依据" in cc_web._WHIP_ASK_SYS and "高于上面的默认判据" in cc_web._WHIP_ASK_SYS)
    _b0 = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    check("...and it is actually in the facts, not just promised",
          '"策略": policy' in _b0)

    print("=== nothing dangerous is left in the action space ===")
    # Deleted, not disabled: `authorize` (saying yes on the human's behalf) and `run`
    # (typing `! cmd`, which executes). With them went the allow-list, the byte-for-byte
    # command resolver, the once-only execution guard — machinery that existed only to
    # make executing things survivable. The safest version of a dangerous feature is
    # its absence.
    for gone in ("_whip_resolve_cmd", "_whip_cmd_allowed", "_whip_norm_cmd",
                 "_whip_already_ran", "_CHAIN"):
        check(f"{gone} is gone", not hasattr(cc_web, gone))
    check("no action executes anything", '"run"' not in _b0 and "authorize" not in _b0)
    check("...and the only thing it can send is still text",
          set(re.findall(r"bridge\.\w+\(", _b0))
          <= {"bridge.send_text_to(", "bridge.input_typed_text(", "bridge.get_screen_for("},
          str(sorted(set(re.findall(r"bridge\.\w+\(", _b0)))))
    check("the allow box is out of the memo as well",
          '"allow"' not in src and "supervisor_allow" not in src)

    print("=== what gets typed is the human's own task text ===")
    # Asserted on the RENDERED message, not on source substrings: the whole claim here
    # is about what lands in somebody's terminal, and a source match can be true of code
    # that never runs. _whip_msg exists so this can be checked at all.
    TASK, NOTES = "把评测管线收口, 测试跑绿", "改动要带测试"
    ask = cc_web._whip_msg(TASK, NOTES, "stopped")
    check("the task text goes in verbatim", TASK in ask, ask[:40])
    check("...and the standing notes with it", NOTES in ask)
    check("...it asks for an explicit guarantee, and says what stops the asking",
          "确认完成" in ask and "不再问" in ask)
    check("...and says it is not permission for anything",
          "不构成对任何需要人确认的动作的许可" in ask)
    check("...and is tagged so the session cannot read it as a human's reply",
          ask.startswith("[驱动]"))
    check("empty notes leave no dangling header",
          "注意事项" not in cc_web._whip_msg(TASK, "", "stopped"))
    check("...and whitespace-only notes count as empty",
          "注意事项" not in cc_web._whip_msg(TASK, "   \n ", "stopped"))
    # Nothing model-written can reach it: the only inputs are the task, the notes and
    # which of two fixed tails to use.
    import inspect as _insp
    check("the message function takes no model output at all",
          list(_insp.signature(cc_web._whip_msg).parameters) == ["task", "notes", "state"],
          str(list(_insp.signature(cc_web._whip_msg).parameters)))
    # The `! cmd` answer is a SENTENCE now, not an execution: decide for yourself
    # whether the task needs it. The session's own permission prompts stay in front of
    # whatever it then does, which is the whole reason this is enough.
    cmd_msg = cc_web._whip_msg(TASK, "", "blocked_cmd")
    check("a blocked command earns words, not a keystroke",
          "授权你自己去做" in cmd_msg)
    # Conditional, and the condition is checkable by the reader: the task it must be
    # necessary FOR is in the same message.
    check("...and the authorisation is conditional on that task",
          "如果这个行为是上面这个任务必须的" in cmd_msg and TASK in cmd_msg)
    check("...and it is told to say so if only a human can do it",
          "只有人能做" in cmd_msg)
    check("...and no command text appears in it — it never quotes one back",
          "!" not in cmd_msg.replace("[驱动]", ""), cmd_msg[-60:])

    print("=== a confirmation ends the asking, and a new task restarts it ===")
    # Why an explicit guarantee rather than inferring doneness: the session gets to end
    # the loop itself, and the human gets a sentence they can hold it to. It is scoped
    # to the task VERSION (`rev`), so editing the task asks again — a new task is a new
    # question, and the memo already counts versions.
    check("a confirmation is recorded against the task version",
          'st["confirmed_rev"] = memo.get("rev")' in _b0)
    check("...and the asking counter resets with it", 'st["nudges"] = 0' in _b0)
    check("a session waiting on YOUR choice is left alone",
          "它在等你做选择,不催" in _b0)
    check("...and that is a 'nothing', not a nudge",
          '"action": "nothing", "why": "它在等你做选择' in _b0)


    print("=== `! cmd` stays mechanical: the command text is a fact ===")
    for name, text, want in (
            ("a fenced bang command", "请你跑:\n```\n! bash tests/run_all.sh\n```", ["! bash tests/run_all.sh"]),
            ("inline backticks", "麻烦执行 `! git push` 谢谢", ["! git push"]),
            ("a bulleted one", "- `! make test`\n", ["! make test"]),
            # A markdown image is `![alt](url)`, which starts with `!` and is not a
            # command — the negative lookahead exists for exactly that.
            ("a markdown image is not a command", "![shot](a.png)\n", []),
            ("prose with an exclamation", "改完了!\n测试都过了。", []),
            # Found by scanning real history: two of the only four `!`-hits in 40
            # session files were comparison operators in backticks. Harmless in the UI
            # popup (a junk row a human ignores); not harmless here, where this list is
            # what the model verifies and the code may then execute.
            ("`!= \"\"` is a comparison, not a command", 'if `!= ""` then', []),
            ("...nor is `!==`", "use `!== null` here", []),
            ("...nor a bare bang", "`!` alone", []),
            ("...but a real one beside them still lands",
             'compare with `!= ""`, then run `! bash x.sh`', ["! bash x.sh"]),
    ):
        got = cc_web._whip_asked_you_to_run(text)
        check(f"{name}", got == want, str(got))

    print("=== a destructive command still goes to YOU, not an authorization ===")
    # The nine prose red lines are gone with the prompt that carried them: they bounded
    # a model that chose actions, and this one only classifies. What survives is the one
    # case that still matters — the session asking a human to run something
    # catastrophic — and it is caught deterministically, before any model call, and
    # reported instead of answered with "授权你自己去做".
    check("the prose red lines went with the deciding prompt", not hasattr(cc_web, "_WHIP_RED"))
    check("...but the command screen stayed", hasattr(cc_web, "_WHIP_RED_TEXT"))
    # VERBATIM from one machine's history: 120 `! cmd` responses across 13 sessions,
    # scanned whole (1.04 GB in 1.5s — the tail-only sample that preceded this found 2
    # and was wrong by sixty times). Against that population the screen caught 2 of 39,
    # and what it missed was not exotic, it was the actual work. These are the real
    # strings, kept as strings, because invented examples are what let it pass at 2/39.
    for name, cmd, want in (
            ("reads a production secret out of a cluster",
             "! kubectl --context acme-aks-prod -n default get secret prod-vault "
             "-o jsonpath='{.data.APP_TOKEN_SECRET}'", True),
            ("...even with the context left as a placeholder",
             "! kubectl --context <prod上下文名> -n app get secret shared-vault", True),
            ("runs something on another host",
             "! ssh deploy@203.0.113.7 -t 'az login --use-device-code'", True),
            ("...and worse, with sudo on the far end",
             "! ssh deploy@203.0.113.7 -t 'sudo tailscale up --hostname=example-vm'", True),
            ("creates cloud resources, i.e. spends money",
             "! S=00000000; az group create --subscription $S --name EXAMPLE-RG", True),
            ("...a VM too", "! az vm create --subscription 00000000-...", True),
            ("deletes an object on a shared cluster",
             "! kubectl --context acme-aks-dev -n jobs delete job "
             "regrade-canary", True),
            ("writes a session cookie into a file",
             "! sed -i '' 's|^SESSION_COOKIE=.*|SESSION_COOKIE=\"session_id=...\"|' /Users/x/.env.local", True),
            ("rm -rf /", "! rm -rf /", True),
            # The other half of the measurement, and the reason none of the following is
            # screened: this is ordinary work, and it is the human's own policy — not a
            # hardcoded list — that should decide about it.
            ("running a local script", "! bash scripts/run_smoke.sh", False),
            ("a local python run", "! python3.11 scripts/fetch_prod.py", False),
            ("fetching from a remote it already has access to", "! git fetch origin main", False),
            ("listing kube contexts", "! kubectl config get-contexts -o name 2>&1", False),
            ("deploying to DEV", "! kubectl --context acme-aks-dev apply -f /tmp/job.yaml", False),
            ("read-only look at prod", "! kubectl get ns --context acme-aks-prod", False),
            ("re-authenticating a CLI", "! mycli logout && mycli login", False),
    ):
        got = bool(cc_web._WHIP_RED_TEXT.search(cmd))
        check(f"{'screened' if want else 'left to the policy'}: {name}", got == want, cmd[:44])


    print("=== the extractor, measured against how these are actually written ===")
    # Every one of the 39 distinct real commands was written `! cmd`, with a space.
    # Without requiring it, the matches included `!important` (CSS) and
    # `!output.isEmpty` (Kotlin) — code fragments offered up as things to run.
    for name, text, want in (
            ("CSS !important is not a command", "用 `!important` 覆盖一下", []),
            ("nor a negated call", "`!output.isEmpty` 判空", []),
            ("a real one still lands", "跑 `! bash x.sh`", ["! bash x.sh"]),
            ("...including a long real one",
             "! kubectl --context acme-aks-dev apply -f /tmp/job.yaml",
             ["! kubectl --context acme-aks-dev apply -f /tmp/job.yaml"]),
    ):
        check(f"{name}", cc_web._whip_asked_you_to_run(text) == want,
              str(cc_web._whip_asked_you_to_run(text))[:60])

    print("=== busy is not stopped, and full is not stuck-in-a-fixable-way ===")
    # All three of these came out of sampling 13 real tabs on another machine rather
    # than from reasoning about the code, which is why they are here as cases and not as
    # a comment: compaction runs for minutes with an unchanging transcript, and the
    # simulation cheerfully decided a compacting session had "stopped at 100% context"
    # and should be told to start a new one.
    for name, screen, want in (
            ("mid-compaction", "✢ Compacting conversation… (3m 29s · ↓ 8.6k tokens)", True),
            ("a tool still running", "● Bash(pytest)\n  ⎿ running… (esc to interrupt)", True),
            ("an ordinary idle composer", "● Done.\n\n────────\n❯ \n────────\n", False),
            ("finished work that merely mentions tokens",
             "跑完了, 一共 8.6k tokens 的输出。\n❯ \n", False),
    ):
        got = bool(cc_web._WHIP_BUSY.search(screen))
        check(f"{'busy' if want else 'not busy'}: {name}", got == want, repr(screen[:40]))
    check("a full context is a death, not something a nudge fixes",
          bool(cc_web._whip_stuck_reason([], "100% context used")), "")
    check("...and it says what to do about it",
          "/clear" in cc_web._whip_stuck_reason([], "100% context used"))
    # Ordering matters: the status line shows "100% context used" WHILE compacting, so
    # if the death check ran first every compacting session would be escalated about.
    _body = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    check("...but busy is checked FIRST, or compaction escalates every time",
          _body.index("_WHIP_BUSY.search") < _body.index("_whip_stuck_reason("))

    print("=== an armed watcher with no task does not invent one ===")
    # The whip needs a POLICY to be registered but nothing forced a TASK, so a session
    # could be armed with no goal — and "keep it going" with no goal means the whip
    # chooses the goal. Sampling caught it: an empty task produced a nudge whose own
    # text said "任务是空的,不需要做任何事", i.e. noise typed at a finished session.
    check("an empty task refuses before any model is called",
          _body.index("not task.strip()") < _body.index("_whip_llm("))
    check("...and says the registration is incomplete, rather than going quiet",
          "Task 是空的" in _body and "_whip_escalate" in
          _body[_body.index("not task.strip()"):_body.index("not task.strip()") + 400])

    print("=== the blocked-command case, end to end ===")
    #   the session wants something → its permission prompt stops it → it asks the HUMAN
    #   to run it and goes quiet → the whip arrives and its whole purpose is "do not
    #   stop".
    # Three designs deep now. First it nudged "用你觉得最合适的途径继续", which reads as
    # encouragement and MEANS "go around the thing that stopped you". Then it grew
    # `authorize` and `run` so it could say yes and execute — safe only behind an
    # allow-list, a byte-for-byte resolver and a once-only guard. Now: it says one
    # sentence, and the session decides. No execution, so none of that machinery.
    body = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    check("a destructive command is screened before any model runs",
          body.index("_WHIP_RED_TEXT.search(c)") < body.index("_whip_llm("))
    check("...and is reported to the human instead",
          "它要你替它跑一条踩红线的命令" in body and "_whip_escalate" in body)
    check("...which is a notification, not a refusal-then-nudge",
          "我不催它" in body)
    # The sentence itself: authorisation is conditional and the condition is the human's
    # own task. "If this is required BY THE TASK, then go ahead" — the task text is
    # right there in the same message, so the condition is checkable by the thing being
    # told. Nothing is granted for anything outside it.
    # Rendered, not source-sliced: these strings live in _whip_msg now, and an
    # assertion that reads a slice of _whip_check breaks the moment the code moves —
    # which is how this very check failed, twice in one session.
    _m = cc_web._whip_msg("任务 X", "", "blocked_cmd")
    check("the authorisation it does give is conditional on the task",
          "如果这个行为是上面这个任务必须的" in _m and "任务 X" in _m)
    check("...and the session is the one that decides, by its own route",
          "按你觉得最合适的途径推进" in _m)
    check("...and this is NOT the pending-confirm case wearing a different hat",
          body.index("_WHIP_RED_TEXT.search(c)") > body.index("pend"))


    check("the policy can narrow the one authorisation the template grants",
          "保留给自己" in cc_web._WHIP_ASK_SYS and "不要归 blocked_cmd" in cc_web._WHIP_ASK_SYS)
    # Without this the fixed blocked_cmd line ("if the task needs it, go ahead") reached
    # a session whose human had reserved exactly that action — the template cannot be
    # tuned, so the CLASSIFICATION has to be.
    check("...by classifying it as something not to nudge at all",
          "asking_human" in cc_web._WHIP_ASK_SYS.split("保留给自己")[1][:120])

    print("=== how often it asks is the human's number, not a backoff ladder ===")
    _bp = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    check("a per-session period overrides the configured ladder",
          'period_min' in _bp and "_whip_backoff(cfg)" in _bp
          and _bp.index("period_min") < _bp.index("backoff ="),
          "period read before the ladder is chosen")
    check("...and with none set it falls back to the ladder",
          "if period_min > 0 else _whip_backoff(cfg)" in _bp)
    check("...it is clamped on the way in, not trusted",
          "min(24 * 60.0, float(payload.supervisor_period_min))" in src)

    print("=== the switch has an expiry, so forgetting it decays to off ===")
    # A watchdog you turned on for one afternoon and forgot is a watchdog typing into
    # your sessions next month. So "registered" is not the boolean the browser last
    # wrote — it is computed from the expiry every time it is asked, which means a stale
    # tab reporting "on" cannot arm anything, and nothing has to remember to turn it off.
    now = _dt.datetime.now()
    future = (now + _dt.timedelta(hours=2)).isoformat(timespec="seconds")
    past = (now - _dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    for name, sup, active in (
            ("on, two hours left", {"enabled": True, "policy": "继续", "expires_at": future}, True),
            ("on, but expired a minute ago", {"enabled": True, "policy": "继续", "expires_at": past}, False),
            ("on with no expiry (the '不限' choice)", {"enabled": True, "policy": "继续", "expires_at": ""}, True),
            # A policy used to be required here, on the grounds that an armed switch
            # with nothing written in it had nothing to say. That was true when the
            # watcher CHOSE its actions from that prose. It no longer chooses anything —
            # it classifies four ways and sends a template built from the human's own
            # Task — so the defaults are complete, the policy is optional tuning, and
            # what is genuinely required is the TASK (checked separately: an empty task
            # escalates rather than letting the whip invent a goal).
            ("on with an empty policy — the defaults are enough",
             {"enabled": True, "policy": "", "expires_at": future}, True),
            ("switched off", {"enabled": False, "policy": "继续", "expires_at": future}, False),
    ):
        check(f"{'watched' if active else 'not watched'}: {name}",
              cc_web._watch_active(sup) == active, str(cc_web._watch_active(sup)))
    check("an expired one is reported as EXPIRED, not as never-armed",
          cc_web._watch_expired({"enabled": True, "policy": "继续", "expires_at": past}) is True
          and cc_web._watch_expired({"enabled": False, "policy": "", "expires_at": ""}) is False)
    check("...and how long is left travels with it, for the window to show",
          0 < (cc_web._watch_remaining({"enabled": True, "policy": "继续",
                                        "expires_at": future}) or 0) <= 7200)
    # The gate itself asks that same question rather than re-deriving it.
    check("the whip's registration gate goes through it",
          'get("watched")' in src[src.index("def _whip_registered"):][:900])

    print("=== the last decision sees what I said recently, not just the task ===")
    # The A/B that justified this: policy "push 随你" + my last message "先别 push" flipped
    # the decision from nudge to escalate. The standing policy is what I meant in
    # general; the last few rounds are what I meant today, and today wins.
    rounds = cc_web._whip_rounds([
        {"type": "user", "message": {"content": [{"type": "text", "text": "先别 push"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "好, 那我等你。"}]}},
    ])
    check("both sides of a round are there", len(rounds) == 2, str(rounds))
    check("...and labelled by WHO, so 'I said' is not read as 'it said'",
          [r.get("谁") for r in rounds] == ["人", "它"], str([r.get("谁") for r in rounds]))
    long_turn = [{"type": "user", "message": {"content": [{"type": "text", "text": "详细说明。" * 400}]}}]
    r = cc_web._whip_rounds(long_turn)[0].get("说", "")
    check("an over-long turn is head+tail truncated, not dropped",
          "chars skipped" in r and len(r) < 1200, f"{len(r)}")
    check("it is bounded to a few rounds",
          len(cc_web._whip_rounds([{"type": "user", "message": {"content":
               [{"type": "text", "text": f"第{i}轮"}]}} for i in range(40)])) <= 8,
          str(len(cc_web._whip_rounds([{"type": "user", "message": {"content":
              [{"type": "text", "text": "x"}]}} for i in range(40)]))))

    print("=== a selector is waiting for a KEYPRESS, so do not type ===")
    # Numbered menus (permission, trust, /model) are already caught by
    # _detect_pending_confirm_from_screen. What this adds is the selector with no
    # numbers — and the trap it walked into first: markdown blockquotes.
    for name, screen, want in (
            ("an ordinary idle composer", "● Done.\n\n────────\n❯ \n────────\n", False),
            ("the trust prompt", "Do you trust the files in this folder?\n❯ 1. Yes\n  2. No\n", True),
            ("a selector with no numbers", "Select a file:\n❯ src/app.py\n  src/util.py\n", True),
            ("arrow-key hint text", "use arrow keys to select, esc to cancel\n", True),
            # `> ` starts a markdown quote, and this project's transcripts are full of
            # them: accepting it meant a session that quoted anything could never be
            # nudged again. A unit case caught that, not reasoning about it.
            ("a markdown blockquote", "他说:\n> 这样不行\n\n我改好了。\n", False),
            ("a > inside a code fence", "```\n> import x\n```\n完成\n", False),
    ):
        got = cc_web._whip_menu_open(screen)
        check(f"{'refuses' if want else 'allows'}: {name}", got == want, repr(screen[:34]))

    print("=== the ways a session dies without asking anything ===")
    # Each of these used to be a silent skip — the whip passed over it forever, and
    # "nobody is looking" was indistinguishable from "nothing to do". None can be
    # nudged (a "继续" does not fix an expired login), so they are the one thing worth
    # a notification.
    for name, text, want in (
            ("usage limit", "You've reached your usage limit. Resets at 3pm.", True),
            ("expired auth", "authentication_error: oauth token expired, please run /login", True),
            ("context too long", "API Error: prompt is too long: 210000 > 200000", True),
            ("a TLS failure", "SSL: certificate verify failed", True),
            ("an ordinary sign-off", "改完了, 测试都过。", False),
    ):
        got = bool(cc_web._whip_stuck_reason([], text))
        check(f"{'notices' if want else 'ignores'}: {name}", got == want,
              cc_web._whip_stuck_reason([], text)[:40])
    check("...and it says what is needed, not just that something is wrong",
          "/login" in cc_web._whip_stuck_reason([], "please run /login"))

    print("=== a binding is verified before anything is typed ===")
    # Every other reader of a binding does this. This one did not, and the consequence
    # is not "reads nothing": a dead tab whose pid has been reused, with the binding
    # still on file, means typing into somebody else's terminal.
    src0 = open(os.path.join(ROOT, "cc_web.py"), encoding="utf-8").read()
    body0 = src0[src0.index("async def _whip_check"):src0.index("async def _whip_pass")]
    check("verify_binding is called", "verify_binding(b)" in body0)
    check("...before the input box is read or anything is sent",
          body0.index("verify_binding(b)") < body0.index("input_typed_text"))
    check("...and a dead binding is dropped, not just skipped",
          "bindings.remove_session(sid)" in body0)

    print("=== 'did the nudge lead anywhere' is recorded ===")
    check("a nudge arms the check", "await_progress" in body0 and "last_nudge_at" in body0)
    check("...and the next pass writes the answer down",
          "nudge-result" in body0 and '"progress": moved' in body0)
    # Without this number there is no way to defend the whip in a month.
    check("...exactly once per nudge", 'st["await_progress"] = False' in body0)

    print("=== an unreachable terminal is said once, not skipped forever ===")
    check("reading the input box failing escalates",
          "桥可能坏了" in body0 and "_whip_escalate" in body0)

    print("=== an unparseable decision does nothing (fail closed) ===")
    check("prose instead of JSON → None", cc_web._whip_json("I think it should continue") is None)
    check("broken JSON → None", cc_web._whip_json('{"action": "nudge"') is None)
    check("a JSON array → None (not a decision)", cc_web._whip_json("[1,2,3]") is None)
    check("JSON in a fence is still read",
          (cc_web._whip_json('```json\n{"action":"nothing","reason":"x"}\n```') or {}).get("action")
          == "nothing")

    print("=== escalation is once per situation, not once per pass ===")
    # A notification that repeats every three minutes is a notification you turn off.
    st = {}
    a = cc_web._whip_escalate("sid1", st, "它卡在权限确认上", dry=True)
    b = cc_web._whip_escalate("sid1", st, "它卡在权限确认上", dry=True)
    c = cc_web._whip_escalate("sid1", st, "另一件事", dry=True)
    check("first time it escalates", a["action"] == "escalate", str(a))
    check("...the same thing again is dropped", b["action"] == "skip", str(b))
    check("...but a different thing gets through", c["action"] == "escalate", str(c))

    print("=== the action space, and what is no longer in it ===")
    body = src[src.index("async def _whip_check"):src.index("async def _whip_pass")]
    # Three versions of this section. It began as "exactly three actions and no way to
    # approve anything". Then the human asked for authorize and run, so the invariant
    # moved to "it may say yes, but only to a command it did not write". Now they are
    # gone again — deliberately, as the answer to "is this method fundamentally
    # unreliable": the judging half is sound, the ACTING half was where every accumulated
    # prompt patch lived, and it was earning a couple of cases a month. So the invariant
    # is back to the strongest form, and this time by subtraction rather than by rule.
    check("the action space is closed, and this is the whole of it",
          '("confirmed", "asking_human", "blocked_cmd", "stopped")' in body, "not found")
    check("...with nothing in it that executes",
          '"action": "run"' not in body and '"authorize"' not in body)
    # The model is never handed a key, an option index, or a menu selection: the only
    # write in the whole path is send_text_to. So a wrong classification costs a message
    # nobody needed, never a tool call nobody authorised.
    sends = re.findall(r"bridge\.\w+\(", body)
    check("...and the only thing it can send is text",
          set(sends) <= {"bridge.send_text_to(", "bridge.input_typed_text(",
                         "bridge.get_screen_for("}, str(sorted(set(sends))))
    # Settled: those options need a keypress, and with auto mode on the prompts that
    # still appear are the ones auto mode said need a human. And it does not even
    # notify — the panel already shows a pending confirmation as its own state, so a
    # notification would be a second copy of something visible, and these are only
    # worth reading while they stay rare.
    _p = body.index("if pending:")
    # To the end of that branch, not to a later landmark: slicing further kept sweeping
    # in whatever got added between, twice.
    _e = body.find("\n\n", _p)
    pend_block = body[_p:(_e if _e > 0 else _p + 1400)]
    check("a permission dialog is neither answered nor notified about",
          '"action": "skip"' in pend_block and "_whip_escalate" not in pend_block,
          pend_block[pend_block.index("return"):][:60].replace("\n", " "))
    check("...and the code says so as a decision, not as a TODO",
          "FINAL behaviour" in body and "Do not \"improve\" this by sending" in body)
    check("the message says it is NOT permission for anything",
          all("不构成对任何需要人确认的动作的许可" in cc_web._whip_msg("t", "", k)
              for k in ("stopped", "blocked_cmd")))


    print("=== opt-in, and the gates come before the money ===")
    check("an unregistered session is dropped first",
          body.index('"not registered"') < body.index("_whip_llm"))
    for what, needle in (("quiet for long enough", "only quiet for"),
                         ("actually idle", "_is_claude_idle"),
                         ("you are not mid-typing", "input_typed_text"),
                         ("nothing else is on stdin", "_WHIP_AT_STDIN"),
                         ("no permission dialog", "_detect_pending_confirm_from_screen"),
                         ("the situation changed since last time", "backing off"),
                         ("not already nudged to death", "_WHIP_MAX_NUDGES")):
        check(f"...gate: {what}", body.index(needle) < body.index("_whip_llm"), needle)
    # There is one model call per pass now, not two. The two-tier split (cheap decides
    # IF, strong decides WHAT) was there to avoid paying for the expensive model on every
    # pass — and it stopped being a split when "WHAT" became a template. All the gates
    # above still come before the one call, which is what actually keeps this cheap.
    check("one call, and every gate comes before it",
          body.count("_whip_llm(") == 1, str(body.count("_whip_llm(")))
    check("...using the cheap model key", "whip_triage_model" in body)
    check("hours of 'cannot think' escalate rather than looking like 'nothing needed'",
          "llm_fails" in body and "等于没人看着" in body)

    print("=== it folded into the loop that already did this ===")
    # _api_error_watcher was already a whip: same 3-minute loop, same wait-first, same
    # attempt cap, same don't-clobber-a-human check. A second program outside would
    # have been a second copy of all four.
    loop = src[src.index("async def _api_error_watcher"):]
    loop = loop[:loop.index("\n\n\n")] if "\n\n\n" in loop else loop
    check("the deterministic API-error nudge still runs FIRST",
          loop.index("_maybe_auto_continue") < loop.index("_whip_pass"), "order")
    check("...and the whip runs in the same pass", "_whip_pass(dry=False)" in loop)
    check("a pass can be watched instead of waited for", "/api/whip-run" in src)
    check("every action is logged where the human already looks",
          "_whip_log" in body and "/api/watch-log" in src)

    print("=== the API-error nudge is unconditional; the whip is opt-in ===")
    # These are two different things and must stay two: a turn that died on a network
    # blip is not a judgement call and needs nobody's permission, while the whip acts
    # on a model reading a situation. The danger is a refactor quietly putting (1)
    # behind (2)'s switch.
    check("the API-error path walks every bound session",
          "for b in bindings.all():" in loop and loop.index("bindings.all()") < loop.index("_whip_pass"))
    # Just that function's own body: the whip's code now sits between it and the
    # watcher, so slicing to the watcher swept the whip in and the assertion measured
    # nothing. (It failed loudly, which is the only reason this comment exists.)
    _i = src.index("async def _maybe_auto_continue")
    _j = src.index("\n\n\n", _i)
    ac = src[_i:_j]
    check("...and never asks whether the session is registered",
          "watched" not in ac and "supervisor" not in ac, "auto-continue is unconditional")
    check("the whip's first act is to check registration",
          '"not registered"' in body)

    print("=== the numbers are settable, and read every pass ===")
    # These used to be checked by looking for the key's NAME in cc_web.py. Every one
    # passed while every one of the knobs did nothing: _load_conf keeps a key only if it
    # already has a default (`elif k in cfg`), and none of the six were in that dict, so
    # the file was parsed and thrown away. `whip_triage_model` fell back to `model`,
    # which meant the cheap tier of a two-tier design silently ran the expensive model on
    # every pass — found by reading a debug line that named the wrong model, not by this
    # suite. So the check now WRITES a conf and reads the values back out.
    conf_path = os.path.join(home, ".claude", "cc_web.conf")
    with open(conf_path, "w", encoding="utf-8") as fh:
        fh.write("token=t\nwhip_quiet_seconds=42\nwhip_max_nudges=9\n"
                 "whip_backoff_seconds=11,22,33\nwhip_interval_seconds=7\n"
                 "whip_triage_model=cheap-one\nwhip_decide_model=dear-one\n"
                 "model=fallback-one\n")
    cfg = cc_web._load_conf()
    for key, want in (("whip_quiet_seconds", "42"), ("whip_max_nudges", "9"),
                      ("whip_backoff_seconds", "11,22,33"), ("whip_interval_seconds", "7")):
        check(f"{key} actually arrives from the conf", cfg.get(key) == want, repr(cfg.get(key)))
    check("...and the numbers parse to what was written",
          cc_web._whip_num(cfg, "whip_quiet_seconds", 600) == 42
          and cc_web._whip_backoff(cfg) == (11, 22, 33),
          f'{cc_web._whip_num(cfg, "whip_quiet_seconds", 600)} / {cc_web._whip_backoff(cfg)}')
    # The two tiers exist so that most passes cost almost nothing. If the cheap key is
    # dropped, that saving disappears without a single visible symptom.
    check("the cheap tier really is the cheap model, not a fallback to the dear one",
          (cfg.get("whip_triage_model") or cfg.get("model")) == "cheap-one",
          str(cfg.get("whip_triage_model") or cfg.get("model")))
    # `whip_decide_model` is gone: there is no second tier to point at a model. An
    # unknown key must stay ignored rather than half-applied, which is checked below.
    check("...and the retired second-tier key is not resurrected by accident",
          "whip_decide_model" not in cfg)
    for key, default in (("whip_quiet_seconds", "600"), ("whip_max_nudges", "3"),
                         ("whip_backoff_seconds", "600,1800,7200"),
                         ("whip_interval_seconds", "180")):
        check(f"...{key} documented with its default ({default})",
              key in open(os.path.join(ROOT, "config.example", "cc_web.conf"),
                          encoding="utf-8").read())
    # Read per pass, not at startup: turning a knob should not need a restart, which on
    # this server also drops live WebSockets (voice, screen).
    check("the loop re-reads the conf every pass",
          "cfg = _load_conf()" in loop and loop.index("cfg = _load_conf()") < loop.index("asyncio.sleep"))
    check("a malformed number falls back instead of crashing the loop",
          "_whip_num" in src and "except (TypeError, ValueError)" in
          src[src.index("def _whip_num"):src.index("def _whip_backoff")])

    print("\nFAILED: " + ", ".join(_fails) if _fails else "\nall pass")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
