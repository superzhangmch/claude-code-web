"""FastAPI server: stateful bridge between web client and iTerm2.

Architecture (after the session-id-first refactor):
- Picker is built from filesystem only (no iTerm2 scan): session_index.json +
  recently-modified JSONL files in ~/.claude/projects/.
- Mapping pid → claude_session_id is *only* computed inside /api/attach. It's
  cached in BindingTable and trusted thereafter for /api/state and /api/input.
- Cache invalidation: pid death, pid_start change (process restart in same tab),
  conflict with a new attach for the same session_id, or explicit /api/reverify.
- /api/attach uses screen-content scoring. Score = how many JSONL fingerprint
  lines appear on a candidate tab's screen. Score=1.0 unique → auto-bind. Else
  return candidates and let the user pick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import time as _time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from iterm_bridge import ClaudeSessionRef, ItermBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ccweb")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_INDEX_PATH = Path.home() / ".claude" / "session_index.json"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CONF_PATH = Path.home() / ".claude" / "cc_web.conf"
TOP_N_SESSIONS = 10
SNAPSHOT_TAIL_ENTRIES = 200
FINGERPRINT_COUNT = 8
ACTIVE_WITHIN_SEC = 24 * 3600  # show unnamed JSONLs modified in last day


# ---------- conf (single file: ~/.claude/cc_web.conf) ----------

def _load_conf() -> dict:
    """Parse the single config file. Returns dict with keys:
      token (str), api_base, api_key, model (str), cwds (list[str]).
    Multiple `cwd=...` lines accumulate into the cwds list. Lines starting
    with `#` or blank are ignored. Defaults applied for missing keys."""
    cfg: dict = {
        "token": "",
        "api_base": "",
        "api_key": "",
        "model": "claude-haiku-4-5",
        "cwds": [],
    }
    try:
        for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "cwd":
                if v:
                    cfg["cwds"].append(v)
            elif k in cfg:
                cfg[k] = v
    except OSError:
        pass
    return cfg


CONF = _load_conf()


def _load_auth_token() -> str:
    env = os.environ.get("CC_WEB_TOKEN")
    if env:
        return env
    if CONF["token"]:
        return CONF["token"]
    tok = secrets.token_urlsafe(24)
    log.warning("no token in %s — generated ephemeral token: %s", CONF_PATH, tok)
    return tok


AUTH_TOKEN = _load_auth_token()
log.info("auth token: %s  (override with $CC_WEB_TOKEN or edit %s)", AUTH_TOKEN, CONF_PATH)


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    token = authorization[len("Bearer "):]
    if not secrets.compare_digest(token, AUTH_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


# ---------- session_index helpers ----------

def load_session_index() -> list[dict]:
    try:
        with SESSION_INDEX_PATH.open() as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    data = [e for e in data if e.get("session_id") and e.get("title")]
    return data


def _extract_user_msgs(jsonl_path: Path, max_chars: int = 160) -> tuple[str, str, str, str]:
    """Returns (first_text, first_ts, last_text, last_ts). Kept for backward compat."""
    ctx = extract_recent_context(jsonl_path, n_exchanges=9999, max_user_chars=max_chars, max_response_chars=0)
    exs = ctx["exchanges"]
    if not exs:
        return ("", "", "", "")
    first = exs[0]["user"]
    last = exs[-1]["user"]
    return (first["text"], first["ts"], last["text"], last["ts"])


def extract_recent_context(
    jsonl_path: Path,
    n_exchanges: int = 3,
    max_user_chars: int = 100,
    max_response_chars: int = 200,
) -> dict:
    """Walk JSONL once, build a list of "exchanges" — each is a real user msg
    paired with the LAST assistant text that came before the NEXT user msg
    (which is what the user actually saw as 'the reply'). Returns last
    n_exchanges, plus first_user_msg/ts for header preview."""
    exchanges: list[dict] = []  # [{user: {text, ts}, response: {text, ts}|None}]
    pending_response_text: str = ""
    pending_response_ts: str = ""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t == "user" and not (e.get("isMeta") or e.get("isSidechain") or e.get("toolUseResult")):
                    msg = e.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    text = ""
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                    elif isinstance(content, list):
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                                text = p["text"].strip()
                                break
                    if not text:
                        continue
                    # Close out the previous exchange's response (if any) before starting new one
                    if exchanges and exchanges[-1]["response"] is None and pending_response_text:
                        exchanges[-1]["response"] = _trunc_msg(pending_response_text, pending_response_ts, max_response_chars)
                    pending_response_text = ""
                    pending_response_ts = ""
                    exchanges.append({
                        "user": {"text": text[:max_user_chars] + ("…" if len(text) > max_user_chars else ""),
                                  "ts": e.get("timestamp", "") or ""},
                        "response": None,
                    })
                elif t == "assistant" and not e.get("isSidechain"):
                    msg = e.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    text = ""
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                    elif isinstance(content, list):
                        parts = []
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                                parts.append(p["text"])
                        text = "\n\n".join(parts).strip()
                    if text:
                        # Track the latest assistant text reply since the last user msg.
                        # When next user msg arrives, this becomes that exchange's response.
                        pending_response_text = text
                        pending_response_ts = e.get("timestamp", "") or ""
    except Exception:
        pass
    # Close out trailing response (if claude already replied to the latest user msg)
    if exchanges and exchanges[-1]["response"] is None and pending_response_text:
        exchanges[-1]["response"] = _trunc_msg(pending_response_text, pending_response_ts, max_response_chars)

    return {
        "exchanges": exchanges[-n_exchanges:] if n_exchanges > 0 else exchanges,
        "first_user_msg": exchanges[0]["user"]["text"] if exchanges else "",
        "first_ts": exchanges[0]["user"]["ts"] if exchanges else "",
    }


def _trunc_msg(text: str, ts: str, max_chars: int) -> dict:
    if len(text) <= 2 * max_chars:
        out = text
    else:
        skipped = len(text) - 2 * max_chars
        out = f"{text[:max_chars]} ..[{skipped} chars skipped].. {text[-max_chars:]}"
    return {"text": out, "ts": ts}


def _project_path_from_jsonl(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = e.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def find_jsonl_for_session(session_id: str) -> Optional[Path]:
    """Locate a JSONL file by its session_id (filename stem) by scanning all project dirs."""
    if not PROJECTS_ROOT.exists():
        return None
    for proj in PROJECTS_ROOT.iterdir():
        if not proj.is_dir():
            continue
        candidate = proj / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


# ---------- fingerprint scoring (the heart of attach verification) ----------

_NOISE_TOKENS = (
    "auto mode", "shift+tab", "esc to interrupt", "Welcome to", "Tips for",
    "Tip:", 'Try "', "claude is", "no conversation found", "running in",
    "▶▶", "⏵⏵", "│", "─", "═", "Cwd:", "Total cost:",
)
def pick_jsonl_fingerprints(jsonl_path: Path, k: int = FINGERPRINT_COUNT) -> list[tuple[str, float]]:
    """Extract distinctive (line, weight) pairs from a session's JSONL.

    User messages get weight 2.0 — they render as `> ...` lines in Ink and are
    short, very distinctive, and tend to stay on screen. Assistant text gets
    weight 1.0. Returns up to ~k entries (will fill more if user msgs exist)."""
    user_lines: list[str] = []
    asst_lines: list[str] = []
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t not in ("user", "assistant"):
                    continue
                if e.get("isSidechain") or e.get("toolUseResult") or e.get("isMeta"):
                    continue
                is_user = (t == "user")
                msg = e.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                texts: list[str] = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                            texts.append(p["text"])
                for t_text in texts:
                    for raw in t_text.splitlines():
                        s = _normalize_for_match(raw)
                        if len(s) < 15 if is_user else len(s) < 25:
                            continue
                        if any(tok in s for tok in _NOISE_TOKENS):
                            continue
                        if is_user:
                            user_lines.append(s)
                        else:
                            asst_lines.append(s)
    except OSError:
        return []

    # Dedup, prefer recent (later in JSONL = appears at end of list, take from tail)
    def uniq_tail(items: list[str], n: int) -> list[str]:
        seen = set()
        out = []
        for x in reversed(items):
            if x in seen:
                continue
            seen.add(x); out.append(x)
            if len(out) >= n:
                break
        return out

    # Take last 5 user msgs (weight 2.0) and last few assistant lines (weight 1.0)
    chosen_users = uniq_tail(user_lines, 5)
    chosen_asst = uniq_tail(asst_lines, max(0, k - len(chosen_users)))

    fingerprints: list[tuple[str, float]] = []
    for u in chosen_users:
        snippet = _slice_middle(u, 30)
        fingerprints.append((snippet, 2.0))
    for a in chosen_asst:
        snippet = _slice_middle(a, 30)
        fingerprints.append((snippet, 1.0))
    return fingerprints


def _slice_middle(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    mid = len(s) // 2
    return s[max(0, mid - n // 2):mid + n // 2]


def _normalize_for_match(s: str) -> str:
    """Strip the markdown rendering chars so JSONL ↔ screen comparison survives
    Ink-rendered bold/italic/code/headers. Also collapse iTerm2's wide-char
    padding (NULL bytes between glyphs) into whitespace."""
    s = s.strip()
    # iTerm2 packs cell padding around wide chars (CJK, emoji) as \x00 — these
    # would block substring matches between JSONL text and screen content. Fold
    # them into spaces so the \s+ collapse below merges them away.
    s = s.replace("\x00", " ")
    # remove common markdown punctuation that Ink will hide
    for ch in ("**", "__", "`", "#", ">", "*", "_"):
        s = s.replace(ch, "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def score_screen(screen_text: str, fingerprints: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """Weighted score = sum(w of matched) / sum(w of all). Returns (score, matched_strings)."""
    if not fingerprints:
        return (0.0, [])
    norm = _normalize_for_match(screen_text)
    total_w = sum(w for _, w in fingerprints) or 1.0
    matched_w = 0.0
    matched: list[str] = []
    for fp, w in fingerprints:
        if fp and fp in norm:
            matched_w += w
            matched.append(fp)
    return (matched_w / total_w, matched)


# ---------- LLM-assisted candidate picking ----------

def _load_llm_conf() -> dict:
    """Re-read the conf so api key/model can be edited without restart."""
    return _load_conf()


def _llm_http_post(url: str, headers: dict, body: dict, timeout: float) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def llm_pick_candidate(jsonl_path: Path, scored: list[dict]) -> Optional[str]:
    """Ask the configured LLM which iterm tab is the best match for this session.

    Returns iterm_session_id of the picked candidate, or None if the LLM
    declines / call fails / config missing."""
    if not scored:
        return None
    cfg = _load_llm_conf()
    api_base = cfg.get("api_base", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "")
    if not api_base or not model:
        return None

    ctx = extract_recent_context(jsonl_path, n_exchanges=5,
                                 max_user_chars=300, max_response_chars=400)
    excerpts = []
    for ex in ctx.get("exchanges", []):
        u = ex["user"]["text"]
        r = ((ex.get("response") or {}).get("text") or "").strip()
        excerpts.append(f"USER: {u}\nASSISTANT: {r}")
    history = "\n---\n".join(excerpts) or "(no history)"

    tabs = []
    for i, c in enumerate(scored, 1):
        ref = c["ref"]
        tail = (c.get("screen") or "")[-1500:]
        tabs.append(f"### Tab {i} (pid={ref.pid}, cwd={ref.cwd})\n{tail}")
    tabs_text = "\n\n".join(tabs)

    prompt = (
        "You match a Claude Code session to one of several iTerm2 tabs.\n"
        "I show you the session's recent transcript and each tab's current screen.\n"
        "BE CONSERVATIVE: only pick a tab if you can identify SPECIFIC text or\n"
        "topics that appear in BOTH the session transcript AND that tab's screen.\n"
        "If no tab clearly shares specific content with the transcript, return 0.\n"
        f"Otherwise return the tab number 1..{len(scored)}.\n\n"
        f"=== SESSION RECENT TRANSCRIPT ===\n{history}\n\n"
        f"=== CANDIDATE TABS ===\n{tabs_text}\n\n"
        "Answer with just one number."
    )

    url = f"{api_base}/v1/chat/completions"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_llm_http_post, url, headers, body, 20.0),
            timeout=25.0,
        )
    except (urllib.error.URLError, asyncio.TimeoutError, OSError, ValueError) as e:
        log.info("llm pick HTTP failed: %s", e)
        return None
    except Exception as e:
        log.info("llm pick unexpected error: %s", e)
        return None
    try:
        d = json.loads(raw)
        content = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.info("llm pick parse failed: %s; body=%s", e, raw[:500])
        return None
    m = re.search(r"\d+", content or "")
    if not m:
        return None
    n = int(m.group(0))
    if n < 1 or n > len(scored):
        return None
    return scored[n - 1]["ref"].iterm_session_id


# ---------- the binding cache ----------

@dataclass
class Binding:
    claude_session_id: str
    iterm_session_id: str
    pid: int
    pid_start: float
    cwd: str
    jsonl_path: Path
    window_index: int = 0
    tab_index: int = 0
    bound_at: float = field(default_factory=_time.time)


class BindingTable:
    def __init__(self) -> None:
        self._by_session: dict[str, Binding] = {}
        self._by_pid: dict[int, Binding] = {}

    def get_by_session(self, sid: str) -> Optional[Binding]:
        return self._by_session.get(sid)

    def get_by_pid(self, pid: int) -> Optional[Binding]:
        return self._by_pid.get(pid)

    def insert(self, b: Binding) -> None:
        # Conflict resolution: any prior binding with same session_id (from a
        # different pid) is stale → drop. Same for any prior binding for this pid.
        old_by_session = self._by_session.get(b.claude_session_id)
        if old_by_session and old_by_session.pid != b.pid:
            self._by_pid.pop(old_by_session.pid, None)
        old_by_pid = self._by_pid.get(b.pid)
        if old_by_pid and old_by_pid.claude_session_id != b.claude_session_id:
            self._by_session.pop(old_by_pid.claude_session_id, None)
        self._by_session[b.claude_session_id] = b
        self._by_pid[b.pid] = b

    def remove_session(self, sid: str) -> None:
        b = self._by_session.pop(sid, None)
        if b:
            self._by_pid.pop(b.pid, None)

    def all(self) -> list[Binding]:
        return list(self._by_session.values())

    def bound_session_ids(self) -> set[str]:
        return set(self._by_session.keys())


bindings = BindingTable()


def _pid_alive_with_start(pid: int, expected_start: float, tolerance: float = 1.5) -> bool:
    """Verify pid is still alive AND its start time matches (catches pid reuse)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return False
    # Check start time
    from iterm_bridge import _pid_start_time
    actual_start = _pid_start_time(pid)
    if actual_start <= 0:
        return False
    return abs(actual_start - expected_start) <= tolerance


