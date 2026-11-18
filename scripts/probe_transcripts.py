"""Manual probe: can captions be fetched from this machine, and how do sketches compare?

Usage:
    python scripts/probe_transcripts.py "search words" [N]     # first N (default 4) search results
    python scripts/probe_transcripts.py VIDEO_ID VIDEO_ID ...   # explicit ids

Prints every pair's transcript overlap; pairs from different channels with a
high overlap are re-uploads of the same speech.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.search_engine import search_ytdlp  # noqa: E402
from core.transcript_match import compare_sketches, get_sketch  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    meta = {}
    if " " in args[0]:
        n = int(args[1]) if len(args) > 1 else 4
        for v in search_ytdlp(args[0], max_results=n):
            meta[v["video_id"]] = v
        ids = list(meta)
    else:
        ids = args
    sketches = {}
    for vid in ids:
        t0 = time.time()
        sketch, reason = get_sketch(vid)
        took = time.time() - t0
        m = meta.get(vid, {})
        label = f"{vid} [{m.get('channel_title', '')[:24]}] {m.get('duration_seconds', '')}s {m.get('title', '')[:50]}"
        if sketch:
            sketches[vid] = sketch
            print(f"{label}: lang={sketch['lang']} words={sketch['words']} "
                  f"hashes={len(sketch['hashes'])} ({took:.1f}s)")
        else:
            print(f"{label}: NO TRANSCRIPT - {reason} ({took:.1f}s)")
    vids = list(sketches)
    rows = []
    for i, a in enumerate(vids):
        for b in vids[i + 1:]:
            r = compare_sketches(sketches[a], sketches[b])
            rows.append((r["overlap"], r["shared"], a, b))
    rows.sort(reverse=True)
    print(f"\n{len(rows)} pairs, highest overlap first:")
    for overlap, shared, a, b in rows[:15]:
        ca, cb = meta.get(a, {}).get("channel_id"), meta.get(b, {}).get("channel_id")
        same = bool(ca) and ca == cb
        print(f"  {a} vs {b}: overlap={overlap} shared={shared}"
              f"{'  (same channel)' if same else ''}")
    if rows:
        low = [r[0] for r in rows if r[0] < 0.2]
        if low:
            print(f"pairs below 0.20: {len(low)}, max {max(low)}")


if __name__ == "__main__":
    main()
