"""Transcript (caption) similarity vs the official catalog.

Why: titles, thumbnails and descriptions are trivially changed by re-uploaders;
the spoken words are not. YouTube publishes captions (manual or auto-generated)
as public metadata, so no media download is needed.

A transcript is reduced to a "sketch": hashes of 4-word shingles, sampled
deterministically (hash % N == 0) so two copies of the same speech keep the
same hashes. Overlap is measured as containment in the smaller sketch, which
keeps partial copies (clips) and compilations detectable. Each hash keeps its
first timestamp so the matched span can be reported.

Sketches are cached as small JSON files under FINGERPRINT_CACHE_DIR/transcripts.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from config import (
    FINGERPRINT_CACHE_DIR,
    TRANSCRIPT_CONFIRMED_OVERLAP,
    TRANSCRIPT_LANGS,
    TRANSCRIPT_LIKELY_OVERLAP,
    TRANSCRIPT_MAX_CANDIDATES,
    TRANSCRIPT_BACKOFF_SECONDS,
    TRANSCRIPT_MAX_CATALOG,
    TRANSCRIPT_MIN_INTERVAL_SECONDS,
    TRANSCRIPT_MIN_SHARED,
    TRANSCRIPT_NEGATIVE_TTL_HOURS,
    TRANSCRIPT_WORKERS,
)
from core import funnel

logger = logging.getLogger(__name__)

Segment = Tuple[float, str]  # (start seconds, text)
ProgressCb = Optional[Callable[[str], None]]

SHINGLE_WORDS = 4
SAMPLE_MOD = 8  # keep ~1 in 8 shingles
_WORD = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)
_VTT_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s+-->")
_TAG = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Sketching (pure functions – unit tested)
# ---------------------------------------------------------------------------


def tokenize(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if not w.startswith("_")]


def build_sketch(segments: List[Segment], lang: str = "") -> Dict[str, Any]:
    """segments -> {"lang", "words", "hashes": {hex_hash: first_time_seconds}}."""
    words: List[Tuple[str, float]] = []
    for start, text in segments:
        for tok in tokenize(text):
            words.append((tok, float(start)))

    hashes: Dict[str, float] = {}
    for i in range(len(words) - SHINGLE_WORDS + 1):
        shingle = " ".join(w for w, _ in words[i : i + SHINGLE_WORDS])
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        if int.from_bytes(digest, "big") % SAMPLE_MOD:
            continue
        hashes.setdefault(digest.hex(), round(words[i][1], 1))
    return {"lang": lang, "words": len(words), "hashes": hashes}


def compare_sketches(cand: Dict[str, Any], official: Dict[str, Any]) -> Dict[str, Any]:
    """Containment of the smaller sketch in the larger one, plus matched spans."""
    ch, oh = cand.get("hashes") or {}, official.get("hashes") or {}
    smaller = min(len(ch), len(oh))
    if not smaller:
        return {"overlap": 0.0, "shared": 0}
    shared = ch.keys() & oh.keys()
    out: Dict[str, Any] = {
        "overlap": round(len(shared) / smaller, 4),
        "shared": len(shared),
    }
    if shared:
        ct = sorted(ch[h] for h in shared)
        ot = sorted(oh[h] for h in shared)
        out["candidate_span"] = [ct[0], ct[-1]]
        out["official_span"] = [ot[0], ot[-1]]
    return out


def tier_for(overlap: float, shared: int) -> Optional[str]:
    if shared < TRANSCRIPT_MIN_SHARED:
        return None
    if overlap >= TRANSCRIPT_CONFIRMED_OVERLAP:
        return "confirmed"
    if overlap >= TRANSCRIPT_LIKELY_OVERLAP:
        return "likely"
    return None


# ---------------------------------------------------------------------------
# Caption parsing
# ---------------------------------------------------------------------------


def parse_json3(raw: str) -> List[Segment]:
    segs: List[Segment] = []
    try:
        data = json.loads(raw)
    except ValueError:
        return segs
    for ev in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if text:
            segs.append((float(ev.get("tStartMs") or 0) / 1000.0, text))
    return segs


def parse_vtt(raw: str) -> List[Segment]:
    """VTT parser. Auto-caption VTT repeats rolling lines, so duplicates are dropped."""
    segs: List[Segment] = []
    start = 0.0
    last = ""
    for line in raw.splitlines():
        m = _VTT_TS.search(line)
        if m:
            h, mi, s, ms = (int(x) for x in m.groups())
            start = h * 3600 + mi * 60 + s + ms / 1000.0
            continue
        text = _TAG.sub("", line).strip()
        if not text or text == last or text.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        last = text
        segs.append((start, text))
    return segs


def _pick_track(info: dict) -> Tuple[Optional[dict], str, str]:
    """Choose a caption track -> (format_entry, lang, kind)."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def _fmt(tracks: List[dict]) -> Optional[dict]:
        for ext in ("json3", "vtt"):
            for t in tracks or []:
                if t.get("ext") == ext and t.get("url"):
                    return t
        return None

    # 1. Auto captions in the ORIGINAL spoken language ("hi-orig"): same speech
    #    gives the same text on both the official video and a copy.
    for lang, tracks in auto.items():
        if lang.endswith("-orig"):
            f = _fmt(tracks)
            if f:
                return f, lang[:-5], "asr"
    # 2. Uploader-provided subtitles, then auto captions, in preferred languages.
    for source, kind in ((subs, "manual"), (auto, "asr")):
        for want in TRANSCRIPT_LANGS:
            for lang, tracks in source.items():
                if lang == want or lang.startswith(want + "-"):
                    f = _fmt(tracks)
                    if f:
                        return f, want, kind
    # 3. Any uploader-provided subtitle track.
    for lang, tracks in subs.items():
        if lang != "live_chat":
            f = _fmt(tracks)
            if f:
                return f, lang.split("-")[0], "manual"
    return None, "", ""


