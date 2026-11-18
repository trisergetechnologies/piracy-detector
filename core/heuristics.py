"""Cheap title / duration / channel heuristics for Phase A.

Used to:
  - Rank discovery results so low-view likely pirates are analyzed first
  - Boost confidence signals later (without needing fingerprints yet)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from config import (
    CATALOG_TITLE_MATCH_THRESHOLD,
    DEEP_CRAWL_MAX_SUBSCRIBERS,
    PIRACY_MIN_DURATION_SECONDS,
)
from core.catalog import best_catalog_match
from core.youtube_client import safe_int

logger = logging.getLogger(__name__)

_TELEGRAM_RE = re.compile(r"(t\.me/|telegram\.me/|telegram|tg://)", re.IGNORECASE)
_FILE_SHARE_RE = re.compile(
    r"(drive\.google\.com|mega\.nz|mediafire\.com|dropbox\.com|terabox)",
    re.IGNORECASE,
)
_PIRACY_TITLE_KWS = (
    "full lecture", "complete course", "batch", "leaked", "free course",
    "telegram", "class recording", "one shot", "full chapter", "recorded class",
)


def _channel_subs(channel_map: Dict[str, dict], channel_id: str) -> int:
    info = channel_map.get(channel_id) or {}
    return safe_int(info.get("statistics", {}).get("subscriberCount"))


def piracy_heuristic_score(
    video: dict,
    catalog: Optional[dict] = None,
    channel_map: Optional[Dict[str, dict]] = None,
) -> Dict[str, Any]:
    """Score how likely a video is a low-view pirate dump (0-100 + signals)."""
    channel_map = channel_map or {}
    catalog = catalog or {}
    official_ids = catalog.get("video_ids") or set()
    official_channels = catalog.get("channel_ids") or set()
    title_index = catalog.get("title_index") or []

    vid = video.get("video_id", "")
    title = video.get("title") or ""
    desc = video.get("description") or ""
    channel_id = video.get("channel_id") or ""
    views = safe_int(video.get("view_count"))
    duration = safe_int(video.get("duration_seconds"))
    subs = _channel_subs(channel_map, channel_id)

    signals: List[str] = []
    score = 0.0

    if vid in official_ids or channel_id in official_channels:
        return {
            "heuristic_score": 0.0,
            "is_official": True,
            "signals": ["official_content"],
            "catalog_match": None,
            "priority": -1,
        }

    # Long-form lecture shape
    if duration >= PIRACY_MIN_DURATION_SECONDS:
        score += 20
        signals.append("long_duration")
    elif duration >= 300:
        score += 8
        signals.append("medium_duration")

    # Small / young dump channels
    if 0 < subs <= DEEP_CRAWL_MAX_SUBSCRIBERS:
        score += 18
        signals.append("small_channel")
    elif subs == 0 and channel_id:
        score += 10
        signals.append("unknown_subs")

    # Low views – the Phase A target
    if 0 < views < 500:
        score += 22
        signals.append("very_low_views")
    elif views < 5000:
        score += 14
        signals.append("low_views")
    elif views < 50000:
        score += 6
        signals.append("moderate_views")

    # Description bait
    if _TELEGRAM_RE.search(desc):
        score += 20
        signals.append("telegram_in_desc")
    if _FILE_SHARE_RE.search(desc):
        score += 12
        signals.append("file_share_in_desc")

    title_l = title.lower()
    if any(kw in title_l for kw in _PIRACY_TITLE_KWS):
        score += 10
        signals.append("piracy_title_kw")

    catalog_match = best_catalog_match(
        title, title_index, threshold=CATALOG_TITLE_MATCH_THRESHOLD
    )
    if catalog_match:
        sim = catalog_match["similarity"]
        score += 15 + min(sim, 1.0) * 15
        signals.append(f"catalog_title_match:{sim}")

    score = min(score, 100.0)

    # Priority: high heuristic first; within that, prefer lower views
    priority = score * 10 - min(views, 100_000) / 1000.0

    return {
        "heuristic_score": round(score, 1),
        "is_official": False,
        "signals": signals,
        "catalog_match": catalog_match,
        "priority": priority,
    }


def annotate_and_prioritize(
    videos: List[dict],
    catalog: Optional[dict] = None,
    channel_map: Optional[Dict[str, dict]] = None,
    drop_official: bool = True,
) -> List[dict]:
    """Attach heuristic fields and sort so likely low-view pirates come first."""
    kept: List[dict] = []
    dropped_official = 0

    for v in videos:
        h = piracy_heuristic_score(v, catalog=catalog, channel_map=channel_map)
        if h["is_official"] and drop_official:
            dropped_official += 1
            continue
        v["heuristic"] = h
        kept.append(v)

    kept.sort(key=lambda x: x.get("heuristic", {}).get("priority", 0), reverse=True)

    if dropped_official:
        logger.info("Heuristics: dropped %d official videos from analysis set", dropped_official)
    logger.info(
        "Heuristics: prioritized %d candidates (top score=%.1f)",
        len(kept),
        kept[0]["heuristic"]["heuristic_score"] if kept else 0,
    )
    return kept


def select_channels_for_deep_crawl(
    videos: List[dict],
    channel_map: Dict[str, dict],
    official_channel_ids: Optional[set] = None,
    max_channels: int = 25,
    max_subscribers: int = DEEP_CRAWL_MAX_SUBSCRIBERS,
) -> List[str]:
    """Pick suspicious small channels from seed hits for full-upload crawl.

    Works before full enrichment: uses search snippets + channel subscriber stats.
    """
    official_channel_ids = official_channel_ids or set()
    scored: Dict[str, float] = {}

    for v in videos:
        cid = v.get("channel_id") or ""
        if not cid or cid in official_channel_ids:
            continue
        subs = _channel_subs(channel_map, cid)
        # Unknown subs (0 / missing) still allowed; skip only clearly large channels
        if subs > max_subscribers:
            continue

        h = v.get("heuristic") or {}
        base = float(h.get("heuristic_score") or 0)
        if base <= 0:
            # Lightweight seed score when heuristics have not run yet
            base = 5.0
            if 0 < subs <= max_subscribers:
                base += 12
            title_l = (v.get("title") or "").lower()
            if any(kw in title_l for kw in _PIRACY_TITLE_KWS):
                base += 10

        desc = v.get("description") or ""
        if _TELEGRAM_RE.search(desc):
            base += 15
        if _FILE_SHARE_RE.search(desc):
            base += 8
        duration = safe_int(v.get("duration_seconds"))
        if duration >= PIRACY_MIN_DURATION_SECONDS:
            base += 10

        scored[cid] = max(scored.get(cid, 0), base)

    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    selected = [cid for cid, _ in ranked[:max_channels]]
    logger.info("Deep-crawl channel shortlist: %d channels", len(selected))
    return selected
