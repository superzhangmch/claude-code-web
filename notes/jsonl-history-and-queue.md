# Claude Code JSONL: conversation backbone & queued-message lifecycle

Reverse-engineered notes on how Claude Code writes its session `.jsonl`, focused
on (a) how the whole conversation is linked, and (b) how a queued message is
recorded. Reference for `cc_web.py`'s transcript + queued handling. Format is an
internal, evolving log (NOT a stable API) — versions differ; findings below hold
across the 2.1.x range we tested, noting version-specific deltas.

Run the companion `analyze_jsonl.py` against any `~/.claude/projects/<proj>/<sid>.jsonl`
to re-verify everything here.

## Entry identity & the conversation tree

- Every **conversation** entry (`user` / `assistant` / `attachment`) has a
  `uuid` (its node id) and a `parentUuid` (its parent) → the conversation is a
  tree/chain.
- Bookkeeping entries have **no `uuid`**: `queue-operation`,
  `file-history-snapshot`, `custom-title`, `mode`, `last-prompt`. (This is why a
  queued `enqueue` can't be matched by id.)
- **`uuid` is stable and never reused for a different message.** Duplicate uuids
  DO appear, but they are *verbatim re-logs* (same uuid + timestamp + parentUuid
  + content) written by compaction — the SAME logical message copied forward, not
  a new message recycling an id. (Verified: for every duplicate uuid, both copies
  had identical type+content and timestamp.)

## Compaction splits the chain (multiple "roots")

At ~1M tokens Claude Code auto-compacts. Each compaction writes:

```json
{"type":"system","subtype":"compact_boundary","content":"Conversation compacted",
 "parentUuid":null,                       // ← re-roots: breaks the parentUuid chain
 "logicalParentUuid":"<pre-compaction node>",  // ← soft link back across the boundary
 "compactMetadata":{"trigger":"auto","preTokens":~1000000, ...}}
```

So a session file is a **forest**: one real root (the first `isMeta` user entry,
e.g. `<local-command-caveat>`) plus one `compact_boundary` root per compaction.
Walking `parentUuid` up from the last entry only reaches the *current* segment.

**Full backbone = walk `parentUuid` up, and at each `compact_boundary`
(parentUuid=null) jump to `logicalParentUuid`, repeat.** This reaches the very
first message. (Verified on a real 5-segment session: 4 jumps, 11482 nodes,
ended exactly at the first `isMeta` user entry.) Off-backbone nodes = compaction
re-log copies + `isSidechain` sub-agent (Task) branches.

## Queued-message lifecycle

Typing while Claude is busy queues the message. Records left, in order:

| entry | fields | meaning |
|---|---|---|
| `queue-operation` `enqueue` | `content`, no uuid | queued (pending) |
| `queue-operation` `dequeue` | **empty** (no content/target), no uuid | removed from queue to be sent — but says NOTHING about which item; **always empty in every version seen** |
| `queue-operation` `remove` | empty pre-2.1.207; **carries `content` in 2.1.207+** | also "removed from queue" — **NOT a cancel** (confirmed: a message the user did NOT cancel still logged `remove`) |
| `queue-operation` `popAll` | `content` | queue flushed |
| `attachment` → `attachment.type=="queued_command"` | `uuid`, `parentUuid`, `origin.kind:"human"`, `prompt` (=content), `timestamp` (= the enqueue's ts) | **THE delivered human message** — Claude's reply chains to its `uuid` via `parentUuid`. Authoritative "this queued msg was submitted." |

Key consequences:
- **`dequeue`/`remove` cannot precisely identify which enqueue** they consumed
  (no content in old versions, no target id ever; queue depth >1 ~13% of the
  time → ambiguous). So they are NOT used for matching.
- The delivered form of a sent queued msg is **EITHER** a `queued_command`
  attachment **(~80%)** OR a plain `type:"user"` turn **(~20%, same content, a few
  seconds after the enqueue)**. Measured consistent across v2.1.193 (80/20) and
  v2.1.218 (81/18). So BOTH forms must be recognised — using only one leaves the
  other's placeholders stale ("Queued" AND the delivered msg = a double).
- `queued_command.origin.kind` may be non-human (~7%); only treat `human` ones.
- A `queued_command`'s inner `timestamp` is the **queue** time — it sits ~1ms from
  its enqueue's `timestamp` (measured: max |Δt| = 1.0ms across a full session;
  qcmd is enqueue − 1ms). It is NOT exactly equal, so an exact-match key fails;
  match by **content + |Δt| < 100ms**. A user-turn delivery instead carries the
  real DELIVERY time (seconds after the enqueue, e.g. 3s), so it shares no
  timestamp — it must be matched by **content + position**.
- **A user-turn delivery is preceded by a `[Request interrupted by user]` turn**:
  submitting a queued msg interrupts Claude's running turn, logging that marker
  (a `type:user` entry) right before the real delivery. It is NOT a real user
  input and must be ignored in matching (else it looks like an intervening
  direct turn and breaks the pairing).

### How cc_web uses this (final design)
- Server (`_filter_entries`): render `enqueue` (human, untagged) as a "Queued"
  placeholder; render `queued_command` (human) as the delivered message
  (`_qcmd`); real user turns render normally; drop dequeue/remove/popAll and
  tagged enqueues. **No dedup server-side** — all matching is client-side.
- Client (`computePairedQueued`), two-tier, HIDE the placeholder when delivered:
  1. **qcmd** — pair by **content + nearest timestamp (|Δt|<100ms)**.
     Order-independent; uniquely disambiguates repeated content (a distant
     same-text qcmd is seconds–hours off, never within 100ms).
  2. **plain user-turn** (only for enqueues qcmd didn't claim) — pair by
     **content + STRICT position**: the oldest same-content pending enqueue, but a
     *real unpaired* user input in between breaks the chain (a later same-text turn
     after a genuine direct message is a RE-TYPE, not a delivery).
     `[Request interrupted by user]` markers + meta/caveat turns are excluded from
     being either a delivery or a chain-breaker.
- Verified on a real 264-enqueue session: 203 qcmd + 50 strict-turn matched, 11
  genuinely pending (mostly `[Image #N]` msgs whose text can't match), **0 doubles**
  and **0 false-clears**.
- **All matching is client-side.** (Positional user-turn matching can't be safely
  split across a batch boundary — server-remove + client-re-pair could drop the
  wrong one.)

## Does cc_web's line-by-line (file `_idx` order) reconstruct history like the backbone?

Compared, on a real 5-compaction session:

- **Nothing missing** — every backbone conversation message is rendered (0 missing).
- **Order identical** to the backbone (after dedup).
- **Duplicates**: 2086 messages rendered twice (compaction verbatim re-logs). The
  two copies are ~2700–3800 lines apart, so a normal tail window never holds both
  → no visible double in practice; only if you "load earlier" past a compaction.
  A client-side uuid-dedup (render each uuid once) would eliminate it.
- **205 "extra"**: real messages on a pre-compaction branch the single backbone
  path skips — line-by-line shows them (arguably *more* complete, not wrong).

**Verdict:** line-by-line preserves the full backbone + correct order + nothing
lost. The only true imperfection is compaction re-log duplicates (low practical
impact). It does NOT try to walk the parentUuid/logicalParentUuid tree — that's
intentional: tree-walk drops ~88% of nodes from the tail (stops at the first
compaction) unless you also bridge `logicalParentUuid`, and has window/sidechain
edge cases; file order is simpler and, per the above, essentially faithful.