def verify_binding(b: Binding) -> bool:
    return _pid_alive_with_start(b.pid, b.pid_start)


# ---------- JSONL cache (delta reads) ----------

class JsonlCache:
    def __init__(self) -> None:
        self._cache: dict[Path, dict] = {}

    def entries(self, path: Optional[Path]) -> list[dict]:
        if path is None:
            return []
        try:
            st = path.stat()
        except FileNotFoundError:
            self._cache.pop(path, None)
            return []
        c = self._cache.get(path)
        if c is not None and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            return c["entries"]
        if c is not None and (st.st_size < c["offset"] or c.get("ino") != st.st_ino):
            c = None
        start = c["offset"] if c else 0
        entries = list(c["entries"]) if c else []
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = f.read()
        except OSError:
            return entries
        new_offset = start + len(remaining)
        for line in remaining.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            e["_idx"] = len(entries) + 1
            prev_round = entries[-1].get("_round", 0) if entries else 0
            is_round_start = False
            if e.get("type") == "user" and not e.get("isMeta") \
                    and not e.get("isSidechain") and not e.get("toolUseResult"):
                msg_obj = e.get("message") or {}
                cc = msg_obj.get("content") if isinstance(msg_obj, dict) else None
                if isinstance(cc, str) and cc.strip():
                    is_round_start = True
                elif isinstance(cc, list) and any(
                    isinstance(p, dict) and p.get("type") == "text" and p.get("text")
                    for p in cc
                ):
                    is_round_start = True
            e["_round"] = prev_round + (1 if is_round_start else 0)
            entries.append(e)
        self._cache[path] = {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ino": st.st_ino,
            "offset": new_offset,
            "entries": entries,
        }
        return entries


