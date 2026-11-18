"""Telegram scan orchestrator – seeds → join/read → risk → optional fingerprint."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from config import (
    TELEGRAM_EXPAND_INVITES,
    TELEGRAM_MAX_CHANNELS,
    TELEGRAM_MAX_MEDIA_FINGERPRINTS,
    TELEGRAM_MESSAGES_PER_CHANNEL,
)
from core.telegram.client import (
    download_media_sample,
    fetch_channel_meta,
    iter_recent_messages,
    resolve_and_join,
    run_async,
    telegram_configured,
    telegram_status,
    _build_client,
    _ensure_connected,
)
from core.telegram.media_fp import fingerprint_local_media, match_media_to_catalog
from core.telegram.risk import (
    looks_like_media_dump,
    message_keyword_hits,
    score_channel,
)
from core.telegram.seeds import (
    extract_telegram_seeds,
    merge_seed_lists,
    parse_manual_seeds,
    seeds_from_videos,
)

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str], None]]


def _progress(cb: ProgressCb, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    logger.info(msg)


def _entity_terms(entity_name: str, extra: Optional[List[str]] = None) -> List[str]:
    parts = [p for p in (entity_name or "").split() if len(p) > 1]
    terms = [entity_name] + parts
    for t in extra or []:
        if t:
            terms.append(t)
    # unique preserve order
    seen = set()
    out = []
    for t in terms:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


async def _scan_async(
    seeds: List[dict],
    entity_name: str,
    catalog_fp_index: Optional[List[dict]] = None,
    max_channels: int = TELEGRAM_MAX_CHANNELS,
    messages_per_channel: int = TELEGRAM_MESSAGES_PER_CHANNEL,
    enable_fingerprinting: bool = True,
    progress_cb: ProgressCb = None,
) -> Dict[str, Any]:
    client = _build_client()
    hits: List[dict] = []
    channels_scored: List[dict] = []
    discovered_seeds: List[dict] = []
    stats = {
        "channels_attempted": 0,
        "channels_scanned": 0,
        "messages_scanned": 0,
        "media_fingerprinted": 0,
        "fingerprint_matches": 0,
        "keyword_hits": 0,
    }

    try:
        if not await _ensure_connected(client):
            return {
                "ok": False,
                "error": "Telegram session not authorized. Run scripts/telegram_login.py",
                "status": telegram_status(),
                "seeds": seeds,
                "channels": [],
                "hits": [],
                "stats": stats,
            }

        terms = _entity_terms(entity_name)
        media_budget = TELEGRAM_MAX_MEDIA_FINGERPRINTS if enable_fingerprinting else 0
        queue = list(seeds[:max_channels])
        scanned_keys = set()

        while queue and len(scanned_keys) < max_channels:
            seed = queue.pop(0)
            key = f"{seed.get('kind')}:{seed.get('value')}"
            if key in scanned_keys:
                continue
            scanned_keys.add(key)
            stats["channels_attempted"] += 1

            _progress(
                progress_cb,
                f"Telegram: resolving {seed.get('value')} ({len(scanned_keys)}/{max_channels})",
            )
            entity = await resolve_and_join(client, seed)
            if entity is None:
                channels_scored.append(
                    {
                        "seed": seed,
                        "error": "resolve_failed",
                        "risk": {"risk_score": 0, "severity": "low", "signals": ["unreachable"]},
                    }
                )
                continue

            meta = await fetch_channel_meta(client, entity)
            messages = await iter_recent_messages(client, entity, limit=messages_per_channel)
            stats["channels_scanned"] += 1
            stats["messages_scanned"] += len(messages)

            entity_hit_messages = 0
            media_count = 0
            invite_links_found = 0
            fp_matches = 0
            channel_hits: List[dict] = []

            for msg in messages:
                text = msg.get("text") or ""
                kw = message_keyword_hits(text, terms)
                media = msg.get("media")

                # Expand invite graph (1 hop)
                if TELEGRAM_EXPAND_INVITES and text:
                    nested = extract_telegram_seeds(
                        text, source=f"tg:{meta.get('username') or meta.get('id')}"
                    )
                    if nested:
                        invite_links_found += len(nested)
                        for ns in nested:
                            nkey = f"{ns.get('kind')}:{ns.get('value')}"
                            if nkey not in scanned_keys:
                                discovered_seeds.append(ns)
                                if len(queue) + len(scanned_keys) < max_channels * 2:
                                    queue.append(ns)

                is_interesting = kw["score"] >= 25
                media_dump = False
                if media and looks_like_media_dump(
                    media.get("filename") or "",
                    media.get("mime") or "",
                    int(media.get("size") or 0),
                ):
                    media_count += 1
                    media_dump = True
                    # Long media + any entity/piracy signal → interesting
                    if int(media.get("duration") or 0) >= 600 or kw["score"] >= 15:
                        is_interesting = True

                if kw["entity_hits"]:
                    entity_hit_messages += 1

                fp_match = None
                if (
                    is_interesting
                    and media_dump
                    and media_budget > 0
                    and catalog_fp_index
                ):
                    _progress(
                        progress_cb,
                        f"Telegram: fingerprinting media in {meta.get('title') or seed.get('value')}",
                    )
                    path = await download_media_sample(client, entity, msg["message_id"])
                    stats["media_fingerprinted"] += 1
                    media_budget -= 1
                    if path:
                        fp = fingerprint_local_media(path)
                        if fp:
                            fp_match = match_media_to_catalog(fp, catalog_fp_index)
                            if fp_match and fp_match.get("is_match"):
                                fp_matches += 1
                                stats["fingerprint_matches"] += 1
                                is_interesting = True

                if not is_interesting and not (fp_match and fp_match.get("is_match")):
                    continue

                stats["keyword_hits"] += 1
                conf = kw["score"]
                cls = "PIRATED"
                reason = "Telegram message matched entity/piracy signals"
                if fp_match and fp_match.get("is_match"):
                    conf = max(conf, int(fp_match["similarity"] * 100))
                    reason = (
                        f"Audio fingerprint match ({fp_match['similarity']:.0%}) "
                        f"with official: {fp_match.get('official_title') or fp_match.get('official_video_id')}"
                    )

                hit = {
                    "platform": "telegram",
                    "video_id": f"tg_{meta.get('id')}_{msg['message_id']}",
                    "title": (text[:120] if text else media.get("filename") if media else "Telegram media"),
                    "description": text,
                    "channel_id": str(meta.get("id") or ""),
                    "channel_title": meta.get("title") or seed.get("value"),
                    "channel_url": meta.get("url") or seed.get("url"),
                    "published_at": msg.get("date"),
                    "view_count": msg.get("views") or 0,
                    "message_id": msg["message_id"],
                    "media": media,
                    "seed": seed,
                    "keyword_analysis": kw,
                    "fingerprint_match": fp_match,
                    "classification": cls,
                    "confidence": conf,
                    "severity": "critical" if (fp_match and fp_match.get("is_strong")) else (
                        "high" if conf >= 60 else "medium"
                    ),
                    "reason": reason,
                    "url": (
                        f"{meta['url']}/{msg['message_id']}"
                        if meta.get("url")
                        else seed.get("url")
                    ),
                }
                channel_hits.append(hit)
                hits.append(hit)

            msg_stats = {
                "media_ratio": (media_count / len(messages)) if messages else 0,
                "entity_hit_messages": entity_hit_messages,
                "invite_links_found": invite_links_found,
                "fingerprint_matches": fp_matches,
                "messages_scanned": len(messages),
            }
            risk = score_channel(meta, msg_stats)
            channels_scored.append(
                {
                    "seed": seed,
                    "meta": meta,
                    "risk": risk,
                    "stats": msg_stats,
                    "hits_count": len(channel_hits),
                }
            )

        # Sort channels by risk
        channels_scored.sort(
            key=lambda c: (c.get("risk") or {}).get("risk_score", 0),
            reverse=True,
        )
        hits.sort(key=lambda h: h.get("confidence", 0), reverse=True)

        return {
            "ok": True,
            "status": telegram_status(),
            "seeds": seeds,
            "discovered_seeds": merge_seed_lists(discovered_seeds),
            "channels": channels_scored,
            "hits": hits,
            "stats": stats,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def run_telegram_scan(
    entity_name: str,
    manual_seeds: Optional[Any] = None,
    youtube_videos: Optional[List[dict]] = None,
    catalog_fp_index: Optional[List[dict]] = None,
    enable_fingerprinting: bool = True,
    max_channels: int = TELEGRAM_MAX_CHANNELS,
    progress_cb: ProgressCb = None,
) -> Dict[str, Any]:
    """Sync entrypoint for Flask workers.

    Always returns seed harvest. Full channel scan requires Telethon session.
    """
    yt_seeds = seeds_from_videos(youtube_videos or [])
    man_seeds = parse_manual_seeds(manual_seeds)
    seeds = merge_seed_lists(man_seeds, yt_seeds)

    base = {
        "ok": True,
        "mode": "seeds_only",
        "status": telegram_status(),
        "seeds": seeds,
        "youtube_bridge_seeds": len(yt_seeds),
        "manual_seeds": len(man_seeds),
        "discovered_seeds": [],
        "channels": [],
        "hits": [],
        "stats": {},
    }

    if not seeds:
        base["ok"] = False
        base["error"] = (
            "No Telegram seeds. Provide channel links/@usernames or run a YouTube "
            "piracy scan that surfaces t.me links in descriptions."
        )
        return base

    if not telegram_configured():
        _progress(
            progress_cb,
            "Telegram client not configured – returning seed harvest only",
        )
        base["warning"] = (
            "Telegram API not configured. Set TELEGRAM_API_ID / TELEGRAM_API_HASH "
            "and run scripts/telegram_login.py. Seed list from YouTube bridge is still useful."
        )
        return base

    _progress(progress_cb, f"Telegram: scanning {min(len(seeds), max_channels)} seed channels...")
    result = run_async(
        _scan_async(
            seeds=seeds,
            entity_name=entity_name,
            catalog_fp_index=catalog_fp_index,
            max_channels=max_channels,
            enable_fingerprinting=enable_fingerprinting,
            progress_cb=progress_cb,
        )
    )
    result["mode"] = "full"
    result["youtube_bridge_seeds"] = len(yt_seeds)
    result["manual_seeds"] = len(man_seeds)
    return result
