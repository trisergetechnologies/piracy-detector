"""Detection benchmark: recall / precision on labelled URLs.

Inputs (CSV with header):
    data/known_pirated.csv   positives (confirmed pirated copies)
    data/known_clean.csv     negatives (legitimate videos/posts that must NOT be flagged)

Columns:
    platform            youtube | telegram
    url                 https://www.youtube.com/watch?v=...  or  https://t.me/<channel>/<msg id>
    entity_name         rights holder (as typed in the dashboard)
    official_channels   official YouTube channels, ';'-separated (@handle, URL or UC id) - needs an API key
    official_videos     official video URLs/ids, ';'-separated - works without an API key
    note                free text

What is measured:
  * matching  - given the URL, do the matching stages (title, duration, transcript,
                audio) flag it? The LLM is left out so the result is repeatable.
  * scan      - did real scans find it? Read from the detected_videos collection and
                the per-run funnel logs; for each miss the stage that lost it is shown.

Run:
    python scripts/benchmark.py
    python scripts/benchmark.py --pirated data/sample_pirated.csv --clean data/sample_clean.csv --no-audio
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
from core.catalog import ingest_official_catalog, normalize_title, title_tokens  # noqa: E402
from core.fingerprint_match import run_fingerprint_matching  # noqa: E402
from core.heuristics import annotate_and_prioritize  # noqa: E402
from core.scorer import piracy_evidence  # noqa: E402

_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")
LLM_OFF = {"classification": "LEGITIMATE", "confidence": 0, "llm_error": True}


def video_id_of(ref: str) -> str:
    ref = (ref or "").strip()
    m = _YT_ID.search(ref)
    return m.group(1) if m else (ref if re.fullmatch(r"[\w-]{11}", ref) else "")


def read_rows(path: str, label: int) -> List[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [dict(r) for r in csv.DictReader(fh) if (r.get("url") or "").strip()]
    for r in rows:
        r["label"] = label
        r["platform"] = (r.get("platform") or "youtube").strip().lower()
    return rows


def ytdlp_meta(video_id: str) -> Optional[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:
        print(f"  ! {video_id}: {str(exc)[:100]}")
        return None
    if not info:
        return None
    return {
        "video_id": video_id,
        "title": info.get("title") or "",
        "channel_id": info.get("channel_id") or "",
        "channel_title": info.get("channel") or info.get("uploader") or "",
        "description": info.get("description") or "",
        "duration_seconds": int(info.get("duration") or 0),
        "source": "benchmark",
    }


def build_catalog(channels: List[str], videos: List[str]) -> dict:
    catalog: Dict[str, Any] = {"video_ids": set(), "channel_ids": set(), "entries": [],
                               "title_index": [], "duration_map": {}, "unresolved": []}
    if channels:
        if config.YOUTUBE_API_KEYS:
            from core.youtube_client import YouTubeClient

            catalog = ingest_official_catalog(YouTubeClient(), official_channel_ids=channels)
        else:
            print("  ! official_channels given but YOUTUBE_API_KEYS is not set - use official_videos")
    for ref in videos:
        vid = video_id_of(ref)
        if not vid or vid in catalog["video_ids"]:
            continue
        meta = ytdlp_meta(vid)
        if not meta:
            catalog["unresolved"].append(ref)
            continue
        catalog["video_ids"].add(vid)
        if meta["channel_id"]:
            catalog["channel_ids"].add(meta["channel_id"])
        catalog["entries"].append(meta)
        catalog["duration_map"][vid] = meta["duration_seconds"]
        catalog["title_index"].append({
            "video_id": vid, "title": meta["title"], "norm_title": normalize_title(meta["title"]),
            "tokens": title_tokens(meta["title"]), "channel_id": meta["channel_id"],
            "duration_seconds": meta["duration_seconds"],
        })
    return catalog


def scan_trace() -> Tuple[Dict[str, str], Dict[str, str]]:
    """What real scans did with each id/url -> ({id_or_url: 'flagged'}, {id: 'stage:reason'})."""
    flagged: Dict[str, str] = {}
    dropped: Dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(config.FUNNEL_LOG_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                log = json.load(fh)
        except Exception:
            continue
        for d in log.get("drops") or []:
            if d.get("final", True):
                dropped[str(d.get("id"))] = f"{d.get('stage')}:{d.get('reason')}"
    try:
        import db

        col = db._col("detected_videos")
        rows = col.find({}, {"_id": 0, "video_id": 1, "url": 1}) if col is not None else []
        for r in rows:
            for key in (r.get("video_id"), r.get("url")):
                if key:
                    flagged[str(key)] = "flagged"
    except Exception as exc:
        print(f"  ! could not read detected_videos: {exc}")
    return flagged, dropped


def methods_of(v: dict) -> List[str]:
    out = []
    signals = (v.get("heuristic") or {}).get("signals") or []
    if any(s.startswith("catalog_title_match") for s in signals):
        out.append("title")
    if any(s.startswith(("duration_match_official", "duration_equals_official")) for s in signals):
        out.append("duration")
    fm = v.get("fingerprint_match") or {}
    if fm.get("is_match"):
        out.append("transcript" if fm.get("backend") == "transcript" else "audio")
        if fm.get("audio_similarity"):
            out.append("audio")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pirated", default=os.path.join(ROOT, "data", "known_pirated.csv"))
    ap.add_argument("--clean", default=os.path.join(ROOT, "data", "known_clean.csv"))
    ap.add_argument("--min-confidence", type=int, default=config.MIN_CONFIDENCE)
    ap.add_argument("--no-audio", action="store_true", help="skip audio fingerprinting (no downloads)")
    ap.add_argument("--out", default=config.BENCHMARK_RESULT_FILE)
    args = ap.parse_args()

    rows = read_rows(args.pirated, 1) + read_rows(args.clean, 0)
    if not rows:
        print(f"No benchmark data. Create {args.pirated} and {args.clean} (columns in the docstring).")
        sys.exit(2)

    flagged, dropped = scan_trace()
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("entity_name") or "", r.get("official_channels") or "",
                r.get("official_videos") or "")].append(r)

    for (entity, channels, videos), items in groups.items():
        yt_items = [r for r in items if r["platform"] == "youtube"]
        cands: List[dict] = []
        if yt_items:
            print(f"\n[{entity or 'no entity'}] building official catalog...")
            catalog = build_catalog([c for c in channels.split(";") if c.strip()],
                                    [v for v in videos.split(";") if v.strip()])
            print(f"  catalog: {len(catalog['entries'])} official videos")
            for r in yt_items:
                vid = video_id_of(r["url"])
                r["id"] = vid
                meta = ytdlp_meta(vid) if vid else None
                if not meta:
                    r.update(tier=None, confidence=0, methods=[], lost_at="inaccessible (deleted/private?)")
                    continue
                r["_cand"] = meta
                cands.append(meta)
            # Official uploads are kept in (drop_official=False) so a negative that is
            # an official video is still scored like any other.
            annotate_and_prioritize(cands, catalog=catalog, drop_official=False, entity_name=entity)
            if catalog["entries"] and cands:
                run_fingerprint_matching(catalog, cands, max_candidates=len(cands),
                                         audio=not args.no_audio and config.FINGERPRINT_ENABLED)
            for r in yt_items:
                v = r.pop("_cand", None)
                if v is None:
                    continue
                official = (v.get("heuristic") or {}).get("is_official")
                ev = {"tier": None, "confidence": 0, "reason": "official upload"} if official \
                    else piracy_evidence(v, LLM_OFF)
                r.update(tier=ev["tier"], confidence=ev["confidence"], methods=methods_of(v),
                         reason=ev["reason"])
        for r in items:
            if r["platform"] != "youtube":
                r["id"] = r["url"].strip().rstrip("/")
                r.update(tier=None, confidence=0, methods=[])

    # ---- outcome per row
    for r in rows:
        key = r.get("id") or ""
        r["scan"] = "flagged" if key in flagged else dropped.get(key, "never_discovered")
        if r["platform"] == "youtube":
            r["predicted"] = bool(r.get("tier")) and int(r.get("confidence") or 0) >= args.min_confidence
        else:
            r["predicted"] = r["scan"] == "flagged"   # Telegram posts are only checked via real scans
        if r["label"] == 1 and not r["predicted"]:
            if r.get("lost_at"):
                pass
            elif r["platform"] == "youtube" and not r.get("tier"):
                r["lost_at"] = "matching: no title/duration/transcript/audio evidence"
            elif r["platform"] == "youtube":
                r["lost_at"] = f"threshold: {r['tier']} tier, confidence {r['confidence']} < {args.min_confidence}"
            else:
                r["lost_at"] = f"scan: {r['scan']}"

    def metrics(sel: List[dict], key: str = "predicted") -> dict:
        pos = [r for r in sel if r["label"] == 1]
        neg = [r for r in sel if r["label"] == 0]
        tp = sum(1 for r in pos if r[key])
        fp = sum(1 for r in neg if r[key])
        return {
            "positives": len(pos), "negatives": len(neg), "tp": tp, "fp": fp,
            "recall": round(tp / len(pos), 3) if pos else None,
            "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        }

    for r in rows:
        r["scan_flagged"] = r["scan"] == "flagged"
    result: Dict[str, Any] = {
        "run_at": datetime.utcnow().isoformat() + "Z",
        "min_confidence": args.min_confidence,
        **metrics(rows),
        "per_platform": {p: metrics([r for r in rows if r["platform"] == p])
                         for p in sorted({r["platform"] for r in rows})},
        "per_method": {},
        "per_tier": {},
        "scan_end_to_end": metrics(rows, "scan_flagged"),
    }
    for m in ("title", "duration", "transcript", "audio"):
        for r in rows:
            r[f"m_{m}"] = m in (r.get("methods") or [])
        result["per_method"][m] = metrics([r for r in rows if r["platform"] == "youtube"], f"m_{m}")
    for t in ("confirmed", "likely", "weak"):
        for r in rows:
            r[f"t_{t}"] = r.get("tier") == t
        result["per_tier"][t] = metrics([r for r in rows if r["platform"] == "youtube"], f"t_{t}")
    result["misses"] = [{"url": r["url"], "platform": r["platform"], "lost_at": r.get("lost_at"),
                         "scan": r["scan"]} for r in rows if r["label"] == 1 and not r["predicted"]]
    result["false_positives"] = [{"url": r["url"], "tier": r.get("tier"), "confidence": r.get("confidence"),
                                  "reason": r.get("reason")} for r in rows if r["label"] == 0 and r["predicted"]]

    def line(name: str, m: dict) -> str:
        return (f"  {name:<22} recall {m['recall']!s:<6} precision {m['precision']!s:<6} "
                f"(TP {m['tp']}/{m['positives']}, FP {m['fp']}/{m['negatives']})")

    print("\n=== MATCHING (given the URL) ===")
    print(line("overall", result))
    for p, m in result["per_platform"].items():
        print(line(p, m))
    print("  -- per method (YouTube; a row can be caught by several) --")
    for name, m in result["per_method"].items():
        print(line(name, m))
    print("  -- per tier --")
    for name, m in result["per_tier"].items():
        print(line(name, m))
    print("\n=== REAL SCANS (detected_videos + funnel logs) ===")
    print(line("found by a scan", result["scan_end_to_end"]))
    if result["misses"]:
        print("\nMisses and the stage that lost them:")
        for m in result["misses"]:
            print(f"  {m['url']}\n      matching: {m['lost_at']}\n      scans:    {m['scan']}")
    if result["false_positives"]:
        print("\nFalse positives:")
        for f in result["false_positives"]:
            print(f"  {f['url']}  [{f['tier']} {f['confidence']}] {f['reason']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()
