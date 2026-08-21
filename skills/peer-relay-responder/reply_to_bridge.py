#!/usr/bin/env python3
"""Reply to an EXTERNAL relayed request by posting your answer back to the relay
bridge. Generic: it knows nothing about any particular bridge or product — it just
POSTs {req_id, content} to the reply-url that arrived in the message framing. Any
bridge following that convention works.

The incoming external message carries, in its footer:
    req=<req_id>  reply-url=<url>
Run this with those two values; put your reply body on stdin.

    reply_to_bridge.py --url <reply-url> --req <req_id> <<'EOF'
    your reply text
    EOF

req_id is a one-time capability: only the session that received this specific
request knows it, so the bridge accepts the reply on that basis (no token).

WHAT THIS SCRIPT DOES AND DOESN'T PROTECT
-----------------------------------------
The URL comes from the message, i.e. from outside, so it is exactly as trustworthy
as the relay that framed it. Given a forged reply-url, posting blindly would turn
this session into (a) an exfiltration channel for whatever it just wrote and (b) a
way to make this machine issue POSTs to services only it can reach. So:

  * only http/https — no file://, ftp://, or anything else urllib would happily open;
  * loopback is refused unless you pass --allow-loopback, so a forged URL can't be
    aimed at services listening on this machine;
  * redirects are refused outright — both because a permitted URL must not be able to
    bounce the reply somewhere else, and because urllib would turn a 302 POST into a
    GET and silently drop the reply body.

Deliberately NOT blocked: private/LAN/tailnet addresses. A legitimate bridge
commonly lives on a tailnet (100.64/10 is "private" to Python), and refusing those
would break the normal case while barely inconveniencing an attacker.

What no check here can decide is whether the URL is the *intended* destination.
That judgment belongs to the session: don't post something you wouldn't hand to
whoever is on the other end.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


def _fail(msg: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    return code


def _host_check(host: str) -> str:
    """"" if the host is fine to post to, else the reason it isn't.

    Reports "cannot resolve" separately from "loopback": both are refused, but saying
    "loopback" about a name that simply doesn't resolve sends you looking in the wrong
    place. (Unresolvable is still refused rather than attempted — resolving later, at
    request time, is exactly the DNS-rebinding gap the loopback check exists to close.)
    """
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addrs = [ai[4][0] for ai in socket.getaddrinfo(host, None)]
        except OSError as e:
            return f"cannot resolve reply-url host {host!r} ({e.strerror or e})"
    for a in addrs:
        try:
            if ipaddress.ip_address(a).is_loopback:
                return (f"refusing loopback reply-url {host!r} "
                        "(pass --allow-loopback if that is really the bridge)")
        except ValueError:
            return f"reply-url host {host!r} resolved to something unusable: {a!r}"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, and say where it wanted to go.

    Not just the cross-origin ones: urllib turns a 301/302/303 POST into a GET, so
    following one would drop the reply body while quite possibly returning 200 from the
    new location — a reply that reads as delivered and never arrived. A reply endpoint
    has no business redirecting, so treat it as an error worth seeing.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refusing to follow a redirect to {newurl} (a reply endpoint must not "
            "redirect: the body would not be re-sent)", headers, fp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="reply-url from the message footer")
    ap.add_argument("--req", required=True, help="req_id from the message footer")
    ap.add_argument("--content", default=None,
                    help="reply body (default: read from stdin, which also keeps it "
                         "out of the process list)")
    ap.add_argument("--allow-loopback", action="store_true",
                    help="permit a reply-url on 127.0.0.1/::1 (local bridge, tests)")
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args()

    parts = urlsplit(a.url)
    if parts.scheme not in ALLOWED_SCHEMES:
        return _fail(f"refusing reply-url scheme {parts.scheme!r} (only http/https)")
    if not parts.hostname:
        return _fail("reply-url has no host")
    if not a.allow_loopback:
        why = _host_check(parts.hostname)
        if why:
            return _fail(why)

    content = a.content if a.content is not None else sys.stdin.read()
    if not content.strip():
        return _fail("empty reply body")

    body = json.dumps({"req_id": a.req, "content": content}).encode("utf-8")
    req = urllib.request.Request(a.url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=a.timeout) as r:
            print(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # Reached the bridge; it said no. Worth distinguishing from "unreachable",
        # because the two need completely different things done about them. `reason`
        # matters as much as the body: a refusal raised by the redirect handler above
        # carries its explanation there, and printing only the code hides it.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
        except Exception:
            pass
        return _fail(" ".join(x for x in (f"bridge rejected the reply: HTTP {e.code}",
                                          str(e.reason or ""), detail) if x).strip(), 1)
    except Exception as e:  # noqa: BLE001
        return _fail(f"cannot reach bridge: {e}", 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
