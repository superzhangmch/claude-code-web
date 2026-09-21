# TODO

Things decided but not built. One heading each, with enough of the reasoning that the
decision does not have to be made twice.

## Find a request INSIDE a session (server-side), and jump to it

**State today.** The 🔍 in the ⚙ menu opens the find bar, which filters what is already
loaded in the browser. Its tooltip claims something else:

    title="在整个 session 里搜(不只是已载入的部分)…"

That is not true, and until this is built the tooltip should be corrected rather than
left promising it.

**What is already there.** `/api/search` is a full backend search across ALL transcripts:
ripgrep + a JSON confirm step so keyword mode matches only genuine user requests (not
claude repeating your words back), `a & b` / `a | b`, cwd/size/days filters, and an
embedding mode. It answers "which session mentioned this". The missing half is
"which round, and take me there".

**Why it matters.** Found on 2026-09-20 looking for one request in a 949-round, 176 MB
session: the match was at round 901, the page had the tail loaded, so find could not see
it. Getting there meant paging ~48 rounds back. On a phone, on 5G, that is the whole
cost of the question — while the answer is a few hundred bytes.

**Shape.** Three small pieces, no new concept:

1. `/api/search` gains a within-session mode returning `[{_idx, _round, ts, snippet}]`
   instead of a session list. The rg pass and the only-real-requests confirm are reused
   as they stand.
2. The find bar gains a "whole session" path — offered when the local pass finds nothing.
   Results list as `r901 · 09-19 00:17 · …snippet…`; tapping one loads that round through
   the existing `round_at` parameter and scrolls to it.
3. A `#s=<sid>&i=<idx>` anchor, so a hit can be linked. Today the URL only carries the
   session (`#s=…`), which is why a search result can be described but not handed over.

**The one open question.** `_round` is cumulative from the top of the file, so a hit's
round number needs either a full parse (54,899 lines ≈ 2 s — fine for an explicit
search) or a way to reuse the offsets `JsonlCache` already tracks. Start with the parse;
optimise only if it is actually slow.

**Not to be built:** a separate "locate a message" entry point. There are already two
doors to searching (the picker's search, the in-session find); a third would be the
mistake the streaming-search endpoint was deleted for — more ways to look for something
than there are questions being asked.
