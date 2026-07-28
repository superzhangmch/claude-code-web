#!/usr/bin/env python3.11
"""Analyze a Claude Code session .jsonl — verify the conversation backbone
(parentUuid + logicalParentUuid across compaction boundaries) and the queued-
message lifecycle. Companion to jsonl-history-and-queue.md.

Usage:
    python3.11 analyze_jsonl.py ~/.claude/projects/<proj>/<session-id>.jsonl

Reports:
  1. entry-id stats (uuid coverage; duplicate uuids = compaction re-logs)
  2. compaction roots (compact_boundary) and whether the full backbone reaches
     the first message
  3. queued-op counts + whether dequeue/remove carry content (version-dependent)
  4. queued_command (delivered human msg) coverage + (content,timestamp) uniqueness
  5. does file `_idx` order reproduce the backbone? (missing / extra / duplicates / order)
Read-only; nothing is written or sent.
"""
import json
import sys
from collections import Counter, defaultdict


def load(path):
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def uuid_first_index(rows):
    first = {}
    for i, e in enumerate(rows):
        u = e.get("uuid")
        if u and u not in first:
            first[u] = i
    return first


def is_convo(e):
    """A real conversation message a human reads (excludes meta / sidechain)."""
    return e.get("type") in ("user", "assistant") and not e.get("isSidechain") and not e.get("isMeta")


def backbone(rows, first):
    """Walk from the last uuid-entry up parentUuid, bridging logicalParentUuid at
    each compact_boundary (parentUuid=null). Returns entries first->last."""
    tail = [e for e in rows if e.get("uuid")]
    if not tail:
        return [], 0
    cur, seen, out, jumps = tail[-1], set(), [], 0
    while cur is not None:
        u = cur.get("uuid")
        if u in seen:
            break
        seen.add(u)
        out.append(cur)
        p = cur.get("parentUuid")
        if p is None:
            lp = cur.get("logicalParentUuid")
            if lp and lp in first:
                jumps += 1
                cur = rows[first[lp]]
                continue
            cur = None
        else:
            cur = rows[first[p]] if p in first else None
    return out[::-1], jumps


def qcmd_prompt(e):
    """(prompt, timestamp) if e is a human queued_command delivery, else (None, None)."""
    if e.get("type") != "attachment":
        return None, None
    a = e.get("attachment")
    if isinstance(a, dict) and a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
        p = a.get("prompt")
        if isinstance(p, str) and p.strip():
            return p.strip(), a.get("timestamp") or e.get("timestamp")
    return None, None


def main(path):
    rows = load(path)
    first = uuid_first_index(rows)
    print(f"file: {path}\nentries: {len(rows)}")

    # 1. id stats
    with_uuid = [e for e in rows if e.get("uuid")]
    dup = [u for u, c in Counter(e.get("uuid") for e in with_uuid).items() if c > 1]
    no_uuid_types = Counter(e.get("type") for e in rows if not e.get("uuid"))
    print(f"\n[ids] with uuid: {len(with_uuid)}  duplicate uuids (compaction re-logs): {len(dup)}")
    print(f"      no-uuid entry types: {dict(no_uuid_types)}")

    # 2. compaction + backbone
    roots = [e for e in with_uuid if e.get("parentUuid") is None]
    boundaries = [e for e in roots if e.get("subtype") == "compact_boundary"]
    bb, jumps = backbone(rows, first)
    print(f"\n[tree] roots(parentUuid=null): {len(roots)}  of which compact_boundary: {len(boundaries)}")
    if bb:
        head = bb[0]
        print(f"       backbone: {len(bb)} nodes, {jumps} logicalParentUuid jumps")
        print(f"       first backbone node: type={head.get('type')} isMeta={head.get('isMeta')}")

    # 3. queued ops
    ops = Counter(e.get("operation") for e in rows if e.get("type") == "queue-operation")
    op_content = Counter()
    for e in rows:
        if e.get("type") == "queue-operation" and (e.get("content") or "").strip():
            op_content[e.get("operation")] += 1
    print(f"\n[queue] ops: {dict(ops)}")
    print(f"        ops carrying content: {dict(op_content)}  (dequeue empty everywhere; remove gains content in 2.1.207+)")

    # 4. queued_command
    enq = [(e.get("content") or "").strip() for e in rows
           if e.get("type") == "queue-operation" and e.get("operation") == "enqueue" and (e.get("content") or "").strip()]
    qc = [qcmd_prompt(e) for e in rows]
    qc = [(p, ts) for p, ts in qc if p]
    keys = [(c, e.get("timestamp")) for e in rows
            if e.get("type") == "queue-operation" and e.get("operation") == "enqueue" and (c := (e.get("content") or "").strip())]
    collisions = sum(c - 1 for c in Counter(keys).values() if c > 1)
    print(f"\n[queued_command] enqueues: {len(enq)}  human queued_command deliveries: {len(qc)}")
    print(f"        (content,timestamp) collisions among enqueues: {collisions}  (0 → exact match key)")

    # 5. line-by-line vs backbone
    bb_ids = [e.get("uuid") for e in bb if is_convo(e)]
    lb_ids = [e.get("uuid") for e in rows if is_convo(e)]
    bbset, lbset = set(bb_ids), set(lb_ids)
    dups_lb = sum(c - 1 for c in Counter(lb_ids).values() if c > 1)
    seen = set()
    lb_dedup = [u for u in lb_ids if not (u in seen or seen.add(u))]
    common = [u for u in lb_dedup if u in bbset]
    bb_common = [u for u in bb_ids if u in lbset]
    print(f"\n[reconstruct] backbone convo msgs: {len(bb_ids)}  line-by-line: {len(lb_ids)} (distinct {len(lbset)})")
    print(f"        MISSING (backbone not rendered): {len(bbset - lbset)}")
    print(f"        EXTRA (rendered, off-backbone): {len(lbset - bbset)}")
    print(f"        duplicate renders (compaction re-log): {dups_lb}")
    print(f"        order identical on common msgs: {common == bb_common}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
