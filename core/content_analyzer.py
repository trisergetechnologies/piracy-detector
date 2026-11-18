"""AI-powered content classification using OpenAI GPT-4o-mini (text) and GPT-4o (vision)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from config import (
    LLM_BREAKER_FAILURES,
    LLM_BREAKER_SECONDS,
    LLM_MODEL,
    OPENAI_API_KEY,
    THUMBNAIL_ANALYSIS_ENABLED,
    VISION_MODEL,
)

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None

# Circuit breaker: after repeated auth/billing failures stop calling the API
# for a while instead of failing once per video. Detection continues without
# the LLM (content + catalog evidence); results are marked `llm_error`.
_FATAL_MARKERS = (
    "401", "incorrect api key", "invalid_api_key", "insufficient_quota",
    "no credits", "billing", "exceeded your current quota", "openai_api_key is not set",
)
_breaker = {"fatal_streak": 0, "open_until": 0.0, "last_error": ""}


def llm_unavailable_reason() -> str:
    """Non-empty while the breaker is open."""
    return _breaker["last_error"] if time.time() < _breaker["open_until"] else ""


def _record_llm_failure(exc: Exception) -> None:
    from core import funnel

    msg = str(exc)
    fatal = any(m in msg.lower() for m in _FATAL_MARKERS)
    funnel.current().error("llm_auth_or_billing" if fatal else "llm_error", msg)
    if fatal:
        _breaker["fatal_streak"] += 1
        _breaker["last_error"] = msg[:200]
        if _breaker["fatal_streak"] >= LLM_BREAKER_FAILURES:
            _breaker["open_until"] = time.time() + LLM_BREAKER_SECONDS
            logger.error("LLM disabled for %ds after repeated auth/billing errors: %s",
                         LLM_BREAKER_SECONDS, msg[:200])


def _llm_failed_result(reason: str) -> dict:
    return {"classification": "LEGITIMATE", "confidence": 0, "severity": "low",
            "reason": f"LLM unavailable: {reason}", "llm_error": True}


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it as an environment variable."
            )
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Text classification (GPT-4o-mini – cheap & fast)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a content-moderation expert that analyses YouTube video metadata "
    "to detect defamatory, pirated, or otherwise infringing content about a "
    "specific person or brand.  Return ONLY valid JSON – no markdown fences."
)

_CLASSIFY_USER = """\
Analyse this YouTube video regarding **{entity_name}** ({entity_type}).

Title       : {title}
Channel     : {channel_title} ({subs} subscribers)
Description : {description}
Tags        : {tags}
Duration    : {duration}
Views       : {views}
Published   : {published}
Official-catalog evidence : {evidence}

Scan focus  : {scan_type}

For PIRATED decisions, channel size and view count are NOT evidence either way:
tiny channels and large channels both re-upload paid content, and a copy with
10 views is as pirated as a viral one. Judge piracy on the content signals only.

IMPORTANT – Scan focus guidance:
- If scan focus is "piracy", your PRIMARY goal is to detect PIRATED content. Any video that
  looks like a re-upload of {entity_name}'s original content by an unauthorised channel should
  be classified PIRATED. Be aggressive about catching piracy. Only classify as DEFAMATORY if
  the content is clearly, unmistakably harassment/trolling — otherwise choose PIRATED or LEGITIMATE.
- If scan focus is "defamatory", your PRIMARY goal is to detect DEFAMATORY content. Only classify
  as PIRATED if the evidence is overwhelming — otherwise choose DEFAMATORY or LEGITIMATE.
- If scan focus is "both", weigh both categories equally.

