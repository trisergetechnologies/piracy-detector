"""Channel / message risk scoring for Telegram piracy dumps."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import TELEGRAM_EXTRA_DUMP_KEYWORDS

# Strong dump / redistribution signals (not normal educator marketing).
_DUMP_KWS = (
    "free course",
    "paid course free",
    "paid course",
    "premium free",
    "leaked",
    "full course",
    "complete course",
    "class recording",
    "recorded class",
    "recorded lecture",
    "google drive",
    "drive link",
    "drive.google",
    "mega.nz",
    "mega link",
    "mediafire",
    "terabox",
    "torrent",
    "zip file",
    "full batch",
    "batch leaked",
    "join for notes",
    "pdf dump",
) + tuple(k.lower() for k in TELEGRAM_EXTRA_DUMP_KEYWORDS)

# Soft education words — only meaningful together with dump signals / media.
_CONTEXT_KWS = (
    "lecture",
    "batch",
    "notes",
    "pdf",
    "crash course",
    "one shot",
    "neet",
    "jee",
)

_FILE_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".mp3", ".m4a", ".pdf", ".zip", ".rar")


def message_keyword_hits(text: str, entity_terms: List[str]) -> Dict[str, Any]:
    """Score a single message body against entity + piracy dump keywords.

    Entity mention alone is NOT enough for a piracy hit (official promo posts
    always mention the educator). Dump keywords or media evidence are required.
    """
    text_l = (text or "").lower()
    entity_hits = [t for t in entity_terms if t and t.lower() in text_l]
    dump_hits = [k for k in _DUMP_KWS if k in text_l]
    context_hits = [k for k in _CONTEXT_KWS if k in text_l]

    score = 0
    if entity_hits and dump_hits:
        score = min(45 + len(entity_hits) * 15 + len(dump_hits) * 12, 100)
    elif dump_hits:
        score = min(25 + len(dump_hits) * 12, 80)
    elif entity_hits and context_hits:
        # Soft signal only — scanner must also see media dump before flagging
        score = min(10 + len(entity_hits) * 5 + len(context_hits) * 3, 24)

    return {
        "score": score,
        "entity_hits": entity_hits,
        "piracy_hits": dump_hits,  # backwards-compatible alias
        "dump_hits": dump_hits,
        "context_hits": context_hits,
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

    Evidence only: media ratio, piracy wording, entity mentions, catalog and
    fingerprint matches. Audience size is not a signal in either direction.
    """
    message_stats = message_stats or {}
    score = 0.0
    signals: List[str] = []

    if message_stats.get("is_official"):
        return {
            "risk_score": 0,
            "severity": "low",
            "signals": ["official_whitelisted"],
        }

    title = (meta.get("title") or meta.get("username") or "").lower()
    about = (meta.get("about") or "").lower()

    catalog_matches = int(message_stats.get("catalog_matches") or 0)
    if catalog_matches:
        score += min(20 + catalog_matches * 5, 40)
        signals.append("official_title_or_duration_matches")

    title_blob = f"{title} {about}"
    kw_hits = [k for k in _DUMP_KWS if k in title_blob]
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
        score += min(30 + fp_matches * 5, 40)
        signals.append("fingerprint_matches")

    score = min(score, 100.0)
    if score >= 70:
        severity = "critical"
    elif score >= 45:
        severity = "high"
    elif score >= 25:
        severity = "medium"
    else:
        severity = "low"

    return {
        "risk_score": round(score, 1),
        "severity": severity,
        "signals": signals,
    }
