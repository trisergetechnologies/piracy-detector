"""Unified scorer that merges LLM text classification, thumbnail analysis,
video metadata signals, and channel signals into one final score per video."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

from config import (
    DEFAMATORY_BIG_CHANNEL_THRESHOLD,
    FINGERPRINT_MATCH_THRESHOLD,
    FINGERPRINT_STRONG_THRESHOLD,
    MIN_CONFIDENCE,
    SEVERITY_ORDER,
)
from core.youtube_client import format_duration, parse_duration, safe_int

logger = logging.getLogger(__name__)

# Weight distribution (must sum to 1.0)
W_TEXT = 0.50
W_THUMB = 0.20
W_META = 0.15
W_CHANNEL = 0.15

_TELEGRAM_RE = re.compile(r"(t\.me/|telegram\.me/|telegram|tg://)", re.IGNORECASE)
_FILE_SHARE_RE = re.compile(
    r"(drive\.google\.com|mega\.nz|mediafire\.com|dropbox\.com|fichier\.com|terabox)",
    re.IGNORECASE,
)
_PIRACY_DESC_KWS = [
    "telegram", "join telegram", "telegram group", "telegram channel",
    "t.me/", "drive link", "google drive", "mega link",
    "free download", "download link", "leaked", "paid course free",
    "premium free", "free course", "join for more",
]


def _desc_piracy_score(description: str) -> float:
    """Score 0-100 based on piracy signals found in the video description."""
    if not description:
        return 0.0
    desc_lower = description.lower()
    score = 0.0

    if _TELEGRAM_RE.search(description):
        score += 40
    if _FILE_SHARE_RE.search(description):
        score += 30

    kw_hits = sum(1 for kw in _PIRACY_DESC_KWS if kw in desc_lower)
    score += min(kw_hits * 8, 30)

    return min(score, 100.0)


def _meta_score(
    video_detail: dict,
    text_cls: str = "LEGITIMATE",
    channel_subs: int = 0,
    description: str = "",
) -> float:
    """Score 0-100 based on video metadata signals.

    Args:
        text_cls: The LLM classification, used to apply context-aware scoring.
        channel_subs: Subscriber count of the uploading channel.
        description: Full video description for piracy keyword scanning.
    """
    score = 0.0
    snippet = video_detail.get("snippet", {})
    stats = video_detail.get("statistics", {})
    duration = parse_duration(video_detail.get("contentDetails", {}).get("duration", ""))

    is_big_channel = channel_subs >= DEFAMATORY_BIG_CHANNEL_THRESHOLD

    # Short-form content is higher-risk for memes/defamatory (0-20)
    if duration <= 60:
        score += 20
    elif duration <= 180:
        score += 12

    # Long-form content is higher-risk for piracy (0-25)
    if text_cls == "PIRATED":
        if duration >= 2400:
            score += 25
        elif duration >= 1200:
            score += 20
        elif duration >= 600:
            score += 12

    # High views suggest wider harm (0-20)
    # BUT for DEFAMATORY from big channels, high views just means mainstream news.
    views = safe_int(stats.get("viewCount"))
    suppress_view_boost = text_cls == "DEFAMATORY" and is_big_channel
    if not suppress_view_boost:
        if views > 1_000_000:
            score += 20
        elif views > 100_000:
            score += 15
        elif views > 10_000:
            score += 8

    # High engagement rate (0-15)
    likes = safe_int(stats.get("likeCount"))
    comments = safe_int(stats.get("commentCount"))
    if views > 0 and not suppress_view_boost:
        eng = (likes + comments) / views
        if eng > 0.05:
            score += 15
        elif eng > 0.02:
            score += 8

    # Piracy signal: disabled comments/likes (pirates often disable these)
    if text_cls == "PIRATED":
        if comments == 0 and views > 100:
            score += 10
        if likes == 0 and views > 100:
            score += 5

    # Recency -- newer = more urgent (0-15)
    pub = snippet.get("publishedAt", "")
    if pub:
        try:
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            days_old = (datetime.now(pub_dt.tzinfo) - pub_dt).days
            if days_old <= 1:
                score += 15
            elif days_old <= 7:
                score += 12
            elif days_old <= 30:
                score += 6
        except Exception:
            pass

    # Emoji / hashtag spam in title (0-10) -- defamatory signal
    title = snippet.get("title", "")
    emoji_chars = sum(1 for ch in title if ord(ch) > 0x1F600)
    if emoji_chars >= 3:
        score += 10
    elif emoji_chars >= 1:
        score += 5
    hashtags = title.count("#")
    if hashtags >= 3:
        score += 10

    # Piracy title keywords boost (0-15)
    if text_cls == "PIRATED":
        title_lower = title.lower()
        piracy_title_kws = [
            "full lecture", "complete", "one shot", "all lectures",
            "full chapter", "batch", "class recording", "leaked",
            "free download", "drive link", "telegram", "free course",
            "paid course free", "premium free",
        ]
        if any(kw in title_lower for kw in piracy_title_kws):
            score += 15

    # Description-based piracy signals (telegram links, drive links, etc.)
    if text_cls == "PIRATED" and description:
        desc_s = _desc_piracy_score(description)
        score += desc_s * 0.25  # up to +25 from description

    return min(score, 100.0)


def _channel_score(channel_info: Optional[dict], text_cls: str = "LEGITIMATE") -> float:
    """Score 0-100 based on channel-level signals."""
    if not channel_info:
        return 30.0  # unknown channel gets a moderate suspicion score

    score = 0.0
    stats = channel_info.get("statistics", {})
    snippet = channel_info.get("snippet", {})

    subs = safe_int(stats.get("subscriberCount"))
    video_count = safe_int(stats.get("videoCount"))

    # Meme/entertainment channel name keywords (0-25)
    ch_name = snippet.get("title", "").lower()
    meme_indicators = [
        "meme", "memes", "funny", "comedy", "roast", "troll",
        "viral", "shorts", "reels", "entertainment", "vines", "lol",
    ]
    if any(kw in ch_name for kw in meme_indicators):
        score += 25

    # Very new channel (< 6 months) (0-20)
    created = snippet.get("publishedAt", "")
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(created_dt.tzinfo) - created_dt).days
            if age_days < 90:
                score += 20
            elif age_days < 180:
                score += 12
        except Exception:
            pass

    # Low subscriber count with lots of videos = piracy signal (0-25)
    if subs < 1000 and video_count > 10:
        score += 25
    elif subs < 5000 and video_count > 50:
        score += 15
    elif subs < 10_000 and video_count > 100:
        score += 10

    # Minimal channel description (0-10)
    desc = snippet.get("description", "")
    if len(desc) < 50:
        score += 10

    # Generic education-keyword names on small channels = piracy signal (0-20)
    piracy_keywords = [
        "study", "education", "lecture", "class", "academy", "free",
        "batch", "tutorial", "learning", "exam", "preparation", "jee",
        "neet", "pw", "wallah", "hub", "zone", "point",
    ]
    if subs < 10_000 and any(kw in ch_name for kw in piracy_keywords):
        score += 20

    # Extra boost: if LLM flagged PIRATED and channel is tiny, it's very suspicious
    if text_cls == "PIRATED" and subs < 5000:
        score += 15

    # Big-channel penalty for DEFAMATORY: established channels are almost never
    # actual troll/meme creators; they are news outlets or commentary channels.
    if text_cls == "DEFAMATORY":
        if subs >= 500_000:
            score -= 55
        elif subs >= DEFAMATORY_BIG_CHANNEL_THRESHOLD:
            score -= 40
        elif subs >= 50_000:
            score -= 25

    return max(min(score, 100.0), 0.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_final_score(
    text_result: dict,
    thumb_result: dict,
    video_detail: dict,
    channel_info: Optional[dict],
    enriched_video: Optional[dict] = None,
) -> dict:
    """Merge all signals into one scored result.

    Args:
        enriched_video: The enriched video dict with full description etc.
                        Used for description-based piracy scoring.

    Returns a dict with:
        final_score       – float 0-100
        classification    – DEFAMATORY / PIRATED / LEGITIMATE
        confidence        – int 0-100 (from LLM)
        severity          – critical / high / medium / low
        reason            – one-sentence from LLM
        thumbnail_analysis – dict from vision model
        sub_scores        – breakdown dict
    """
    enriched_video = enriched_video or {}

    # --- text sub-score (0-100) ---
    text_cls = text_result.get("classification", "LEGITIMATE")
    text_conf = float(text_result.get("confidence", 0))
    if text_cls == "LEGITIMATE":
        text_s = max(0, 100 - text_conf)
    else:
        text_s = text_conf

    # --- thumbnail sub-score (0-100) ---
    thumb_neg = thumb_result.get("is_negative_thumbnail", False)
    thumb_conf = float(thumb_result.get("confidence", 0))
    thumb_s = thumb_conf if thumb_neg else max(0, 100 - thumb_conf)

    # --- metadata & channel (pass LLM classification for context-aware scoring) ---
    ch_subs = safe_int(
        (channel_info or {}).get("statistics", {}).get("subscriberCount")
    )
    description = (
        enriched_video.get("description", "")
        or video_detail.get("snippet", {}).get("description", "")
        or ""
    )
    meta_s = _meta_score(video_detail, text_cls, channel_subs=ch_subs, description=description)
    chan_s = _channel_score(channel_info, text_cls)

    final = (
        W_TEXT * text_s
        + W_THUMB * thumb_s
        + W_META * meta_s
        + W_CHANNEL * chan_s
    )

    # --- Combined piracy boost ---
    # Small channel + long video + telegram/drive in description = very high piracy
    if text_cls == "PIRATED":
        duration = enriched_video.get("duration_seconds", 0) or parse_duration(
            video_detail.get("contentDetails", {}).get("duration", "")
        )
        desc_piracy = _desc_piracy_score(description)
        is_small = ch_subs < 50_000
        is_long = duration >= 600
        has_telegram = desc_piracy >= 30

        if is_small and is_long and has_telegram:
            final = min(final + 15, 100.0)
        elif is_small and has_telegram:
            final = min(final + 10, 100.0)
        elif is_small and is_long:
            final = min(final + 5, 100.0)

        # Phase A: catalog title match + low views strengthen piracy confidence
        heur = enriched_video.get("heuristic") or {}
        catalog_match = heur.get("catalog_match")
        if catalog_match and catalog_match.get("similarity", 0) >= 0.45:
            final = min(final + 8 + catalog_match["similarity"] * 7, 100.0)
        views = safe_int(
            enriched_video.get("view_count")
            or video_detail.get("statistics", {}).get("viewCount")
        )
        if is_small and is_long and 0 < views < 5000:
            final = min(final + 6, 100.0)

    # Determine classification: trust the LLM unless it has very low confidence
    classification = text_cls if text_conf >= MIN_CONFIDENCE else "LEGITIMATE"
    confidence = int(text_conf)
    reason = text_result.get("reason", "")

    # --- Phase B: audio fingerprint can prove piracy and override LLM ---
    fp_match = enriched_video.get("fingerprint_match") or {}
    fp_sim = float(fp_match.get("similarity") or 0)
    fp_score = round(fp_sim * 100, 1)
    if fp_match.get("is_match") and fp_sim >= FINGERPRINT_MATCH_THRESHOLD:
        classification = "PIRATED"
        # Confidence from match quality, not LLM vibes
        confidence = max(confidence, int(round(fp_sim * 100)))
        final = max(final, min(55 + fp_sim * 45, 100.0))
        official_title = fp_match.get("official_title") or fp_match.get("official_video_id")
        reason = (
            f"Audio fingerprint match ({fp_sim:.0%}) with official content"
            + (f": {official_title}" if official_title else "")
        )
        if fp_match.get("is_strong") or fp_sim >= FINGERPRINT_STRONG_THRESHOLD:
            confidence = max(confidence, 92)
            final = max(final, 90.0)

    # Severity mapping
    severity = text_result.get("severity", "low")
    if final >= 80:
        severity = "critical"
    elif final >= 60:
        severity = max(severity, "high", key=lambda s: SEVERITY_ORDER.get(s, 0))

    return {
        "final_score": round(final, 1),
        "classification": classification,
        "confidence": confidence,
        "severity": severity,
        "reason": reason,
        "thumbnail_analysis": thumb_result,
        "fingerprint_match": fp_match or None,
        "sub_scores": {
            "text": round(text_s, 1),
            "thumbnail": round(thumb_s, 1),
            "metadata": round(meta_s, 1),
            "channel": round(chan_s, 1),
            "fingerprint": fp_score,
        },
    }


def score_videos(
    videos_enriched: List[dict],
    text_results: List[dict],
    thumb_results: List[dict],
    video_details_map: Dict[str, dict],
    channel_map: Dict[str, dict],
) -> List[dict]:
    """Score a batch of videos. Each enriched video dict gets a 'score_result' key added."""
    scored: List[dict] = []
    for v, txt, thm in zip(videos_enriched, text_results, thumb_results):
        vid = v["video_id"]
        detail = video_details_map.get(vid, {})
        ch = channel_map.get(v.get("channel_id", ""))
        sr = compute_final_score(txt, thm, detail, ch, enriched_video=v)
        v["score_result"] = sr
        scored.append(v)
    return scored
