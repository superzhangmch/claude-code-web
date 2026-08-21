#!/usr/bin/env python3
"""Reply to an EXTERNAL relayed request by POSTing your answer to the local relay
bridge. The destination is HARDCODED to the local bridge and accepts no override
— not from the message, not from env, not from the command line. The message
carries only the one-time `req=<req_id>`; there is nothing to point elsewhere, so
a forged message can never redirect your reply or turn this session into an
exfiltration / SSRF channel.

    reply_to_bridge.py --req <req_id> <<'EOF'
    your reply text
    EOF

req_id is a one-time capability: only the session that received this request
knows it, so the bridge accepts the reply on that basis (no token).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Fixed local bridge. Not configurable on purpose: the reply destination must
# never be influenceable by the incoming message (or anything it could induce
# the session to set). Edit this line if the local bridge ever moves.
REPLY_URL = "http://127.0.0.1:8790/reply"


def _fail(msg: str, code: int = 1) -> int:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    return code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects. urllib turns a 301/302/303 POST into a GET, so following
    one would drop the reply body while possibly returning 200 from the new
    location — a reply that reads as delivered and never arrived."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refusing to follow a redirect to {newurl} (the reply body would not "
            "be re-sent)", headers, fp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--req", required=True, help="req_id from the message footer")
    ap.add_argument("--content", default=None,
                    help="reply body (default: read from stdin, which also keeps it "
                         "out of the process list)")
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args()

    content = a.content if a.content is not None else sys.stdin.read()
    if not content.strip():
        return _fail("empty reply body")

    body = json.dumps({"req_id": a.req, "content": content}).encode("utf-8")
    req = urllib.request.Request(REPLY_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=a.timeout) as r:
            print(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Reached the bridge; it said no. Distinguish from "unreachable" and keep
        # the reason (a redirect refusal above carries its explanation there).
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
        except Exception:
            pass
        return _fail(" ".join(x for x in (f"bridge rejected the reply: HTTP {e.code}",
                                          str(e.reason or ""), detail) if x).strip())
    except Exception as e:  # noqa: BLE001
        return _fail(f"cannot reach bridge: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