Classify into exactly ONE category:

 • DEFAMATORY  – intentional mocking, trolling, fake news, defamation, harassment, or meme
   content SPECIFICALLY CREATED to target {entity_name}.
   IMPORTANT: DEFAMATORY applies ONLY to content from small, unofficial channels that
   deliberately create troll / meme / harassment material. It does NOT apply to:
   - News channels or media outlets (any channel with 100K+ subscribers is almost certainly
     a legitimate media outlet, NOT a troll channel)
   - Political commentary, debate coverage, or opinion pieces from established channels
   - Satire or parody from well-known comedy channels
   - Videos that merely USE words like "troll", "viral", "exposed", "controversy", "roast"
     in the title for engagement but are actually news coverage or commentary
   When the channel has {subs} subscribers and that number is above 50,000, strongly lean
   toward LEGITIMATE unless the content is unmistakably personal harassment.

 • PIRATED     – unauthorized re-upload or redistribution of the entity's original content.
   CRITICAL piracy indicators (if ANY of these are present, strongly lean PIRATED):
   1. **Telegram link in description** – Pirates almost ALWAYS put a Telegram link, Telegram
      group/channel invite (t.me/..., telegram.me/...) in the description. This is the single
      strongest piracy signal. If the description contains "telegram", "t.me/", or a Telegram
      invite link, the video is almost certainly pirated.
   2. **Non-official channel hosting the entity's lectures** – The uploading channel is not
      {entity_name}'s own, yet hosts lectures, courses, or educational material originally
      from {entity_name} (any length; clips and split parts count).
   3. **File-sharing links** – Description contains Google Drive links (drive.google.com),
      Mega links (mega.nz), MediaFire, or any cloud storage / download links.
   4. **"Free" / "leaked" bait** – Title or description says "free", "leaked", "paid course
      free", "premium free", "free download", "join telegram for more".
   5. **Course/subject keywords** – Title mentions course names, subject names, class numbers,
      exam names (JEE/NEET), batch names, "one shot", "full lecture", "complete chapter"
      alongside or referring to {entity_name}'s content.
   6. **Generic channel name** – Channel is named generically like "Study Hub", "Free Education",
      "JEE Academy", "Exam Preparation" — NOT {entity_name}'s official or affiliated channel.
   7. **Disabled comments/likes** – Pirates often disable comments to avoid copyright reports.
   Whatever the channel's size, if ANY of indicators 1-4 are present together with
   {entity_name}'s content, classify as PIRATED with high confidence.
   If the official-catalog evidence above reports a title match, treat that as strong support.
   When the description contains a Telegram link and the channel is NOT {entity_name}'s
   official channel, classify as PIRATED with 85+ confidence.

 • LEGITIMATE  – news coverage, political reporting, fair criticism, fan appreciation, reviews,
   educational discussion, opinion/debate, satire from established creators, or content from
   {entity_name}'s own official channel.
   IMPORTANT: If the channel has many subscribers (50K+), it is very likely a news outlet,
   media company, or established creator — classify as LEGITIMATE even if the title sounds
   sensational (e.g. "XYZ trolled", "XYZ exposed", "XYZ viral moment"). Mainstream media
   routinely uses such language for engagement; that does NOT make it defamatory.
   Content uploaded by {entity_name}'s OWN official channel is always LEGITIMATE.

