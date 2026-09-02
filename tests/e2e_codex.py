#!/usr/bin/env python3
"""Acceptance run for codex support: a live session, in a real browser, end to end.

Not part of tests/run_all.sh — it CREATES and DESTROYS a real codex session, drives a
real browser at it, and costs a couple of minutes and some tokens. Run it by hand when
the codex path changed, or when a suite that only stubs things says everything is fine
and you want to know whether the product is:

    python3 tests/e2e_codex.py         # exit 0 = the whole path works

What it walks, in order, on a brand-new session in a brand-new directory:

  1. creation — trust prompt answered for you, bound in seconds (it used to hang the
     full 30s waiting for a file codex never writes)
  2. the list — present, short id distinguishable, no reviewer sub-threads titled with
     codex's 2000-word "treat this as untrusted evidence" prompt
  3. the browser — codex wording throughout, type in the composer, the answer arriving
     on its own, idle afterwards
  3b. the echo — busy first, then send: placeholder with the pending badge, then
     promoted when the log catches up
  4. auto mode — a command runs unattended, and its activity line is the sub-tool plus
     the command rather than a wall of JavaScript
  5. cross-agent — ask_peer reaches this codex session from the claude side
  6. the other instance — claude on 8443 is untouched
  7. teardown — close-tab, and the session leaves the list

Every assertion here has failed at least once for a real reason; several of the bugs
this catches were invisible to the endpoint tests (the page said "No live claude tab"
while every API call returned 200).
"""

import importlib.util, json, os, re, socket, ssl, subprocess, sys, time, urllib.request, pathlib
ROOT = "/home/zhangmiaochang/code/claude-code-web"; sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("u", os.path.join(ROOT, "tests/test_ui_smoke.py"))
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)
TOK = next(l.split('=',1)[1].strip() for l in (pathlib.Path.home()/'.claude/cc_web.conf').read_text().splitlines() if l.startswith('token='))
CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
CX, CC = "https://100.80.14.27:8444", "https://100.80.14.27:8443"
fails = []
def ck(label, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   [{str(extra)[:150]}]" if extra and not ok else ""))
    if not ok: fails.append(label)
def api(base, path, body=None, timeout=40):
    r = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET")
    r.add_header("Authorization", "Bearer "+TOK)
    if body is not None: r.add_header("Content-Type", "application/json")
    try: return json.load(urllib.request.urlopen(r, timeout=timeout, context=CTX))
    except urllib.error.HTTPError as e: return {"HTTP": e.code, "detail": e.read().decode()[:120]}

print("=== 1. 新建会话: 全新目录, 信任提示应自动过, 且是 auto mode ===")
cwd = "/home/zhangmiaochang/my_code/e2e_" + str(int(time.time()))[-5:]
t0 = time.time()
r = api(CX, "/api/new-session", {"cwd": cwd, "name": "e2e_run"}, timeout=120)
sid = (r.get("binding") or {}).get("claude_session_id", "")
pane = r.get("iterm_session_id", "")
ck("新建返回了一个已绑定的会话", bool(sid) and bool(pane), r)
ck(f"...在 {time.time()-t0:.0f}s 内完成 (曾经会卡满 30s)", time.time()-t0 < 45)
st = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=2")
ck("没有卡在信任提示上", st.get("pending_confirm") is None, st.get("pending_confirm"))

print("=== 2. 列表: 出现、短 id 唯一、无子 agent 会话 ===")
tabs = api(CX, "/api/tabs").get("tabs", [])
ck("新会话在列表里", any(t["sid"] == sid for t in tabs), [t["sid"][:12] for t in tabs])
def short(s): return (s[-4:] if len(s)>14 and s[14]=='7' else s[:4]) if re.match(r'^[0-9a-f]{8}-',s,re.I) else (s.split('-')[-1] or s)[:4]
shorts = [short(t["sid"]) for t in tabs]
ck("短 id 互不相同", len(set(shorts)) == len(shorts), shorts)
ck("没有 2000 字标题的审核子会话", all(len(str(t.get("name") or t.get("tab_name") or "")) <= 80 for t in tabs),
   max((len(str(t.get('name') or '')) for t in tabs), default=0))

print("=== 3. 浏览器: 打开、发送、回显、转正 ===")
def free():
    s=socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); return p
