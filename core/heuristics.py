"""Cheap evidence heuristics for piracy candidates.

Used to:
  - Rank discovery results BEFORE any cut, so evidence decides what is analyzed
  - Supply signals to the scorer (catalog title match, duration match, links)

No popularity signal (views, subscribers, likes) is used anywhere here: a
pirated copy with 12 views ranks exactly like one with 1.2M views.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from config import (
    CATALOG_TITLE_MATCH_THRESHOLD,
    DURATION_MATCH_TOLERANCE_RATIO,
    DURATION_MATCH_TOLERANCE_SECONDS,
    PIRACY_MIN_DURATION_SECONDS,
    PIRACY_TITLE_KEYWORDS,
)
from core.catalog import best_catalog_match
from core.youtube_client import safe_int

logger = logging.getLogger(__name__)

_TELEGRAM_RE = re.compile(r"(t\.me/|telegram\.me/|telegram|tg://)", re.IGNORECASE)
_FILE_SHARE_RE = re.compile(
    r"(drive\.google\.com|mega\.nz|mediafire\.com|dropbox\.com|terabox)",
    re.IGNORECASE,
)


def has_piracy_keyword(text: str) -> bool:
    low = (text or "").lower()
    return any(kw in low for kw in PIRACY_TITLE_KEYWORDS)


def durations_match(a: int, b: int) -> bool:
    """True when two durations are equal within re-encode tolerance."""
    if not a or not b:
        return False
    tol = max(DURATION_MATCH_TOLERANCE_SECONDS, int(max(a, b) * DURATION_MATCH_TOLERANCE_RATIO))
    return abs(a - b) <= tol


def piracy_heuristic_score(
    video: dict,
    catalog: Optional[dict] = None,
    channel_map: Optional[Dict[str, dict]] = None,  # kept for call compatibility; unused
    entity_name: str = "",
) -> Dict[str, Any]:
    """Score how much evidence there is that a video is a re-upload (0-100 + signals)."""
    catalog = catalog or {}
    official_ids = catalog.get("video_ids") or set()
    official_channels = catalog.get("channel_ids") or set()
    title_index = catalog.get("title_index") or []
    duration_map = catalog.get("duration_map") or {}

    vid = video.get("video_id", "")
    title = video.get("title") or ""
    desc = video.get("description") or ""
    channel_id = video.get("channel_id") or ""
    duration = safe_int(video.get("duration_seconds"))

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
        score += 15
        signals.append("long_duration")
    elif duration >= 300:
        score += 6
        signals.append("medium_duration")

    # Description bait
    if _TELEGRAM_RE.search(desc):
        score += 15
        signals.append("telegram_in_desc")
    if _FILE_SHARE_RE.search(desc):
        score += 10
        signals.append("file_share_in_desc")

    if has_piracy_keyword(title):
        score += 10
        signals.append("piracy_title_kw")

    name_l = (entity_name or "").strip().lower()
    if name_l and name_l in f"{title} {desc}".lower():
        score += 10
        signals.append("brand_mention")

    # Same title as an official video (re-titling aside, the cheapest strong signal)
    catalog_match = best_catalog_match(
        title, title_index, threshold=CATALOG_TITLE_MATCH_THRESHOLD
    )
    if catalog_match:
        sim = catalog_match["similarity"]
        official_dur = safe_int(duration_map.get(catalog_match["official_video_id"]))
        same_duration = durations_match(duration, official_dur)
        if catalog_match.get("distinctive_shared", 0) >= 2 or same_duration:
            score += 20 + min(sim, 1.0) * 15
            signals.append(f"catalog_title_match:{sim}")
        else:
            # Shares only the catalog's common course words: same subject,
            # not evidence of a copy. Keeps a small ranking nudge only.
            score += 5
            signals.append(f"generic_title_overlap:{sim}")
        if same_duration:
            score += 25
            signals.append("duration_match_official")
    elif duration >= 300 and duration_map:
        # Re-titled copy: same length as some official video. Weak alone
        # (coincidences happen), so it only raises priority for content checks.
        for off_vid, off_dur in duration_map.items():
            if abs(duration - safe_int(off_dur)) <= 1:
                score += 12
                signals.append(f"duration_equals_official:{off_vid}")
                break

    score = min(score, 100.0)
    return {
        "heuristic_score": round(score, 1),
        "is_official": False,
        "signals": signals,
        "catalog_match": catalog_match,
        "priority": score,
    }


def _mark_bulk_channels(videos: List[dict]) -> None:
    """A channel hosting several official-titled videos is a dump channel."""
    per_channel: Dict[str, int] = {}
    for v in videos:
        sigs = (v.get("heuristic") or {}).get("signals") or []
        if v.get("channel_id") and any(s.startswith("catalog_title_match") for s in sigs):
            per_channel[v["channel_id"]] = per_channel.get(v["channel_id"], 0) + 1
    for v in videos:
        n = per_channel.get(v.get("channel_id") or "", 0)
        if n >= 3:
            h = v["heuristic"]
            h["signals"].append(f"channel_bulk_catalog_titles:{n}")
            h["heuristic_score"] = round(min(h["heuristic_score"] + 10, 100.0), 1)
            h["priority"] = h["heuristic_score"]


def annotate_and_prioritize(
    videos: List[dict],
    catalog: Optional[dict] = None,
    channel_map: Optional[Dict[str, dict]] = None,
    drop_official: bool = True,
    entity_name: str = "",
) -> List[dict]:
    """Attach heuristic fields and sort by evidence (stable; never by popularity)."""
    from core import funnel

    kept: List[dict] = []
    dropped_official: List[str] = []

    for v in videos:
        h = piracy_heuristic_score(v, catalog=catalog, entity_name=entity_name)
        if h["is_official"] and drop_official:
            dropped_official.append(v.get("video_id", ""))
            continue
        v["heuristic"] = h
        kept.append(v)

    _mark_bulk_channels(kept)
    kept.sort(key=lambda x: x.get("heuristic", {}).get("priority", 0), reverse=True)

    if dropped_official:
        funnel.current().drop_many(dropped_official, "filter", "official_content")
        logger.info("Heuristics: dropped %d official videos from analysis set", len(dropped_official))
    logger.info(
        "Heuristics: prioritized %d candidates (top score=%.1f)",
        len(kept),
        kept[0]["heuristic"]["heuristic_score"] if kept else 0,
    )
    return kept


def select_channels_for_deep_crawl(
    videos: List[dict],
    channel_map: Optional[Dict[str, dict]] = None,  # unused: no subscriber filter
    official_channel_ids: Optional[set] = None,
    max_channels: int = 25,
    max_subscribers: int = 0,  # ignored; kept for call compatibility
    catalog: Optional[dict] = None,
    entity_name: str = "",
) -> List[str]:
    """Pick channels to crawl by evidence only: official-titled uploads,
    piracy wording, Telegram/drive links, lecture-length videos."""
    official_channel_ids = official_channel_ids or set()
    best: Dict[str, float] = {}
    count: Dict[str, int] = {}

    for v in videos:
        cid = v.get("channel_id") or ""
        if not cid or cid in official_channel_ids:
            continue
        h = v.get("heuristic") or piracy_heuristic_score(v, catalog=catalog, entity_name=entity_name)
        s = float(h.get("heuristic_score") or 0)
        best[cid] = max(best.get(cid, 0.0), s)
        if s >= 20:
            count[cid] = count.get(cid, 0) + 1

    # Several suspicious uploads from one channel outrank a single one.
    ranked = sorted(
        best.items(),
        key=lambda x: x[1] + min(count.get(x[0], 0), 5) * 6,
        reverse=True,
    )
    selected = [cid for cid, s in ranked if s > 0][:max_channels]
    logger.info("Deep-crawl channel shortlist: %d channels", len(selected))
    return selected