Return JSON with these fields (no extra text):
{{
  "classification": "DEFAMATORY" | "PIRATED" | "LEGITIMATE",
  "confidence": <int 0-100>,
  "severity": "critical" | "high" | "medium" | "low",
  "reason": "<one-sentence explanation>"
}}
"""


def classify_text(
    video: dict,
    entity_name: str,
    entity_type: str = "generic",
    scan_type: str = "both",
    channel_info: Optional[dict] = None,
) -> dict:
    """Classify a single video's text metadata via LLM.

    Returns dict with keys: classification, confidence, severity, reason.
    On failure returns a LEGITIMATE classification with confidence 0.
    """
    subs = "unknown"
    if channel_info:
        subs = channel_info.get("statistics", {}).get("subscriberCount", "unknown")

    prompt = _CLASSIFY_USER.format(
        entity_name=entity_name,
        entity_type=entity_type,
        title=video.get("title", ""),
        channel_title=video.get("channel_title", ""),
        subs=subs,
        description=(video.get("description", "") or "")[:1500],
        tags=", ".join(video.get("tags", [])[:15]),
        duration=video.get("duration_formatted", ""),
        views=video.get("view_count", "N/A"),
        published=video.get("published_at", ""),
        scan_type=scan_type,
        evidence=_evidence_line(video),
    )

    down = llm_unavailable_reason()
    if down:
        from core import funnel

        funnel.current().error("llm_skipped_breaker_open", down)
        return _llm_failed_result(down)

    try:
        resp = _get_client().chat.completions.create(
            model=LLM_MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        result = json.loads(raw)
        for key in ("classification", "confidence", "severity", "reason"):
            if key not in result:
                result[key] = {"classification": "LEGITIMATE", "confidence": 0, "severity": "low", "reason": "parse error"}[key]
        _breaker["fatal_streak"] = 0
        return result
    except Exception as exc:
        logger.error("LLM text classification failed: %s", exc)
        _record_llm_failure(exc)
        return _llm_failed_result(str(exc)[:200])


def _evidence_line(video: dict) -> str:
    """Catalog evidence already computed for this video (helps precision)."""
    heur = video.get("heuristic") or {}
    cm = heur.get("catalog_match") or {}
    parts: List[str] = []
    if cm:
        parts.append(
            f"title is {float(cm.get('similarity') or 0):.0%} similar to official video "
            f"'{cm.get('official_title', '')}'"
        )
    signals = heur.get("signals") or []
    if "duration_match_official" in signals:
        parts.append("duration equals that official video")
    if any(s.startswith("channel_bulk_catalog_titles") for s in signals):
        parts.append("this channel hosts several videos titled like official ones")
    return "; ".join(parts) or "none"


def batch_classify_text(
    videos: List[dict],
    entity_name: str,
    entity_type: str = "generic",
    scan_type: str = "both",
    channel_map: Optional[Dict[str, dict]] = None,
) -> List[dict]:
    """Classify a list of videos. Returns list of classification dicts (same order)."""
    channel_map = channel_map or {}
    results: List[dict] = []
    for v in videos:
        ch = channel_map.get(v.get("channel_id", ""))
        r = classify_text(v, entity_name, entity_type, scan_type, ch)
        results.append(r)
    return results


# ---------------------------------------------------------------------------
# Thumbnail analysis (GPT-4o vision)
# ---------------------------------------------------------------------------

_THUMB_SYSTEM = (
    "You analyse YouTube video thumbnails for signs of defamatory or meme content "
    "targeting a specific person.  Return ONLY valid JSON – no markdown fences."
)

_THUMB_USER = """\
Does this YouTube thumbnail appear to be a meme, troll, or defamatory image \
targeting **{entity_name}**?

Look for:
- Distorted / edited photos of the person
- Negative text overlays (e.g. EXPOSED, SCAM, FAKE)
- Meme template formats, emoji spam, red circles/arrows clickbait
- Comparison layouts meant to mock

Return JSON:
{{
  "is_negative_thumbnail": true | false,
  "confidence": <int 0-100>,
  "description": "<brief description of what you see>"
}}
"""


def analyze_thumbnail(
    thumbnail_url: str,
    entity_name: str,
) -> dict:
    """Analyse a single thumbnail via GPT-4o vision.

    Returns dict: is_negative_thumbnail, confidence, description.
    """
    if not THUMBNAIL_ANALYSIS_ENABLED or not thumbnail_url or llm_unavailable_reason():
        return {"is_negative_thumbnail": False, "confidence": 0, "description": "skipped"}

    try:
        resp = _get_client().chat.completions.create(
            model=VISION_MODEL,
            temperature=0.1,
            max_tokens=300,
            messages=[
                {"role": "system", "content": _THUMB_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _THUMB_USER.format(entity_name=entity_name),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": thumbnail_url, "detail": "low"},
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        result = json.loads(raw)
        return {
            "is_negative_thumbnail": result.get("is_negative_thumbnail", False),
            "confidence": result.get("confidence", 0),
            "description": result.get("description", ""),
        }
    except Exception as exc:
        logger.error("Thumbnail analysis failed for %s: %s", thumbnail_url[:80], exc)
        return {"is_negative_thumbnail": False, "confidence": 0, "description": f"error: {exc}"}
