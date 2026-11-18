"""Channel / message risk scoring for Telegram piracy dumps."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_PIRACY_KWS = (
    "free course", "paid course", "lecture", "batch", "leaked", "telegram",
    "drive", "mega", "full course", "class recording", "notes", "pdf",
    "jee", "neet", "one shot", "crash course", "premium free",
)
_FILE_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".mp3", ".m4a", ".pdf", ".zip", ".rar")


def message_keyword_hits(text: str, entity_terms: List[str]) -> Dict[str, Any]:
    """Score a single message body against entity + piracy keywords."""
    text_l = (text or "").lower()
    entity_hits = [t for t in entity_terms if t and t.lower() in text_l]
    piracy_hits = [k for k in _PIRACY_KWS if k in text_l]
    score = min(len(entity_hits) * 25 + len(piracy_hits) * 8, 100)
    return {
        "score": score,
        "entity_hits": entity_hits,
        "piracy_hits": piracy_hits,
    }


def looks_like_media_dump(filename: str, mime: str = "", size: int = 0) -> bool:
    name = (filename or "").lower()
    mime_l = (mime or "").lower()
    if any(name.endswith(ext) for ext in _FILE_EXTS):
        return True
    if mime_l.startswith("video/") or mime_l.startswith("audio/"):
        return True
    # Large files without clear type still suspicious in edu dump channels
    if size and size > 50_000_000:
        return True
    return False


def score_channel(
    meta: dict,
    message_stats: Optional[dict] = None,
) -> Dict[str, Any]:
    """Risk score 0-100 for a Telegram channel/group.

    Favours non-viral dump patterns: low subscribers, high media ratio,
    piracy keywords, multi-brand dumps.
    """
    message_stats = message_stats or {}
    score = 0.0
    signals: List[str] = []

    title = (meta.get("title") or meta.get("username") or "").lower()
    about = (meta.get("about") or "").lower()
    participants = int(meta.get("participants_count") or meta.get("members") or 0)

    # Non-viral / small channels are the Phase C target
    if 0 < participants < 500:
        score += 25
        signals.append("very_small_audience")
    elif participants < 5000:
        score += 18
        signals.append("small_audience")
    elif participants < 50000:
        score += 8
        signals.append("mid_audience")
    else:
        # Large channels can still pirate, but less "hidden"
        score += 2

    title_blob = f"{title} {about}"
    kw_hits = [k for k in _PIRACY_KWS if k in title_blob]
    if kw_hits:
        score += min(len(kw_hits) * 6, 24)
        signals.append("piracy_keywords_in_profile")

    media_ratio = float(message_stats.get("media_ratio") or 0)
    if media_ratio >= 0.4:
        score += 20
        signals.append("high_media_ratio")
    elif media_ratio >= 0.2:
        score += 10
        signals.append("moderate_media_ratio")

    entity_msg_hits = int(message_stats.get("entity_hit_messages") or 0)
    if entity_msg_hits >= 5:
        score += 20
        signals.append("repeated_entity_mentions")
    elif entity_msg_hits >= 1:
        score += 10
        signals.append("entity_mentions")

    invite_exports = int(message_stats.get("invite_links_found") or 0)
    if invite_exports >= 2:
        score += 10
        signals.append("exports_more_invites")

    fp_matches = int(message_stats.get("fingerprint_matches") or 0)
    if fp_matches:
        score += min(fp_matches * 25, 40)
        signals.append("fingerprint_proof")

    score = min(score, 100.0)
    severity = "low"
    if score >= 75:
        severity = "critical"
    elif score >= 55:
        severity = "high"
    elif score >= 35:
        severity = "medium"

    return {
        "risk_score": round(score, 1),
        "severity": severity,
        "signals": signals,
        "participants": participants,
    }
