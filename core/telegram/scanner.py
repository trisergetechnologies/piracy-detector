"""Telegram scan orchestrator (public content only).

Flow:
  seeds (manual + YouTube bridge + stored) + global channel search with query
  variants -> read recent posts of each PUBLIC channel (never joined, private
  invites never imported) -> per-message evidence -> tiers
  (confirmed / likely / weak) -> crawl forwards, links and mentions found in
  hit messages.

FloodWait: short waits are slept through; a long one stops the run cleanly and
the unscanned channels go first on the next run (local JSON state file).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import (
    CATALOG_TITLE_MATCH_THRESHOLD,
    QUERY_MAX_NAME_VARIANTS,
    TELEGRAM_CACHE_DIR,
    TELEGRAM_EXPAND_INVITES,
    TELEGRAM_MAX_CHANNELS,
    TELEGRAM_MAX_MEDIA_FINGERPRINTS,
    TELEGRAM_MAX_SEARCH_QUERIES,
    TELEGRAM_MESSAGES_PER_CHANNEL,
    TELEGRAM_SEARCH_DELAY_SECONDS,
    TELEGRAM_SEARCH_SUFFIXES,
    TELEGRAM_TERM_STOPWORDS,
    TELEGRAM_WEAK_CONTEXT_HITS_PER_CHANNEL,
)
from core import funnel
from core.catalog import best_catalog_match, title_tokens
from core.heuristics import durations_match
from core.telegram.client import (
    FloodStop,
    download_media_sample,
    fetch_channel_meta,
    iter_recent_messages,
    resolve_public,
    run_async,
    search_public_channels,
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
    is_official_telegram_channel,
    merge_seed_lists,
    official_telegram_usernames,
    parse_manual_seeds,
    seeds_from_videos,
)

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str], None]]
_AV_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".m4a", ".ogg", ".wav")


def _progress(cb: ProgressCb, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    logger.info(msg)


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def _entity_terms(entity_name: str, extra: Optional[List[str]] = None) -> List[str]:
    """Strings that count as a mention of the rights holder.

    Single words are only used when the name has exactly one distinctive word
    ("PracBee", "VA-NEE-NI Excellence"). For multi-word names ("Physics
    Wallah", "Quant Finance By Subhadra") single words like "physics" or "by"
    matched nearly every post, so adjacent word pairs are used instead.
    """
    name = " ".join((entity_name or "").split())
    if not name:
        return [t for t in extra or [] if t]
    terms: List[str] = [name]
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if base and base != name:
        terms.append(base)

    words = [w.strip(".-_") for w in re.split(r"[\s/|,()]+", name)]
    words = [w for w in words if w]
    distinctive = [w for w in words if len(w) >= 3 and w.lower() not in TELEGRAM_TERM_STOPWORDS]
    if len(distinctive) == 1:
        terms.append(distinctive[0])
    elif len(words) > 2:
        for a, b in zip(words, words[1:]):
            if a in distinctive or b in distinctive:
                terms.append(f"{a} {b}")
    if 1 <= len(distinctive) <= 2:
        compact = re.sub(r"[^0-9a-z\u0900-\u097F]+", "", "".join(distinctive).lower())
        if len(compact) >= 5:
            terms.append(compact)
    terms += [t for t in extra or [] if t]

    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def search_queries(entity_name: str) -> List[str]:
    """Query variants for Telegram's public channel search."""
    name = " ".join((entity_name or "").split())
    if not name:
        return []
    bases = [name]
    for t in _entity_terms(name)[1:]:
        if len(bases) > QUERY_MAX_NAME_VARIANTS:
            break
        if t.lower() != name.lower():
            bases.append(t)
    out: List[str] = []
    for suffix in TELEGRAM_SEARCH_SUFFIXES:  # breadth first: every base, then next suffix
        for base in bases:
            q = f"{base} {suffix}".strip()
            if q.lower() not in (x.lower() for x in out):
                out.append(q)
    return out[: max(TELEGRAM_MAX_SEARCH_QUERIES, 0)]


def media_catalog_match(media: Optional[dict], text: str, catalog: Optional[dict]) -> Dict[str, Any]:
    """Compare a posted file (name, caption, duration) with the official catalog."""
    out: Dict[str, Any] = {"title_sim": 0.0, "duration_match": False}
    if not media or not catalog:
        return out
    title_index = catalog.get("title_index") or []
    duration_map = catalog.get("duration_map") or {}

    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", media.get("filename") or "")
    stem = re.sub(r"[_\-.]+", " ", stem)
    caption = (text or "").strip().split("\n")[0][:200]

    best = None
    for candidate in (stem, caption):
        if len(candidate) < 6:
            continue
        m = best_catalog_match(candidate, title_index, threshold=CATALOG_TITLE_MATCH_THRESHOLD)
        if m and (best is None or m["similarity"] > best["similarity"]):
            best = m
    if best is None and title_index:
        # Long captions dilute Jaccard: accept an official title fully contained in the text.
        toks = title_tokens(f"{stem} {(text or '')[:600]}")
        for entry in title_index:
            et = entry.get("tokens") or set()
            if len(et) >= 3 and len(et & toks) / len(et) >= 0.8:
                best = {"official_video_id": entry["video_id"], "official_title": entry["title"],
                        "similarity": round(len(et & toks) / len(et), 3)}
                break

    duration = int(media.get("duration") or 0)
    if best:
        out.update(title_sim=best["similarity"], official_video_id=best["official_video_id"],
                   official_title=best["official_title"])
        out["duration_match"] = durations_match(
            duration, int(duration_map.get(best["official_video_id"]) or 0)
        )
        if best.get("distinctive_shared", 2) < 2 and not out["duration_match"]:
            # Shares only the catalog's common course words (exam name, year):
            # same subject, not evidence of this lecture.
            return {"title_sim": 0.0, "duration_match": False}
    elif duration >= 120:
        for off_vid, off_dur in duration_map.items():
            if abs(duration - int(off_dur or 0)) <= 1:
                out.update(duration_match=True, official_video_id=off_vid)
                break
    return out


def classify_message(ev: Dict[str, Any]) -> Tuple[Optional[str], int, str]:
    """Evidence -> (tier, confidence, reason). Mirrors the YouTube tiers:
    confirmed 90-99 (content match), likely 70-89, weak <= 69 (text only).
    Audience size and view counts are not inputs."""
    fp = ev.get("fp") or {}
    if fp.get("is_match"):
        sim = float(fp.get("similarity") or 0)
        official = fp.get("official_title") or fp.get("official_video_id") or "official content"
        what = f"audio fingerprint ({fp.get('backend')}) matches official video '{official}' ({sim:.0%})"
        if fp.get("tier") == "confirmed":
            return "confirmed", min(max(90, int(round(sim * 100))), 99), what
        return "likely", min(89, 74 + int(sim * 15)), what

    media, entity, dump = ev.get("media"), ev.get("entity"), ev.get("dump_kw")
    title_sim = float(ev.get("title_sim") or 0)
    same_dur = bool(ev.get("duration_match"))
    official = ev.get("official_title") or ev.get("official_video_id") or ""

    if media and title_sim and same_dur:
        return "likely", 85, f"file name/caption and duration match official video '{official}'"
    if media and same_dur and entity:
        return "likely", 78, f"file has the same duration as official video '{official}' and the post/channel names the rights holder"
    if media and title_sim:
        return "likely", 72, f"file name/caption matches official video '{official}' ({title_sim:.0%})"
    if media and entity and dump:
        return "likely", 72, "media file posted with the rights holder's name and redistribution wording"
    if entity and dump:
        return "weak", 66, "post names the rights holder with redistribution wording (no file verified)"
    if media and entity:
        return "weak", 60, "media file in a post/channel naming the rights holder (content not verified)"
    if dump and ev.get("has_links"):
        return "weak", 50, "redistribution wording with links, rights holder not named"
    return None, 0, ""


# ---------------------------------------------------------------------------
# Resume state (local JSON; which channels were scanned / deferred)
# ---------------------------------------------------------------------------


def _state_path(entity_name: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", (entity_name or "entity").lower()).strip("_") or "entity"
    root = Path(TELEGRAM_CACHE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"scan_state_{slug}.json"


def load_state(entity_name: str) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(entity_name).read_text(encoding="utf-8"))
    except Exception:
        return {"scanned": {}, "deferred": []}


def save_state(entity_name: str, state: Dict[str, Any]) -> None:
    try:
        _state_path(entity_name).write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        logger.warning("Telegram state save failed: %s", exc)


def _seed_key(seed: dict) -> str:
    return f"{seed.get('kind')}:{str(seed.get('value') or '').lstrip('@').lower()}"


def order_seeds(seeds: List[dict], state: Dict[str, Any]) -> List[dict]:
    """Deferred channels first, then never-scanned, then least recently scanned."""
    deferred = set(state.get("deferred") or [])
    scanned = state.get("scanned") or {}

    def rank(s: dict) -> Tuple[int, float]:
        k = _seed_key(s)
        if k in deferred:
            return (0, 0.0)
        if k not in scanned:
            return (1, 0.0)
        return (2, float(scanned[k]))

    return sorted(seeds, key=rank)  # stable: input order kept inside each group


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


async def _scan_async(
    seeds: List[dict],
    entity_name: str,
    catalog_fp_index: Optional[List[dict]] = None,
    max_channels: int = TELEGRAM_MAX_CHANNELS,
    messages_per_channel: int = TELEGRAM_MESSAGES_PER_CHANNEL,
    enable_fingerprinting: bool = True,
    official_usernames: Optional[set] = None,
    progress_cb: ProgressCb = None,
    catalog: Optional[dict] = None,
) -> Dict[str, Any]:
    fn = funnel.current()
    client = _build_client()
    hits: List[dict] = []
    channels_scored: List[dict] = []
    discovered_seeds: List[dict] = []
    official_usernames = official_usernames or set()
    state = load_state(entity_name)
    stats = {
        "channels_attempted": 0,
        "channels_scanned": 0,
        "channels_inaccessible": 0,
        "messages_scanned": 0,
        "media_fingerprinted": 0,
        "fingerprint_matches": 0,
        "keyword_hits": 0,
        "official_channels_skipped_hits": 0,
        "soft_signals_ignored": 0,
        "search_queries": 0,
        "search_channels_found": 0,
        "confirmed": 0, "likely": 0, "weak": 0,
        "stopped_reason": None,
    }

    try:
        if not await _ensure_connected(client):
            fn.error("telegram_auth", "session not authorized")
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
        terms_l = [t.lower() for t in terms]
        media_budget = TELEGRAM_MAX_MEDIA_FINGERPRINTS if enable_fingerprinting else 0
        flood: Optional[FloodStop] = None

        # ---- Discovery: Telegram's public channel search, with query variants
        found: List[dict] = []
        for q in search_queries(entity_name):
            _progress(progress_cb, f"Telegram: searching public channels for '{q}'")
            try:
                res = await search_public_channels(client, q)
            except FloodStop as exc:
                flood = exc
                fn.error("telegram_floodwait", str(exc))
                fn.query("telegram_search", q, 0, 0, error=str(exc))
                break
            except Exception as exc:
                fn.error("telegram_search", f"{q}: {exc}")
                fn.query("telegram_search", q, 0, 0, error=str(exc))
                continue
            stats["search_queries"] += 1
            known = {_seed_key(s) for s in seeds + found}
            new = [s for s in res if _seed_key(s) not in known]
            fn.query("telegram_search", q, len(res), len(new))
            found.extend(new)
            await asyncio.sleep(TELEGRAM_SEARCH_DELAY_SECONDS)
        stats["search_channels_found"] = len(found)
        discovered_seeds.extend(found)

        queue = order_seeds(merge_seed_lists(seeds, found), state)
        fn.stage("queries", stats["search_queries"])
        fn.stage("raw_results", len(queue))
        scanned_keys: set = set()
        queued_keys = {_seed_key(s) for s in queue}

        def _enqueue(new_seeds: List[dict], front: bool) -> None:
            for ns in new_seeds:
                k = _seed_key(ns)
                if k in queued_keys:
                    continue
                queued_keys.add(k)
                discovered_seeds.append(ns)
                if front:
                    queue.insert(0, ns)
                else:
                    queue.append(ns)

        while queue and stats["channels_attempted"] < max_channels and not flood:
            seed = queue.pop(0)
            key = _seed_key(seed)
            if key in scanned_keys:
                continue
            scanned_keys.add(key)
            stats["channels_attempted"] += 1
            label = seed.get("value")

            _progress(
                progress_cb,
                f"Telegram: reading {label} ({stats['channels_attempted']}/{max_channels})",
            )
            try:
                entity, why = await resolve_public(client, seed)
                if entity is None:
                    stats["channels_inaccessible"] += 1
                    fn.unreachable(f"telegram:{label}", why)
                    fn.drop(f"telegram:{label}", "access", why.split(":")[0])
                    channels_scored.append({
                        "seed": seed,
                        "error": why,
                        "risk": {"risk_score": 0, "severity": "low", "signals": ["unreachable"]},
                    })
                    state.setdefault("scanned", {})[key] = time.time()
                    continue

                meta = await fetch_channel_meta(client, entity)
                messages = await iter_recent_messages(client, entity, limit=messages_per_channel)
            except FloodStop as exc:
                flood = exc
                scanned_keys.discard(key)  # not scanned: retried first on the next run
                queue.insert(0, seed)
                break
            except Exception as exc:
                # FloodWaitError from Telethon (wait longer than the configured maximum)
                seconds = getattr(exc, "seconds", None)
                if seconds is not None:
                    flood = FloodStop(int(seconds), f"read:{label}")
                    scanned_keys.discard(key)
                    queue.insert(0, seed)
                    break
                stats["channels_inaccessible"] += 1
                reason = type(exc).__name__
                fn.unreachable(f"telegram:{label}", f"{reason}: {str(exc)[:120]}")
                fn.drop(f"telegram:{label}", "access", reason)
                channels_scored.append({
                    "seed": seed,
                    "error": reason,
                    "risk": {"risk_score": 0, "severity": "low", "signals": ["unreachable"]},
                })
                continue

            state.setdefault("scanned", {})[key] = time.time()
            stats["channels_scanned"] += 1
            stats["messages_scanned"] += len(messages)

            is_official = bool(getattr(entity, "verified", False)) or is_official_telegram_channel(
                official_usernames, seed=seed, meta=meta
            )
            if is_official:
                _progress(
                    progress_cb,
                    f"Telegram: official channel {meta.get('username') or label} "
                    "(link discovery only; posts not flagged)",
                )

            profile = f"{meta.get('title') or ''} {meta.get('about') or ''}".lower()
            channel_names_entity = any(t in profile for t in terms_l)

            entity_hit_messages = 0
            media_count = 0
            invite_links_found = 0
            fp_matches = 0
            catalog_matches = 0
            context_only_hits = 0
            channel_hits: List[dict] = []
            other_links: List[dict] = []

            for msg in messages:
                text = msg.get("text") or ""
                kw = message_keyword_hits(text, terms)
                media = msg.get("media")
                links = extract_telegram_seeds(
                    text, source=f"tg:{meta.get('username') or meta.get('id')}"
                ) if text else []
                fwd = msg.get("forwarded_from")
                if fwd:
                    links.append({
                        "kind": "public", "value": fwd["username"],
                        "url": f"https://t.me/{fwd['username']}",
                        "source": f"forward:{meta.get('username') or meta.get('id')}",
                    })
                invite_links_found += len(links)

                dump_kw = bool(kw.get("dump_hits"))
                media_dump = bool(media and looks_like_media_dump(
                    media.get("filename") or "",
                    media.get("mime") or "",
                    int(media.get("size") or 0),
                ))
                if media_dump:
                    media_count += 1
                if kw["entity_hits"]:
                    entity_hit_messages += 1

                cm = media_catalog_match(media, text, catalog) if media_dump else {}
                msg_entity = bool(kw["entity_hits"])
                ev: Dict[str, Any] = {
                    "media": media_dump,
                    "entity": msg_entity or channel_names_entity,
                    "dump_kw": dump_kw,
                    "has_links": bool(links),
                    "title_sim": cm.get("title_sim", 0.0),
                    "duration_match": cm.get("duration_match", False),
                    "official_video_id": cm.get("official_video_id"),
                    "official_title": cm.get("official_title"),
                }
                tier, conf, reason = classify_message(ev)

                # Short partial download -> audio fingerprint, for files that already
                # carry some evidence (cheap checks first).
                fp_match = None
                name_l = (media.get("filename") or "").lower() if media else ""
                is_av = bool(media) and (
                    (media.get("mime") or "").startswith(("video/", "audio/"))
                    or name_l.endswith(_AV_EXTS)
                )
                if tier and is_av and media_budget > 0 and catalog_fp_index and not is_official:
                    _progress(
                        progress_cb,
                        f"Telegram: verifying a file in {meta.get('title') or label}",
                    )
                    media_budget -= 1
                    stats["media_fingerprinted"] += 1
                    path = None
                    try:
                        path = await download_media_sample(client, entity, msg["message_id"])
                    except FloodStop as exc:
                        flood = exc
                    if path:
                        fp = fingerprint_local_media(path)
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        if fp:
                            fp_match = match_media_to_catalog(fp, catalog_fp_index)
                            if fp_match and fp_match.get("is_match"):
                                fp_matches += 1
                                stats["fingerprint_matches"] += 1
                                ev["fp"] = fp_match
                                tier, conf, reason = classify_message(ev)
                        else:
                            fn.drop(f"tg_{meta.get('id')}_{msg['message_id']}", "verify_audio",
                                    "partial_download_not_decodable", final=False)
                elif tier and is_av and catalog_fp_index and not is_official and media_budget <= 0:
                    fn.drop(f"tg_{meta.get('id')}_{msg['message_id']}", "verify_audio",
                            "media_budget", final=False)

                if not tier:
                    if kw["score"] > 0 or kw["entity_hits"]:
                        stats["soft_signals_ignored"] += 1
                    other_links.extend(links)
                    continue

                # Official whitelist: the rights holder's own posts are never hits
                if is_official:
                    stats["official_channels_skipped_hits"] += 1
                    fn.drop(f"tg_{meta.get('id')}_{msg['message_id']}", "filter", "official_content")
                    continue

                # A channel that merely names the rights holder would turn every file
                # into a weak hit; keep a few and say how many more there are.
                if tier == "weak" and not msg_entity and not dump_kw:
                    context_only_hits += 1
                    if context_only_hits > TELEGRAM_WEAK_CONTEXT_HITS_PER_CHANNEL:
                        fn.drop(f"tg_{meta.get('id')}_{msg['message_id']}", "threshold",
                                "weak_similar_files_cap")
                        continue

                if cm.get("title_sim") or cm.get("duration_match"):
                    catalog_matches += 1
                stats["keyword_hits"] += 1
                stats[tier] += 1
                signals = [k for k in ("media", "entity", "dump_kw", "duration_match") if ev.get(k)]
                if ev["title_sim"]:
                    signals.append(f"catalog_title_match:{ev['title_sim']}")
                if fp_match and fp_match.get("is_match"):
                    signals.append("audio_fingerprint_match")
                signals.append(f"tier:{tier}")

                hit = {
                    "platform": "telegram",
                    "video_id": f"tg_{meta.get('id')}_{msg['message_id']}",
                    "title": (text[:120] if text else media.get("filename") if media else "Telegram media"),
                    "description": text,
                    "channel_id": str(meta.get("id") or ""),
                    "channel_title": meta.get("title") or label,
                    "channel_url": meta.get("url") or seed.get("url"),
                    "published_at": msg.get("date"),
                    "view_count": msg.get("views") or 0,
                    "message_id": msg["message_id"],
                    "media": media,
                    "seed": seed,
                    "keyword_analysis": kw,
                    "fingerprint_match": fp_match,
                    "heuristic": {
                        "heuristic_score": conf,
                        "signals": signals,
                        "catalog_match": (
                            {"official_video_id": cm.get("official_video_id"),
                             "official_title": cm.get("official_title", ""),
                             "similarity": cm.get("title_sim", 0.0)}
                            if cm.get("official_video_id") else None
                        ),
                    },
                    "classification": "PIRATED",
                    "confidence": conf,
                    "severity": {"confirmed": "critical", "likely": "high"}.get(tier, "medium"),
                    "reason": f"{tier.upper()}: {reason}",
                    "url": (
                        f"{meta['url']}/{msg['message_id']}"
                        if meta.get("url")
                        else seed.get("url")
                    ),
                }
                channel_hits.append(hit)
                hits.append(hit)

                # Snowball: sources forwarded/linked/mentioned in a hit are scanned next
                # (only hits tied to this rights holder, not generic dump posts).
                if TELEGRAM_EXPAND_INVITES and links and (tier != "weak" or ev["entity"]):
                    _enqueue(links, front=tier in ("confirmed", "likely"))
                if flood:
                    break

            # Other links posted by a channel that produced likely/confirmed hits.
            if TELEGRAM_EXPAND_INVITES and not is_official and any(
                h["confidence"] >= 70 for h in channel_hits
            ):
                _enqueue(other_links, front=False)

            msg_stats = {
                "media_ratio": (media_count / len(messages)) if messages else 0,
                "entity_hit_messages": entity_hit_messages,
                "invite_links_found": invite_links_found,
                "fingerprint_matches": fp_matches,
                "catalog_matches": catalog_matches,
                "messages_scanned": len(messages),
                "is_official": is_official,
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

        # ---- What was not scanned, and why
        if flood:
            stats["stopped_reason"] = f"FloodWait {flood.seconds}s ({flood.where})"
            fn.error("telegram_floodwait", str(flood))
        elif queue:
            stats["stopped_reason"] = f"channel limit ({max_channels}) reached"
        left = [s for s in queue if _seed_key(s) not in scanned_keys]
        for s in left:
            reason = "private_invite_not_joined" if s.get("kind") == "invite" else (
                "floodwait" if flood else "channel_cap")
            fn.drop(f"telegram:{s.get('value')}", "access" if reason != "channel_cap" else "cap",
                    reason, final=False)
        state["deferred"] = [_seed_key(s) for s in left if s.get("kind") != "invite"][:500]
        save_state(entity_name, state)
        stats["channels_deferred"] = len(state["deferred"])

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
    official_telegram: Optional[Any] = None,
    progress_cb: ProgressCb = None,
    catalog: Optional[dict] = None,
) -> Dict[str, Any]:
    """Sync entrypoint for Flask workers.

    Seeds are optional: without any, channels are discovered through
    Telegram's public search. A full scan requires a Telethon session.
    """
    yt_seeds = seeds_from_videos(youtube_videos or [])
    man_seeds = parse_manual_seeds(manual_seeds)
    seeds = merge_seed_lists(man_seeds, yt_seeds)
    official = official_telegram_usernames(official_telegram)

    base = {
        "ok": True,
        "mode": "seeds_only",
        "status": telegram_status(),
        "seeds": seeds,
        "youtube_bridge_seeds": len(yt_seeds),
        "manual_seeds": len(man_seeds),
        "official_telegram": sorted(official),
        "discovered_seeds": [],
        "channels": [],
        "hits": [],
        "stats": {},
    }

    if not telegram_configured():
        _progress(
            progress_cb,
            "Telegram client not configured – returning seed harvest only",
        )
        funnel.current().error("telegram_not_configured", "no API id/hash or session")
        base["warning"] = (
            "Telegram API not configured. Set TELEGRAM_API_ID / TELEGRAM_API_HASH "
            "and run scripts/telegram_login.py. Nothing on Telegram was scanned."
        )
        if not seeds:
            base["ok"] = False
            base["error"] = base["warning"]
        return base

    if official:
        _progress(
            progress_cb,
            f"Telegram: whitelisting {len(official)} official channel(s)",
        )

    _progress(progress_cb, f"Telegram: {len(seeds)} seed channel(s) + public channel search...")
    result = run_async(
        _scan_async(
            seeds=seeds,
            entity_name=entity_name,
            catalog_fp_index=catalog_fp_index,
            max_channels=max_channels,
            enable_fingerprinting=enable_fingerprinting,
            official_usernames=official,
            progress_cb=progress_cb,
            catalog=catalog,
        )
    )
    result["mode"] = "full"
    result["youtube_bridge_seeds"] = len(yt_seeds)
    result["manual_seeds"] = len(man_seeds)
    result["official_telegram"] = sorted(official)
    if not official:
        result["warning"] = (
            "No official Telegram channel was given, so the rights holder's own "
            "channel (unless Telegram-verified) may appear among the hits."
        )
    return result