# YouTube rate-limits caption downloads per IP (HTTP 429 after a burst). Requests
# are paced, and after repeated 429s fetching pauses; whatever was fetched stays
# cached on disk, so later scans continue where this one stopped.
_limit = {"streak": 0, "paused_until": 0.0, "last": 0.0, "hits": 0}
_pace_lock = threading.Lock()


def rate_limited() -> bool:
    return time.time() < _limit["paused_until"]


def _pace() -> None:
    with _pace_lock:
        wait = _limit["last"] + TRANSCRIPT_MIN_INTERVAL_SECONDS - time.time()
        if wait > 0:
            time.sleep(wait)
        _limit["last"] = time.time()


def _note_429() -> None:
    _limit["streak"] += 1
    _limit["hits"] += 1
    if _limit["streak"] >= 3 and not rate_limited():
        _limit["paused_until"] = time.time() + TRANSCRIPT_BACKOFF_SECONDS
        logger.warning("Caption downloads rate-limited by YouTube (HTTP 429); pausing %ds",
                       TRANSCRIPT_BACKOFF_SECONDS)
        funnel.current().error(
            "transcript_rate_limited",
            f"YouTube returned HTTP 429 for captions; paused {TRANSCRIPT_BACKOFF_SECONDS}s",
        )


def fetch_segments(video_id: str) -> Tuple[List[Segment], str, str]:
    """Return (segments, lang, reason). reason is '' on success."""
    try:
        import yt_dlp
    except ImportError:
        return [], "", "yt_dlp_missing"
    if rate_limited():
        return [], "", "rate_limited"

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as exc:
        return [], "", f"video_inaccessible: {str(exc)[:120]}"
    if not info:
        return [], "", "video_inaccessible"

    track, lang, _kind = _pick_track(info)
    if not track:
        return [], "", "no_captions"
    if rate_limited():
        return [], lang, "rate_limited"
    try:
        _pace()
        resp = requests.get(track["url"], timeout=20)
        if resp.status_code == 429:
            _note_429()
            return [], lang, "rate_limited"
        _limit["streak"] = 0
        if resp.status_code != 200 or not resp.text.strip():
            return [], lang, f"caption_fetch_http_{resp.status_code}"
        segs = parse_json3(resp.text) if track.get("ext") == "json3" else parse_vtt(resp.text)
    except Exception as exc:
        return [], lang, f"caption_fetch_error: {str(exc)[:120]}"
    if not segs:
        return [], lang, "caption_empty"
    return segs, lang, ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_path(video_id: str) -> Path:
    root = Path(FINGERPRINT_CACHE_DIR) / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{video_id}.json"


