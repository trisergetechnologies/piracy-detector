"""Unified scorer that merges LLM text classification, thumbnail analysis,
video metadata signals, and channel signals into one final score per video."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

from config import (
    DEFAMATORY_BIG_CHANNEL_THRESHOLD,
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

    # High views suggest wider harm (0-20) – defamatory only.
    # For piracy, popularity is never a signal: a 12-view copy is as pirated
    # as a viral one. For DEFAMATORY from big channels, high views just means
    # mainstream news.
    views = safe_int(stats.get("viewCount"))
    suppress_view_boost = text_cls == "PIRATED" or (text_cls == "DEFAMATORY" and is_big_channel)
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

    # Minimal channel description (0-10)
    desc = snippet.get("description", "")
    if len(desc) < 50:
        score += 10

    # Generic education-keyword channel names (0-20). No subscriber condition:
    # channel size is not evidence of piracy either way.
    piracy_keywords = [
        "study", "education", "lecture", "class", "academy", "free",
        "batch", "tutorial", "learning", "exam", "preparation", "jee",
        "neet", "pw", "wallah", "hub", "zone", "point",
    ]
    if text_cls == "PIRATED" and any(kw in ch_name for kw in piracy_keywords):
        score += 20

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
# Piracy evidence tiers
# ---------------------------------------------------------------------------
#   confirmed (conf 90-99) – the content itself matches official content
#   likely    (conf 70-89) – borderline content match, or same title AND same
#                            duration as an official video, or LLM + title match
#   weak      (conf <= 69) – metadata only (LLM alone, title alone, heuristics
#                            alone). Kept and labelled, never silently dropped.
# Views, likes and subscriber counts are not inputs.


def _signal_value(signals: List[str], prefix: str) -> Optional[str]:
    for s in signals:
        if s == prefix or s.startswith(prefix + ":"):
            return s.split(":", 1)[1] if ":" in s else ""
    return None


def piracy_evidence(enriched_video: dict, text_result: Optional[dict] = None) -> dict:
    """Return {"tier": confirmed|likely|weak|None, "confidence": int, "reason": str}."""
    text_result = text_result or {}
    heur = enriched_video.get("heuristic") or {}
    signals: List[str] = list(heur.get("signals") or [])
    h_score = float(heur.get("heuristic_score") or 0)
    cm = heur.get("catalog_match") or {}
    fm = enriched_video.get("fingerprint_match") or {}

    llm_failed = bool(text_result.get("llm_error"))
    llm_conf = int(float(text_result.get("confidence") or 0))
    llm_pirated = (
        not llm_failed
        and text_result.get("classification") == "PIRATED"
        and llm_conf >= MIN_CONFIDENCE
    )
    llm_note = " (AI classifier unavailable)" if llm_failed else ""

    # 1. Content match
    if fm.get("is_match"):
        sim = float(fm.get("similarity") or 0)
        official = fm.get("official_title") or fm.get("official_video_id") or "official content"
        backend = fm.get("backend") or "audio"
        if backend == "transcript":
            what = f"spoken words match official video '{official}' ({sim:.0%} of sampled phrases"
            what += f", {fm.get('shared_phrases')} shared)" if fm.get("shared_phrases") else ")"
            if fm.get("audio_similarity"):
                what += f"; audio fingerprint agrees ({float(fm['audio_similarity']):.0%})"
        else:
            what = f"audio fingerprint ({backend}) matches official video '{official}' ({sim:.0%})"
        tier = fm.get("tier") or ("confirmed" if fm.get("is_strong") else "likely")
        if tier == "confirmed":
            return {"tier": "confirmed", "confidence": min(max(90, int(round(sim * 100))), 99),
                    "reason": what}
        return {"tier": "likely", "confidence": min(89, 74 + int(sim * 15)), "reason": what}

    # 2. Metadata evidence against the official catalog
    title_sim = float(cm.get("similarity") or 0)
    same_duration = "duration_match_official" in signals
    official_title = cm.get("official_title") or ""
    if cm and same_duration:
        return {"tier": "likely", "confidence": 85 if title_sim >= 0.7 else 80,
                "reason": f"same title ({title_sim:.0%} similar) and same duration as official "
                          f"video '{official_title}', uploaded by a non-official channel{llm_note}"}
    if cm and llm_pirated:
        return {"tier": "likely", "confidence": 75,
                "reason": f"title matches official video '{official_title}' ({title_sim:.0%}) and "
                          f"AI classifier flagged it: {text_result.get('reason', '')}"}

    # 3. Weak: one metadata signal only
    if llm_pirated:
        return {"tier": "weak", "confidence": min(max(llm_conf - 20, 60), 69),
                "reason": f"metadata only, no content match – AI classifier: {text_result.get('reason', '')}"}
    if cm:
        return {"tier": "weak", "confidence": min(69, 58 + int(title_sim * 10)),
                "reason": f"title resembles official video '{official_title}' ({title_sim:.0%}); "
                          f"content not verified{llm_note}"}
    dur_ref = _signal_value(signals, "duration_equals_official")
    if dur_ref and ("brand_mention" in signals or "piracy_title_kw" in signals):
        return {"tier": "weak", "confidence": 62,
                "reason": f"same duration as official video {dur_ref} plus brand/piracy wording; "
                          f"content not verified{llm_note}"}
    # Heuristics alone only count when the classifier could not run; otherwise
    # its LEGITIMATE verdict stands.
    if llm_failed and h_score >= 40 and "brand_mention" in signals:
        shown = ", ".join(s for s in signals if not s.startswith("tier:"))
        return {"tier": "weak", "confidence": min(69, 40 + int(h_score * 0.4)),
                "reason": f"heuristics only ({shown}); content not verified{llm_note}"}
    return {"tier": None, "confidence": 0, "reason": ""}


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
    # Piracy evidence is evaluated first so that a video proven by content or
    # catalog evidence is scored as PIRATED (no view/subscriber terms) even
    # when the LLM said LEGITIMATE or could not run.
    evidence = piracy_evidence(enriched_video, text_result)
    tier = evidence["tier"]
    llm_defamatory = text_cls == "DEFAMATORY" and text_conf >= MIN_CONFIDENCE
    if tier and llm_defamatory and tier == "weak":
        tier = None  # a confident DEFAMATORY verdict outranks metadata-only piracy
    score_cls = "PIRATED" if tier else text_cls

    meta_s = _meta_score(video_detail, score_cls, channel_subs=ch_subs, description=description)
    chan_s = _channel_score(channel_info, score_cls)

    final = (
        W_TEXT * text_s
        + W_THUMB * thumb_s
        + W_META * meta_s
        + W_CHANNEL * chan_s
    )

    # LLM verdict (defamatory path unchanged): trusted only above MIN_CONFIDENCE
    classification = text_cls if text_conf >= MIN_CONFIDENCE else "LEGITIMATE"
    confidence = int(text_conf)
    reason = text_result.get("reason", "")
    severity = text_result.get("severity", "low")

    # --- Piracy: evidence tiers decide; the LLM is only one of the signals ---
    fp_match = enriched_video.get("fingerprint_match") or {}
    fp_score = round(float(fp_match.get("similarity") or 0) * 100, 1)
    if tier:
        classification = "PIRATED"
        confidence = evidence["confidence"]
        reason = f"{tier.upper()}: {evidence['reason']}"
        # The score of a piracy hit is its evidence confidence, so results sort
        # by strength of proof (confirmed 90+, likely 70-89, weak <= 69).
        final = float(confidence)
        if tier == "confirmed":
            severity = "critical"
        elif tier == "likely":
            severity = "high"
        else:
            severity = max(severity, "medium", key=lambda s: SEVERITY_ORDER.get(s, 0))
    elif classification == "PIRATED":
        # Safeguard: a PIRATED verdict always has a tier; never report one without it.
        classification = "LEGITIMATE"
    else:
        if final >= 80:
            severity = "critical"
        elif final >= 60:
            severity = max(severity, "high", key=lambda s: SEVERITY_ORDER.get(s, 0))

    heur = enriched_video.get("heuristic")
    if tier and isinstance(heur, dict):
        sigs = [s for s in heur.get("signals") or [] if not s.startswith("tier:")]
        heur["signals"] = sigs + [f"tier:{tier}"]

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
