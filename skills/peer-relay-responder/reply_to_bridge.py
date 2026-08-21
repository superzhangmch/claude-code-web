#!/usr/bin/env python3
"""Reply to an EXTERNAL relayed request by POSTing your answer to the relay
bridge. The reply DESTINATION is a PRE-AGREED local setting, NOT something the
incoming message carries — so a forged message can never redirect your reply
elsewhere or turn this session into an exfiltration / SSRF channel. The message
carries only the one-time `req=<req_id>`; this script already knows where to send.

    reply_to_bridge.py --req <req_id> <<'EOF'
    your reply text
    EOF

Destination resolution (first wins):
  1. --url <url>            explicit override (rare; e.g. tests). Treated as
                            untrusted → the http/https + loopback + no-redirect
                            checks below apply.
  2. $PEER_RELAY_REPLY_URL  operator config (env). Trusted (local, pre-agreed).
  3. built-in default       http://127.0.0.1:8790/reply. Trusted.

Because the pre-agreed destination is set by the machine's owner, not by the
message, loopback (a local bridge on 127.0.0.1) is fine for the config/default
path with no flag. The checks below only guard an explicit --url override:

  * only http/https — no file://, ftp://, etc.;
  * loopback refused unless --allow-loopback (a forged --url can't be aimed at a
    local-only service);
  * redirects refused outright (urllib turns a 302 POST into a GET and silently
    drops the reply body).

Deliberately NOT blocked: private/LAN/tailnet addresses — a legitimate bridge
often lives on a tailnet.

req_id is a one-time capability: only the session that received this request
knows it, so the bridge accepts the reply on that basis (no token).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_REPLY_URL = "http://127.0.0.1:8790/reply"


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
    ap.add_argument("--req", required=True, help="req_id from the message footer")
    ap.add_argument("--url", default=None,
                    help="explicit reply-url override (rare). Untrusted: http/https "
                         "+ loopback + no-redirect checks apply. Default: "
                         "$PEER_RELAY_REPLY_URL or the built-in local bridge url.")
    ap.add_argument("--content", default=None,
                    help="reply body (default: read from stdin, which also keeps it "
                         "out of the process list)")
    ap.add_argument("--allow-loopback", action="store_true",
                    help="permit a loopback --url override (127.0.0.1/::1)")
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args()

    # Pre-agreed destination unless explicitly overridden. Config/default is set by
    # the machine owner (trusted); only an explicit --url is treated as untrusted.
    url = a.url or os.environ.get("PEER_RELAY_REPLY_URL") or DEFAULT_REPLY_URL
    trusted_source = a.url is None  # came from env/default, not the command line

    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        return _fail(f"refusing reply-url scheme {parts.scheme!r} (only http/https)")
    if not parts.hostname:
        return _fail("reply-url has no host")
    if not trusted_source and not a.allow_loopback:
        why = _host_check(parts.hostname)
        if why:
            return _fail(why)

    content = a.content if a.content is not None else sys.stdin.read()
    if not content.strip():
        return _fail("empty reply body")

    body = json.dumps({"req_id": a.req, "content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
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