def get_sketch(video_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Cached sketch for a video -> (sketch | None, reason)."""
    path = _cache_path(video_id)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("hashes"):
                return cached, ""
            age_h = (time.time() - float(cached.get("ts") or 0)) / 3600.0
            if age_h < TRANSCRIPT_NEGATIVE_TTL_HOURS:
                return None, cached.get("reason") or "no_captions"
        except Exception:
            pass

    segs, lang, reason = fetch_segments(video_id)
    if segs:
        sketch = build_sketch(segs, lang)
        if len(sketch["hashes"]) < TRANSCRIPT_MIN_SHARED:
            segs, reason = [], "transcript_too_short"
        else:
            try:
                path.write_text(json.dumps(sketch), encoding="utf-8")
            except OSError as exc:
                logger.warning("Transcript cache write failed for %s: %s", video_id, exc)
            return sketch, ""
    # Only cache stable negatives; transient errors are retried next scan.
    if reason in ("no_captions", "caption_empty", "transcript_too_short"):
        try:
            path.write_text(json.dumps({"reason": reason, "ts": time.time()}), encoding="utf-8")
        except OSError:
            pass
    return None, reason or "no_captions"


def _get_many(video_ids: List[str]) -> Dict[str, Tuple[Optional[dict], str]]:
    if not video_ids:
        return {}
    with ThreadPoolExecutor(max_workers=max(TRANSCRIPT_WORKERS, 1)) as pool:
        futures = {
            vid: pool.submit(contextvars.copy_context().run, get_sketch, vid) for vid in video_ids
        }
        out: Dict[str, Tuple[Optional[dict], str]] = {}
        for vid, fut in futures.items():
            try:
                out[vid] = fut.result()
            except Exception as exc:
                out[vid] = (None, f"transcript_error: {str(exc)[:120]}")
        return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_sketch(
    sketch: Dict[str, Any], catalog_sketches: Dict[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Best official video for a candidate sketch (None if nothing shared)."""
    best: Optional[Dict[str, Any]] = None
    for off_vid, off in catalog_sketches.items():
        res = compare_sketches(sketch, off)
        if res["shared"] and (best is None or res["overlap"] > best["overlap"]):
            best = {**res, "official_video_id": off_vid}
    return best


def _catalog_order(catalog: dict, candidates: List[dict]) -> List[dict]:
    """Official videos whose titles/durations already match a candidate go first."""
    wanted: List[str] = []
    for v in candidates:
        h = v.get("heuristic") or {}
        cm = h.get("catalog_match") or {}
        if cm.get("official_video_id"):
            wanted.append(cm["official_video_id"])
        for sig in h.get("signals") or []:
            if sig.startswith("duration_equals_official:"):
                wanted.append(sig.split(":", 1)[1])
    rank = {vid: i for i, vid in reversed(list(enumerate(wanted)))}
    entries = list(catalog.get("entries") or [])
    entries.sort(key=lambda e: rank.get(e.get("video_id"), len(rank) + 1))
    return entries


def run_transcript_matching(
    catalog: dict,
    candidates: List[dict],
    max_catalog: int = TRANSCRIPT_MAX_CATALOG,
    max_candidates: int = TRANSCRIPT_MAX_CANDIDATES,
    progress_cb: ProgressCb = None,
) -> Dict[str, Any]:
    """Annotate candidates (in evidence order) with a transcript `fingerprint_match`.

    Mutates candidate dicts. Returns summary stats.
    """
    fn = funnel.current()
    summary: Dict[str, Any] = {
        "enabled": True, "catalog_transcripts": 0, "candidates_tested": 0,
        "candidates_without_transcript": 0, "confirmed": 0, "likely": 0,
        "skipped_reason": None,
    }
    entries = _catalog_order(catalog, candidates)[:max_catalog]
    if not entries:
        summary.update(enabled=False, skipped_reason="no_official_catalog")
        return summary
    if len(catalog.get("entries") or []) > len(entries):
        fn.note("transcript_catalog_cap", {
            "used": len(entries), "catalog": len(catalog.get("entries") or []),
        })

    if progress_cb:
        progress_cb(f"Reading captions of {len(entries)} official videos...")
    meta = {e["video_id"]: e for e in entries if e.get("video_id")}
    catalog_sketches: Dict[str, Dict[str, Any]] = {}
    deferred = 0
    for vid, (sketch, reason) in _get_many(list(meta)).items():
        if sketch:
            catalog_sketches[vid] = sketch
        elif reason == "rate_limited":
            deferred += 1
        else:
            fn.unreachable(f"official:{vid}", f"no transcript ({reason})")
    summary["catalog_transcripts"] = len(catalog_sketches)
    if deferred:
        summary["catalog_rate_limited"] = deferred
    if not catalog_sketches:
        summary.update(
            enabled=False,
            skipped_reason="rate_limited" if deferred else "no_official_transcripts",
        )
        return summary

    selected = [v for v in candidates if v.get("video_id")][:max_candidates]
    for v in candidates[max_candidates:]:
        fn.drop(v.get("video_id"), "verify_transcript", "candidate_cap", final=False)
    if progress_cb:
        progress_cb(f"Comparing captions of {len(selected)} suspects with official content...")
    sketches = _get_many([v["video_id"] for v in selected])

    for v in selected:
        sketch, reason = sketches.get(v["video_id"], (None, "transcript_error"))
        if not sketch:
            limited = reason == "rate_limited"
            key = "candidates_rate_limited" if limited else "candidates_without_transcript"
            summary[key] = summary.get(key, 0) + 1
            fn.drop(v["video_id"], "verify_transcript",
                    "rate_limited" if limited else "no_transcript", final=False, detail=reason)
            continue
        summary["candidates_tested"] += 1
        best = match_sketch(sketch, catalog_sketches)
        if not best:
            continue
        tier = tier_for(best["overlap"], best["shared"])
        off = meta.get(best["official_video_id"], {})
        match = {
            "official_video_id": best["official_video_id"],
            "official_title": off.get("title", ""),
            "official_channel_id": off.get("channel_id", ""),
            "similarity": best["overlap"],
            "is_match": tier is not None,
            "is_strong": tier == "confirmed",
            "backend": "transcript",
            "tier": tier,
            "shared_phrases": best["shared"],
            "candidate_span": best.get("candidate_span"),
            "official_span": best.get("official_span"),
        }
        if tier:
            v["fingerprint_match"] = match
            summary[tier] += 1
        else:
            v["transcript_near_miss"] = match

    logger.info("Transcript matching: %s", summary)
    return summary