jsonl_cache = JsonlCache()
bridge = ItermBridge()

# Tmp tab counter (for New + button)
_tmp_counter = 0


def _next_tmp_label() -> str:
    global _tmp_counter
    _tmp_counter += 1
    return f"tmp_{_tmp_counter:02d}"


def _suggested_cwds() -> list[str]:
    """Re-read on every call so editing the conf doesn't require a restart."""
    return _load_conf()["cwds"]


async def _ensure_iterm2_running() -> None:
    try:
        out = subprocess.run(
            ["pgrep", "-x", "iTerm2"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        out = ""
    if out:
        return
    try:
        subprocess.Popen(["open", "-a", "iTerm"])
    except Exception as e:
        log.warning("could not launch iTerm2: %s", e)
        return
    import asyncio
    for _ in range(20):
        await asyncio.sleep(0.5)
        if subprocess.run(["pgrep", "-x", "iTerm2"], capture_output=True).stdout.strip():
            return


# ---------- picker (file-only) ----------

NAMED_TOP_N = 5
UNNAMED_TOP_N = 5


def build_picker_sessions() -> list[dict]:
    """Top 5 named (by JSONL mtime) + up to 5 recent unnamed (within 24h).
    Pure filesystem, no iTerm2."""
    import datetime as _dt
    titles = {e["session_id"]: e for e in load_session_index()}
    cutoff = _time.time() - ACTIVE_WITHIN_SEC
    named_items: list[tuple[float, Path, dict]] = []
    unnamed_items: list[tuple[float, Path]] = []
    if PROJECTS_ROOT.exists():
        for proj in PROJECTS_ROOT.iterdir():
            if not proj.is_dir():
                continue
            for jsonl in proj.glob("*.jsonl"):
                try:
                    mtime = jsonl.stat().st_mtime
                except OSError:
                    continue
                sid = jsonl.stem
                named = titles.get(sid)
                if named is not None:
                    named_items.append((mtime, jsonl, named))
                elif mtime >= cutoff:
                    unnamed_items.append((mtime, jsonl))
    named_items.sort(key=lambda x: x[0], reverse=True)
    unnamed_items.sort(key=lambda x: x[0], reverse=True)
    items: list[tuple[float, Path, Optional[dict]]] = []
    for mtime, jsonl, named in named_items[:NAMED_TOP_N]:
        items.append((mtime, jsonl, named))
    for mtime, jsonl in unnamed_items[:UNNAMED_TOP_N]:
        items.append((mtime, jsonl, None))
    items.sort(key=lambda x: x[0], reverse=True)
    bound_ids = bindings.bound_session_ids()
    out: list[dict] = []
    for mtime, jsonl, named in items:
        sid = jsonl.stem
        last_visit = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        try:
            file_size = jsonl.stat().st_size
        except OSError:
            file_size = 0
        is_bound = sid in bound_ids
        binding_info = bindings.get_by_session(sid) if is_bound else None
        # last_user_msg / first_user_msg / last_visit derived from JSONL on
        # every request (the index intentionally only stores title +
        # project_path + first_user_msg snapshot).
        ctx = extract_recent_context(jsonl, n_exchanges=3, max_user_chars=64, max_response_chars=100)
        exs = ctx["exchanges"]
        last_ex = exs[-1] if exs else None
        last_user = last_ex["user"] if last_ex else None
        out.append({
            "claude_session_id": sid,
            "title": (named.get("title") if named else ""),
            "project_path": (named.get("project_path") if named else "") or _project_path_from_jsonl(jsonl) or "",
            "last_visit": last_visit,
            "file_size": file_size,
            "first_user_msg": ctx["first_user_msg"],
            "first_ts": ctx["first_ts"],
            "last_user_msg": last_user["text"] if last_user else "",
            "last_ts": last_user["ts"] if last_user else "",
            "exchanges": exs,        # last 3 user+response pairs
            "named": named is not None,
            "bound": is_bound,
            "binding": _serialize_binding(binding_info) if binding_info else None,
        })
    return out


# ---------- transcript / mode filtering (unchanged from before) ----------

def _trim_brief(e: dict) -> Optional[dict]:
    msg = e.get("message") or {}
    content = msg.get("content")
    new_content = None
    if isinstance(content, str):
        if content.strip():
            new_content = content
    elif isinstance(content, list):
        parts = [
            {"type": "text", "text": p["text"]}
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and (p.get("text") or "").strip()
        ]
        if parts:
            new_content = parts
    if new_content is None:
        return None
    return {
        "uuid": e.get("uuid"),
        "type": e.get("type"),
        "_idx": e.get("_idx"),
        "_round": e.get("_round"),
        "timestamp": e.get("timestamp"),
        "sid": e.get("sessionId"),
        "message": {"content": new_content},
    }


def _truncate_tool_result_content(content):
    MAX_STR = 10_000
    BASE64_KEEP = 16

    def trim_str(s: str) -> str:
        if len(s) > MAX_STR:
            return s[:MAX_STR] + "...(truncated)"
        return s

    def trim_dict(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if k == "source" and isinstance(v, dict) and v.get("type") == "base64" and isinstance(v.get("data"), str):
                v = {**v, "data": v["data"][:BASE64_KEEP] + "..."}
            elif isinstance(v, str):
                v = trim_str(v)
            elif isinstance(v, dict):
                v = trim_dict(v)
            elif isinstance(v, list):
                v = [trim_dict(x) if isinstance(x, dict) else (trim_str(x) if isinstance(x, str) else x) for x in v]
            out[k] = v
        return out

    if isinstance(content, str):
        return trim_str(content)
    if isinstance(content, list):
        return [trim_dict(x) if isinstance(x, dict) else (trim_str(x) if isinstance(x, str) else x) for x in content]
    return content


def _trim_all(e: dict) -> Optional[dict]:
    msg = e.get("message") or {}
    content = msg.get("content")
    new_content = None
    if isinstance(content, str):
        if content.strip():
            new_content = content
    elif isinstance(content, list):
        kept = []
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text" and p.get("text"):
                kept.append({"type": "text", "text": p["text"]})
            elif t == "tool_use":
                kept.append({"type": "tool_use", "name": p.get("name"), "input": p.get("input")})
            elif t == "tool_result":
                kept.append({
                    "type": "tool_result",
                    "content": _truncate_tool_result_content(p.get("content")),
                    "is_error": bool(p.get("is_error")),
                })
            elif t == "thinking" and p.get("thinking"):
                kept.append({"type": "thinking", "thinking": p["thinking"]})
        if kept:
            new_content = kept
    if new_content is None:
        return None
    out = {
        "uuid": e.get("uuid"),
        "type": e.get("type"),
        "_idx": e.get("_idx"),
        "_round": e.get("_round"),
        "timestamp": e.get("timestamp"),
        "sid": e.get("sessionId"),
        "message": {"content": new_content},
    }
    if e.get("isMeta"):
        out["isMeta"] = True
    return out


def _filter_entries(entries: list[dict], mode: str) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        t = e.get("type")
        if t == "queue-operation":
            op = e.get("operation")
            content = e.get("content")
            if op in ("enqueue", "popAll") and isinstance(content, str) and content.strip():
                out.append({
                    "uuid": e.get("uuid"),
                    "type": "user",
                    "_idx": e.get("_idx"),
                    "_round": e.get("_round"),
                    "timestamp": e.get("timestamp"),
                    "sid": e.get("sessionId"),
                    "_queued": True,
                    "message": {"content": content},
                })
            continue
        if t not in ("user", "assistant"):
            continue
        if e.get("isSidechain"):
            continue
        if mode == "brief":
            if e.get("isMeta") or e.get("toolUseResult"):
                continue
            trimmed = _trim_brief(e)
        else:
            trimmed = _trim_all(e)
        if trimmed:
            out.append(trimmed)
    return out


def _is_user_msg(e: dict) -> bool:
    if e.get("type") != "user":
        return False
    if e.get("isMeta") or e.get("isSidechain") or e.get("toolUseResult"):
        return False
    msg = e.get("message") or e
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(p, dict) and p.get("type") == "text" and p.get("text") for p in content)
    return False


def _is_claude_idle(all_entries: list[dict]) -> bool:
    if not all_entries:
        return True
    for e in reversed(all_entries):
        if e.get("type") == "assistant":
            msg = e.get("message") or {}
            sr = msg.get("stop_reason") if isinstance(msg, dict) else None
            return sr == "end_turn"
        if e.get("type") == "user":
            if e.get("isMeta") or e.get("isSidechain") or e.get("toolUseResult"):
                continue
            return False
    return True


def _last_n_rounds(entries: list[dict], n: int) -> list[dict]:
    count = 0
    start = 0
    for i in range(len(entries) - 1, -1, -1):
        if _is_user_msg(entries[i]):
            count += 1
            if count == n:
                start = i
                break
    if count < n:
        start = 0
    return entries[start:]


# ---------- the FastAPI app ----------

async def _binding_reaper(interval_sec: float = 30.0) -> None:
    """Periodically drop bindings whose pid is dead (or whose start time has
    drifted, indicating pid reuse). Runs forever; cancelled on shutdown."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            return
        for b in bindings.all():
            if not verify_binding(b):
                log.info("reaper: dropping dead binding sid=%s pid=%d",
                         b.claude_session_id[:8], b.pid)
                bindings.remove_session(b.claude_session_id)


async def _bg_initial_connect() -> None:
    try:
        await bridge.connect()
        log.info("iTerm2 bridge connected")
    except Exception as e:
        log.warning("initial iTerm2 connect failed: %s "
                    "(will retry on first request)", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire-and-forget the initial connect. When launched by launchd, iTerm2
    # may pop a "Allow this script to control iTerm?" dialog that no one will
    # click — synchronously awaiting connect there hangs startup forever.
    # ensure_connected will retry lazily on the first real request.
    asyncio.create_task(_bg_initial_connect())
    reaper_task = asyncio.create_task(_binding_reaper(30.0))
    try:
        yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(lifespan=lifespan)


# ---------- request models ----------

class AttachPayload(BaseModel):
    claude_session_id: str


class AttachConfirmPayload(BaseModel):
    claude_session_id: str
    iterm_session_id: str
    force: bool = False


class DetachPayload(BaseModel):
    claude_session_id: str


class InputPayload(BaseModel):
    claude_session_id: str
    text: str
    press_enter: bool = True


class NewSessionPayload(BaseModel):
    cwd: str


# ---------- public endpoints ----------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    token = body.get("token", "")
    if not secrets.compare_digest(token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")
    return {"ok": True}


@app.get("/api/sessions", dependencies=[Depends(require_token)])
async def get_sessions():
    """Picker list — pure filesystem. Each entry has 'bound' = True iff there's
    an active pid binding for that session_id."""
    return {"sessions": build_picker_sessions()}


@app.post("/api/attach", dependencies=[Depends(require_token)])
async def post_attach(payload: AttachPayload):
    """Resolve a session_id → iTerm2 pid via screen-content scoring.

    Outcomes:
      - 'bound'    : auto-bind (single tab with score 1.0)
      - 'choose'   : multiple plausible candidates → ask user
      - 'no_match' : zero matches → user must still pick from candidates
      - 'not_running' : no claude tab in matching cwd at all → suggest resume"""
    sid = payload.claude_session_id
    # Already bound? Verify alive — if so, return immediately.
    existing = bindings.get_by_session(sid)
    if existing and verify_binding(existing):
        return {
            "result": "bound",
            "binding": _serialize_binding(existing),
        }
    if existing:  # stale
        bindings.remove_session(sid)

    jsonl = find_jsonl_for_session(sid)
    if jsonl is None:
        raise HTTPException(status_code=404, detail="unknown session_id")

    target_cwd = _project_path_from_jsonl(jsonl)
    fingerprints = pick_jsonl_fingerprints(jsonl)

    try:
        await bridge.ensure_connected()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cannot reach iTerm2: {e}")

    refs = await bridge.list_claude_tabs()
    # Restrict to candidates whose cwd matches the target session's cwd.
    candidates_refs = [r for r in refs if target_cwd is None or r.cwd == target_cwd]
    if not candidates_refs:
        return {"result": "not_running", "session_id": sid, "cwd": target_cwd}

    scored = []
    for r in candidates_refs:
        try:
            screen = await bridge.get_screen_for(r.iterm_session_id, max_lines=200)
        except Exception:
            screen = None
        score, matched = score_screen(screen or "", fingerprints)
        scored.append({"ref": r, "score": score, "matched": matched, "screen": (screen or "")[-1500:]})
    scored.sort(key=lambda x: x["score"], reverse=True)

    top = scored[0] if scored else None
    llm_pick = await llm_pick_candidate(jsonl, scored)

    # Auto-bind iff the LLM picked a tab AND no scored candidate >0 contradicts
    # it. Two cases satisfy that:
    #   1. heuristic top has score>0 and matches the LLM's pick (both agree)
    #   2. ALL candidates score 0 (heuristic is silent — trust the LLM alone)
    # Anything else (LLM rejected; or some other tab scored >0 against LLM's
    # choice) → pop the dialog. Misrouting input is the worst failure mode,
    # but a confident LLM with no contradicting heuristic signal is good
    # enough to skip the manual confirm.
    if llm_pick:
        contradicting = next(
            (c for c in scored
             if c["score"] > 0 and c["ref"].iterm_session_id != llm_pick),
            None,
        )
        chosen = next(
            (c for c in scored if c["ref"].iterm_session_id == llm_pick),
            None,
        )
        if chosen and contradicting is None:
            # Conflict check: same pid already bound to a DIFFERENT live session.
            # Surface it instead of silently stealing the tab.
            existing = bindings.get_by_pid(chosen["ref"].pid)
            if (existing and existing.claude_session_id != sid
                    and verify_binding(existing)):
                return {
                    "result": "conflict",
                    "session_id": sid,
                    "candidates": [_candidate_dict(c) for c in scored],
                    "llm_pick": llm_pick,
                    "conflict": {
                        "iterm_session_id": chosen["ref"].iterm_session_id,
                        "pid": chosen["ref"].pid,
                        "with_session": existing.claude_session_id,
                    },
                }
            b = _build_binding(sid, chosen["ref"], jsonl)
            if b:
                bindings.insert(b)
                reason = ("llm_alone_heuristic_silent"
                          if chosen["score"] == 0
                          else "llm_and_score_agree")
                return {
                    "result": "bound",
                    "binding": _serialize_binding(b),
                    "score": chosen["score"],
                    "llm_pick": llm_pick,
                    "auto_bind_reason": reason,
                }

    result = "no_match" if (not top or top["score"] == 0) else "choose"
    return {
        "result": result,
        "session_id": sid,
        "candidates": [_candidate_dict(c) for c in scored],
        "llm_pick": llm_pick,
    }


@app.post("/api/attach/confirm", dependencies=[Depends(require_token)])
async def post_attach_confirm(payload: AttachConfirmPayload):
    """User picked a specific iterm_session_id from the candidate list."""
    try:
        await bridge.ensure_connected()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cannot reach iTerm2: {e}")
    refs = await bridge.list_claude_tabs()
    ref = next((r for r in refs if r.iterm_session_id == payload.iterm_session_id), None)
    if ref is None:
        raise HTTPException(status_code=404, detail="iterm session not found")
    jsonl = find_jsonl_for_session(payload.claude_session_id)
    if jsonl is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if not payload.force:
        existing = bindings.get_by_pid(ref.pid)
        if (existing and existing.claude_session_id != payload.claude_session_id
                and verify_binding(existing)):
            return {
                "result": "conflict",
                "conflict": {
                    "iterm_session_id": ref.iterm_session_id,
                    "pid": ref.pid,
                    "with_session": existing.claude_session_id,
                },
            }
    b = _build_binding(payload.claude_session_id, ref, jsonl)
    if not b:
        raise HTTPException(status_code=500, detail="could not derive pid_start")
    bindings.insert(b)
    return {"result": "bound", "binding": _serialize_binding(b)}


@app.post("/api/detach", dependencies=[Depends(require_token)])
async def post_detach(payload: DetachPayload):
    bindings.remove_session(payload.claude_session_id)
    return {"ok": True}


@app.post("/api/reverify", dependencies=[Depends(require_token)])
async def post_reverify(payload: AttachPayload):
    """Browser detected a sid mismatch. Force-clear the binding and let the
    next /api/attach do a fresh pairing."""
    bindings.remove_session(payload.claude_session_id)
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(require_token)])
async def get_state(
    claude_session_id: Optional[str] = None,
    since_idx: Optional[int] = None,
    rounds: Optional[int] = None,
    before_idx: Optional[int] = None,
    mode: str = "brief",
):
    """Read state for a specific bound session. Picker is via /api/sessions."""
    if not claude_session_id:
        raise HTTPException(status_code=400, detail="claude_session_id required")
    b = bindings.get_by_session(claude_session_id)
    if b is None:
        raise HTTPException(status_code=409, detail="session not bound")
    if not verify_binding(b):
        bindings.remove_session(claude_session_id)
        raise HTTPException(status_code=410, detail="tab/pid is gone")

    all_entries = jsonl_cache.entries(b.jsonl_path)

    gap_before_idx: Optional[int] = None
    if since_idx is not None:
        delta = [e for e in all_entries if e.get("_idx", 0) > since_idx]
        if rounds is not None:
            capped = _last_n_rounds(delta, rounds)
            if capped and len(capped) < len(delta):
                gap_before_idx = capped[0].get("_idx")
            sliced = capped
        else:
            sliced = delta
    elif before_idx is not None:
        older = [e for e in all_entries if e.get("_idx", 0) < before_idx]
        sliced = _last_n_rounds(older, rounds or 5)
    elif rounds is not None:
        sliced = _last_n_rounds(all_entries, rounds)
    else:
        sliced = all_entries[-SNAPSHOT_TAIL_ENTRIES:]

    transcript = _filter_entries(sliced, mode)
    new_since_idx = since_idx
    if all_entries:
        new_since_idx = all_entries[-1].get("_idx", since_idx)

    has_more = bool(transcript and transcript[0].get("_idx", 0) > 1)

    try:
        screen = await bridge.get_screen_for(b.iterm_session_id)
    except Exception:
        screen = None

    return {
        "binding": _serialize_binding(b),
        "transcript": transcript,
        "since_idx": new_since_idx,
        "has_more_history": has_more,
        "gap_before_idx": gap_before_idx,
        "claude_idle": _is_claude_idle(all_entries),
        "screen": screen,
    }


@app.post("/api/input", dependencies=[Depends(require_token)])
async def post_input(payload: InputPayload):
    b = bindings.get_by_session(payload.claude_session_id)
    if b is None:
        raise HTTPException(status_code=409, detail="session not bound")
    if not verify_binding(b):
        bindings.remove_session(payload.claude_session_id)
        raise HTTPException(status_code=410, detail="tab/pid is gone")
    try:
        await bridge.ensure_connected()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cannot reach iTerm2: {e}")
    text = payload.text
    body = text.replace("\r\n", "\n").rstrip("\r\n")
    multi_line = "\n" in body
    if multi_line:
        body = "\x1b[200~" + body + "\x1b[201~"
    final = body
    if payload.press_enter:
        final = final + "\r"
    ok = await bridge.send_text_to(b.iterm_session_id, final)
    if not ok:
        raise HTTPException(status_code=404, detail="iterm session vanished")
    return {"ok": True}


class ResumePayload(BaseModel):
    claude_session_id: str


@app.post("/api/resume", dependencies=[Depends(require_token)])
async def post_resume(payload: ResumePayload):
    """Open a new iTerm2 tab on the server and run `claude --resume <session_id>`.
    Used when the user picked a session that's not currently running."""
    sid = payload.claude_session_id
    jsonl = find_jsonl_for_session(sid)
    if jsonl is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    cwd = _project_path_from_jsonl(jsonl)
    if not cwd:
        raise HTTPException(status_code=400, detail="cannot determine cwd for session")
    await _ensure_iterm2_running()
    try:
        await bridge.ensure_connected()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cannot reach iTerm2: {e}")
    label = f"resume_{sid[:6]}"
    iterm_id = await bridge.open_resume_claude_tab(cwd, sid, label)
    if not iterm_id:
        raise HTTPException(status_code=500, detail="failed to open new tab")

    # We just opened that tab and ran `claude --resume <sid>` in it. The pid
    # ↔ session mapping is unambiguous: claude on this iterm_session_id IS this
    # session. Poll briefly for the claude process to come up, then bind
    # directly — no attach/score/LLM round trip needed.
    deadline = _time.time() + 12.0
    bound: Optional[Binding] = None
    while _time.time() < deadline:
        await asyncio.sleep(0.6)
        try:
            refs = await bridge.list_claude_tabs()
        except Exception:
            continue
        match = next((r for r in refs if r.iterm_session_id == iterm_id), None)
        if match is None:
            continue
        b = _build_binding(sid, match, jsonl)
        if b:
            bindings.insert(b)
            bound = b
            break

    if bound:
        return {
            "ok": True, "result": "bound",
            "binding": _serialize_binding(bound),
            "iterm_session_id": iterm_id, "label": label, "cwd": cwd,
        }
    # Fell through — claude didn't come up within deadline. Frontend will
    # fall back to its retry-attach path.
    return {"ok": True, "iterm_session_id": iterm_id, "label": label, "cwd": cwd}


@app.get("/api/cwds", dependencies=[Depends(require_token)])
async def get_cwds():
    return {"cwds": _suggested_cwds()}


@app.post("/api/new-session", dependencies=[Depends(require_token)])
async def post_new_session(payload: NewSessionPayload):
    cwd = payload.cwd.strip()
    if not cwd:
        raise HTTPException(status_code=400, detail="cwd required")
    if cwd not in _suggested_cwds():
        raise HTTPException(status_code=400, detail="cwd not in suggested list")
    await _ensure_iterm2_running()
    try:
        await bridge.ensure_connected()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"cannot reach iTerm2: {e}")
    label = _next_tmp_label()
    request_started = _time.time()
    iterm_id = await bridge.open_new_claude_tab(cwd, label)
    if not iterm_id:
        raise HTTPException(status_code=500, detail="failed to open new tab")

    # Poll: wait for the new claude pid to show up on this iterm tab AND for
    # claude to create its JSONL under ~/.claude/projects/<encoded cwd>/.
    # claude generates a fresh session_id on start, so we discover it by
    # picking the newest JSONL born after our request started.
    encoded = cwd.replace("/", "-").replace("_", "-")
    proj_dir = PROJECTS_ROOT / encoded
    deadline = _time.time() + 12.0
    bound: Optional[Binding] = None
    while _time.time() < deadline:
        await asyncio.sleep(0.6)
        try:
            refs = await bridge.list_claude_tabs()
        except Exception:
            continue
        match = next((r for r in refs if r.iterm_session_id == iterm_id), None)
        if match is None or not proj_dir.exists():
            continue
        fresh: list[tuple[float, Path]] = []
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            birth = getattr(st, "st_birthtime", st.st_ctime)
            if birth >= request_started - 1.0:
                fresh.append((birth, jsonl))
        if not fresh:
            continue
        fresh.sort(reverse=True)
        jsonl_path = fresh[0][1]
        sid = jsonl_path.stem
        b = _build_binding(sid, match, jsonl_path)
        if b:
            bindings.insert(b)
            bound = b
            break

    if bound:
        return {
            "ok": True, "result": "bound",
            "binding": _serialize_binding(bound),
            "iterm_session_id": iterm_id, "label": label, "cwd": cwd,
        }
    return {"ok": True, "iterm_session_id": iterm_id, "label": label, "cwd": cwd}


# ---------- helpers used by endpoints ----------

def _build_binding(sid: str, ref: ClaudeSessionRef, jsonl: Path) -> Optional[Binding]:
    from iterm_bridge import _pid_start_time
    pid_start = _pid_start_time(ref.pid)
    if pid_start <= 0:
        return None
    return Binding(
        claude_session_id=sid,
        iterm_session_id=ref.iterm_session_id,
        pid=ref.pid,
        pid_start=pid_start,
        cwd=ref.cwd,
        jsonl_path=jsonl,
        window_index=ref.window_index,
        tab_index=ref.tab_index,
    )


def _serialize_binding(b: Binding) -> dict:
    return {
        "claude_session_id": b.claude_session_id,
        "iterm_session_id": b.iterm_session_id,
        "pid": b.pid,
        "cwd": b.cwd,
        "window_index": b.window_index,
        "tab_index": b.tab_index,
        "bound_at": b.bound_at,
    }


def _candidate_dict(c: dict) -> dict:
    r: ClaudeSessionRef = c["ref"]
    return {
        "iterm_session_id": r.iterm_session_id,
        "pid": r.pid,
        "tty": r.tty,
        "cwd": r.cwd,
        "name": r.name,
        "window_index": r.window_index,
        "tab_index": r.tab_index,
        "score": c["score"],
        "matched_count": len(c["matched"]),
        "screen_tail": c["screen"],
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