wd = free(); g = subprocess.Popen(["geckodriver","--port",str(wd)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
drv = None
try:
    for _ in range(40):
        time.sleep(0.3)
        try: urllib.request.urlopen(f"http://127.0.0.1:{wd}/status",timeout=2).read(); break
        except Exception: pass
    drv = u.Driver(wd)
    base = "https://thinkpad-x13-linux.tail3870a7.ts.net:8444"
    drv.go(base); drv.js(f"localStorage.setItem('cc_web_token','{TOK}');"); drv.go(base)
    time.sleep(10)
    # 点行进入, 不用 #s= 直接跳: 那样页面不会走完打开流程 (脚本的问题, 非产品的)
    clicked = drv.js("""
      var rows = document.querySelectorAll('#picker-list .brief-row');
      for (var i = 0; i < rows.length; i++) {
        if ((rows[i].innerText || '').indexOf('%s') >= 0) { rows[i].click(); return 'by-name'; }
      }
      if (rows.length) { rows[0].click(); return 'first-row'; }
      return 'no-rows';""" % "e2e_run")
    print("      (进入方式:", clicked, ")")
    time.sleep(9)
    ck("页面标题说这是 codex", "Codex" in drv.js("return document.title"), drv.js("return document.title"))
    ck("输入框措辞是 codex", "Codex" in drv.js("var e=document.getElementById('input');return e?e.placeholder:''"))
    ck("正文里没有 claude 字样", drv.js("return (document.body.innerText.match(/claude/i)||[]).length") == 0)
    MARK = "E2E-" + str(int(time.time()))[-6:]
    drv.js(f"""var ta=document.getElementById('input'); ta.focus(); ta.value='Reply with exactly: {MARK}';
               ta.dispatchEvent(new Event('input',{{bubbles:true}}));""")
    drv.js("var b=document.getElementById('send'); if(b) b.click();")
    echoed = badged = answered = None
    for i in range(40):
        time.sleep(2)
        # 只看 transcript 区域: document.body 会把输入框里还没发出去的文字也算进来,
        # 那样"答案出现了"会在发送的瞬间就假成立。
        body = drv.js("var t=document.getElementById('transcript')||document.querySelector('#main,main');"
                      "return t ? t.innerText : document.body.innerText")
        d = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=3")
        q = [e for e in d.get("transcript", []) if e.get("_queued")]
        if echoed is None and MARK in body: echoed = i*2
        if badged is None and q: badged = i*2
        if echoed is not None and body.count(MARK) >= 2: answered = i*2; break
    ck(f"我的消息立刻回显 ({echoed}s)", echoed is not None and echoed <= 6, echoed)
    ck(f"codex 的回答自己出现在页面上 ({answered}s)", answered is not None, answered)
    d = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=3")
    ck("落盘后占位已撤销", not any(e.get("_queued") for e in d.get("transcript", [])))
    ck("回合结束后 idle=True", d.get("claude_idle") is True, d.get("claude_idle"))
finally:
    if drv:
        try: drv.call("DELETE", f"/session/{drv.sid}")
        except Exception: pass
    g.terminate()

print("=== 3b. 回显占位: 先让它忙, 再发 (纯 API, 不和浏览器抢) ===")
api(CX, "/api/input", {"claude_session_id": sid, "text": "Write 250 words about tides.", "press_enter": True})
time.sleep(7)
busy = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=2").get("claude_idle") is False
ck("会话进入忙碌 (占位的前提)", busy)
M2 = "BADGE-" + str(int(time.time()))[-5:]
r2 = api(CX, "/api/input", {"claude_session_id": sid, "text": f"Then reply with exactly: {M2}", "press_enter": True})
print("      (第二次发送返回:", r2, ")")
d0 = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=4")
print("      (发送后立刻: binding.sid=", (d0.get('binding') or {}).get('claude_session_id'),
      " 占位数=", sum(1 for e in d0.get('transcript', []) if e.get('_queued')), ")")
seen_badge = seen_plain = None
def _t(e):
    c = (e.get("message") or {}).get("content")
    return c if isinstance(c, str) else (str(c[0].get("text")) if isinstance(c, list) and c else "")
# 从 T+0 开始采样, 不先 sleep: 一个"热"会话可能 2 秒内就把消息落盘并转正 —— 那样占位
# 确实存在过, 只是循环开头那一觉正好把它睡掉了 (第一次跑就是这么误判的).
for i in range(30):
    d = api(CX, f"/api/state?claude_session_id={sid}&mode=brief&rounds=4") if i else d0
    def _unused(e):
        c = (e.get("message") or {}).get("content")
        return c if isinstance(c, str) else (str(c[0].get("text")) if isinstance(c, list) and c else "")
    q = [e for e in d.get("transcript", []) if e.get("_queued") and M2 in _t(e)]
    plain = [e for e in d.get("transcript", []) if not e.get("_queued") and M2 in _t(e)]
    if seen_badge is None and q: seen_badge = i*2
    if q and plain: pass
    if seen_badge is not None and not q and plain: seen_plain = i*2; break
    time.sleep(2)
ck(f"忙碌中发出的消息带未确认标记 ({seen_badge}s)", seen_badge is not None, "没出现 _queued")
ck(f"...落盘后标记撤销、消息转正 ({seen_plain}s)", seen_plain is not None, "占位没撤销")

print("=== 4. auto mode: 跑命令不问人, 工具行可读 ===")
api(CX, "/api/input", {"claude_session_id": sid, "text": "Run: echo E2E-CMD-OK", "press_enter": True})
tool_line = None
for i in range(30):
    time.sleep(4)
    d = api(CX, f"/api/state?claude_session_id={sid}&mode=medium&rounds=3")
    for e in d.get("transcript", []):
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tool_line = (part.get("name"), (part.get("input") or {}).get("command"))
    if tool_line: break
ck("工具调用被解析成子工具 + 命令", tool_line is not None and tool_line[0] and tool_line[1],
   tool_line)
ck("...而不是整段 JS 脚本", tool_line is not None and "await tools." not in str(tool_line[1]), tool_line)

print("=== 5. 跨 agent: 从 claude 侧问这个 codex 会话 ===")
# 会话已经说过话了, pending 的合成 id 已被真 thread id 顶替 —— 重新解析一次, 这正是
# 服务端 resolve_session 做的事, 客户端也该照做而不是死抱旧 id.
live = api(CX, "/api/tabs").get("tabs", [])
real = next((t["sid"] for t in live if t.get("cwd") == cwd), sid)
print(f"      (ask-peer 用的 id: {real[:20]}  原始: {sid})")
sid = real
p = subprocess.run(["/usr/bin/python3", os.path.expanduser("~/.claude/skills/ask-peer-claude-code/ask_peer.py"),
                    "--to", sid, "--host", "100.80.14.27", "--timeout", "120"],
                   input="What is 5*5? Just the number.", capture_output=True, text=True, timeout=200)
try: peer = json.loads(p.stdout)
except Exception: peer = {"raw": p.stdout[-200:], "err": p.stderr[-200:]}
ck("ask-peer 拿到了回答", peer.get("status") == "done" and "25" in str(peer.get("reply", "")), peer)

print("=== 6. claude 实例不受影响 ===")
cc = api(CC, "/api/sessions?brief=1")
ck("claude 的会话列表照常", len(cc.get("sessions", [])) > 0, cc.get("HTTP"))
ck("...分组仍只有 tabs", set(s.get("group") for s in cc.get("sessions", [])) <= {"tabs"},
   set(s.get("group") for s in cc.get("sessions", [])))
info = api(CC, "/api/server-info")
ck("...且报自己是 claude", info.get("agent") == "claude", info)

print("=== 7. 收尾: 关掉这个会话 ===")
c = api(CX, "/api/close-tab", {"claude_session_id": sid, "iterm_session_id": pane, "send_exit": True})
ck("close-tab 成功", c.get("tab_closed") is True, c)
time.sleep(4)
ck("列表里已消失", not any(t["sid"] == sid for t in api(CX, "/api/tabs").get("tabs", [])))

print()
print(("FAILED (%d): " % len(fails)) + "; ".join(fails) if fails else "ALL GOOD")
sys.exit(1 if fails else 0)
