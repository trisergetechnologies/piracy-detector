"""Unified YouTube Piracy + Defamatory Content Detection API.

Phase A discovery upgrades (piracy):
  - Official catalog ingest (channel / playlist IDs)
  - Anti-viral search (date/relevance + long duration; no viewCount)
  - Obfuscated / brand-light query expansion
  - Deep-crawl of suspicious small channels
  - Cheap title/duration/channel heuristics to prioritize low-view copies

Phase B proof layer:
  - Audio fingerprint catalog index + suspect matching
  - Strong matches override LLM → PIRATED with match% confidence

Phase C Telegram (sibling platform):
  - Seed harvest from YouTube descriptions + manual seeds
  - Telethon channel scan, invite graph expansion, risk scores
  - Same fingerprint DB for Telegram media dumps

Phase D product layer:
  - Continuous monitors + alerts
  - False-positive review queue
  - Multi-platform DMCA / takedown packs

Endpoints
---------
POST /scan                 – full scan (piracy + defamatory)  [async]
POST /scan/defamatory      – defamatory-only [async]
POST /scan/piracy          – piracy-only [async]
POST /scan/telegram        – Telegram piracy scan [async]
GET  /scan/status/<scan_id>– poll scan progress
GET  /results/<scan_id>    – fetch results of a past scan
GET  /history              – list recent scans
GET  /telegram/seeds       – list harvested Telegram seeds
POST /report/takedown      – generate takedown report for flagged videos
GET/POST /monitors         – list / create continuous monitors
PATCH/DELETE /monitors/<id>
GET  /alerts               – list alerts
POST /alerts/read          – mark alerts read
GET  /review               – review queue (pending hits)
POST /review               – confirm / reject hit
GET  /health               – service health
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

import db
from config import (
    DEFAMATORY_BIG_CHANNEL_MIN_CONFIDENCE,
    DEFAMATORY_BIG_CHANNEL_THRESHOLD,
    DEFAULT_MAX_RESULTS,
    FINGERPRINT_ENABLED,
    MIN_CONFIDENCE,
    MONITOR_DEFAULT_INTERVAL_HOURS,
    TAKEDOWN_REQUIRE_CONFIRMED,
    THUMBNAIL_ANALYSIS_ENABLED,
)
from core.catalog import ingest_official_catalog
from core.content_analyzer import (
    analyze_thumbnail,
    batch_classify_text,
)
from core.fingerprint_match import build_catalog_fingerprints, run_fingerprint_matching
from core.heuristics import annotate_and_prioritize
from core.monitor import (
    create_entity_monitor,
    emit_new_hit_alerts,
    filter_truly_new_hits,
    start_scheduler,
)
from core.scorer import score_videos
from core.search_engine import discover_videos
from core.telegram.client import telegram_status
from core.telegram.scanner import run_telegram_scan
from core.telegram.seeds import seeds_from_videos
from core.youtube_client import (
    YouTubeClient,
    format_duration,
    parse_duration,
    safe_int,
)
from report_generator import generate_takedown_report

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

yt_client = YouTubeClient()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Accept ISO date, 'today', 'yesterday', '1 week', '1 month', etc."""
    if not raw:
        return None
    low = raw.strip().lower()
    now = datetime.utcnow()
    from datetime import timedelta

    shortcuts = {
        "today": now,
        "yesterday": now - timedelta(days=1),
        "1 week": now - timedelta(weeks=1),
        "1 week ago": now - timedelta(weeks=1),
        "1 month": now - timedelta(days=30),
        "1 month ago": now - timedelta(days=30),
    }
    if low in shortcuts:
        return shortcuts[low].strftime("%Y-%m-%dT00:00:00Z")
    # Try ISO parse
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def _do_scan_work(
    scan_id: str,
    entity_name: str,
    entity_type: str,
    scan_type: str,
    max_results: int,
    min_conf: int,
    date_from: Optional[str],
    date_to: Optional[str],
    known_channels: List[str],
    do_thumbnails: bool,
    official_channel_ids: Optional[List[str]] = None,
    official_playlist_ids: Optional[List[str]] = None,
    enable_deep_crawl: bool = True,
    enable_fingerprinting: bool = True,
) -> None:
    """Heavy scan work that runs in a background thread."""
    try:
        logger.info("=== Scan %s started: %s (%s) [%s] ===", scan_id, entity_name, entity_type, scan_type)
        fp_summary: dict = {"enabled": False, "matches": 0}

        # ----- Phase 0: Official catalog (rights-holder reference) -----
        catalog: dict = {
            "video_ids": set(),
            "channel_ids": set(),
            "entries": [],
            "title_index": [],
        }
        if scan_type in ("both", "piracy") and (official_channel_ids or official_playlist_ids):
            db.update_scan_progress(scan_id, "Ingesting official content catalog...")
            catalog = ingest_official_catalog(
                yt_client,
                official_channel_ids=official_channel_ids,
                official_playlist_ids=official_playlist_ids,
            )
            logger.info(
                "Catalog: %d official videos, %d owned channels",
                len(catalog.get("video_ids") or []),
                len(catalog.get("channel_ids") or []),
            )

        # ----- Phase 1: Discovery (anti-viral + deep crawl) -----
        db.update_scan_progress(scan_id, "Discovering videos (anti-viral + deep crawl)...")
        raw_videos = discover_videos(
            entity_name=entity_name,
            entity_type=entity_type,
            scan_type=scan_type,
            date_from=date_from,
            date_to=date_to,
            max_results_per_query=30,
            known_channel_ids=known_channels or None,
            yt_client=yt_client,
            catalog=catalog,
            enable_deep_crawl=enable_deep_crawl and scan_type in ("both", "piracy"),
        )
        logger.info("Phase 1 complete: %d raw videos discovered", len(raw_videos))

        if not raw_videos:
            summary = {
                "total_analyzed": 0,
                "defamatory_found": 0,
                "pirated_found": 0,
                "legitimate_filtered": 0,
                "catalog_videos": len(catalog.get("video_ids") or []),
            }
            db.complete_scan(scan_id, summary)
            return

        # Soft cap before enrichment (heuristics will re-prioritize after)
        enrich_cap = min(max(max_results * 2, max_results), 500)
        raw_videos = raw_videos[:enrich_cap]

        # ----- Phase 1b: Enrich via YouTube Data API -----
        db.update_scan_progress(scan_id, f"Enriching {len(raw_videos)} videos...")
        video_ids = [v["video_id"] for v in raw_videos]
        video_details_list = yt_client.get_video_details(video_ids)
        video_details_map: Dict[str, dict] = {v["id"]: v for v in video_details_list}

        channel_ids_needed: set[str] = set()
        for v in raw_videos:
            detail = video_details_map.get(v["video_id"], {})
            snippet = detail.get("snippet", {})
            stats = detail.get("statistics", {})
            cd = detail.get("contentDetails", {})

            v["title"] = v.get("title") or snippet.get("title", "")
            v["channel_id"] = v.get("channel_id") or snippet.get("channelId", "")
            v["channel_title"] = v.get("channel_title") or snippet.get("channelTitle", "")
            v["description"] = v.get("description") or snippet.get("description", "")
            v["tags"] = snippet.get("tags", [])
            v["published_at"] = v.get("published_at") or snippet.get("publishedAt", "")
            v["thumbnail_url"] = v.get("thumbnail_url") or snippet.get("thumbnails", {}).get("high", {}).get("url", "")
            v["view_count"] = safe_int(stats.get("viewCount"))
            v["like_count"] = safe_int(stats.get("likeCount"))
            v["comment_count"] = safe_int(stats.get("commentCount"))
            dur = parse_duration(cd.get("duration", ""))
            v["duration_seconds"] = dur
            v["duration_formatted"] = format_duration(dur)

            if v["channel_id"]:
                channel_ids_needed.add(v["channel_id"])

        channel_map: Dict[str, dict] = yt_client.get_channels_batch(list(channel_ids_needed))
        logger.info("Phase 1b complete: enriched %d videos, %d channels", len(raw_videos), len(channel_map))

        # ----- Phase 1c: Cheap heuristics – prioritize low-view likely pirates -----
        db.update_scan_progress(scan_id, "Prioritizing low-view / catalog-similar candidates...")
        drop_official = scan_type in ("both", "piracy")
        raw_videos = annotate_and_prioritize(
            raw_videos,
            catalog=catalog,
            channel_map=channel_map,
            drop_official=drop_official,
        )
        raw_videos = raw_videos[:max_results]
        # Keep video_details_map aligned with trimmed set
        keep_ids = {v["video_id"] for v in raw_videos}
        video_details_map = {k: v for k, v in video_details_map.items() if k in keep_ids}

        # ----- Phase 1d: Audio fingerprint matching (Phase B) -----
        do_fp = (
            enable_fingerprinting
            and FINGERPRINT_ENABLED
            and scan_type in ("both", "piracy")
            and bool(catalog.get("entries"))
        )
        if do_fp:
            def _fp_progress(msg: str) -> None:
                db.update_scan_progress(scan_id, msg)

            fp_summary = run_fingerprint_matching(
                catalog=catalog,
                candidates=raw_videos,
                progress_cb=_fp_progress,
            )
            # Persist fingerprints for reuse across scans
            for v in raw_videos:
                fp = v.get("fingerprint")
                if fp and v.get("video_id"):
                    try:
                        db.save_fingerprint(v["video_id"], fp)
                    except Exception:
                        pass
            logger.info(
                "Phase 1d fingerprinting: %s",
                {k: fp_summary.get(k) for k in (
                    "catalog_fingerprinted", "candidates_tested",
                    "matches", "strong_matches", "skipped_reason",
                )},
            )
        else:
            fp_summary = {
                "enabled": False,
                "matches": 0,
                "skipped_reason": (
                    "disabled"
                    if not enable_fingerprinting or not FINGERPRINT_ENABLED
                    else "no_official_catalog"
                ),
            }

        # ----- Phase 2: AI Classification -----
        db.update_scan_progress(scan_id, f"Classifying {len(raw_videos)} videos with AI...")
        text_results = batch_classify_text(
            raw_videos, entity_name, entity_type, scan_type, channel_map
        )

        thumb_results: List[dict] = []
        for i, v in enumerate(raw_videos):
            txt = text_results[i]
            if (
                do_thumbnails
                and txt.get("classification") != "LEGITIMATE"
                and v.get("thumbnail_url")
            ):
                db.update_scan_progress(scan_id, f"Analyzing thumbnails ({i+1}/{len(raw_videos)})...")
                tr = analyze_thumbnail(v["thumbnail_url"], entity_name)
            else:
                tr = {"is_negative_thumbnail": False, "confidence": 0, "description": "skipped"}
            thumb_results.append(tr)

        logger.info("Phase 2 complete: classified %d videos", len(raw_videos))

        # ----- Phase 3: Scoring -----
        db.update_scan_progress(scan_id, "Scoring and filtering results...")
        scored = score_videos(raw_videos, text_results, thumb_results, video_details_map, channel_map)

        defamatory: List[dict] = []
        pirated: List[dict] = []
        legitimate_count = 0

        want_defamatory = scan_type in ("both", "defamatory")
        want_piracy = scan_type in ("both", "piracy")

        for v in scored:
            sr = v.get("score_result", {})
            cls = sr.get("classification", "LEGITIMATE")
            conf = sr.get("confidence", 0)

            # Skip classifications that don't match the requested scan_type
            if cls == "DEFAMATORY" and not want_defamatory:
                legitimate_count += 1
                continue
            if cls == "PIRATED" and not want_piracy:
                legitimate_count += 1
                continue

            # Safety net: if the channel is large and the LLM said DEFAMATORY,
            # require a much higher confidence before treating it as defamatory.
            if cls == "DEFAMATORY":
                ch_info = channel_map.get(v.get("channel_id", ""))
                ch_subs = safe_int(
                    (ch_info or {}).get("statistics", {}).get("subscriberCount")
                )
                if ch_subs >= DEFAMATORY_BIG_CHANNEL_THRESHOLD:
                    if conf < DEFAMATORY_BIG_CHANNEL_MIN_CONFIDENCE:
                        logger.info(
                            "Skipping DEFAMATORY for big channel %s (%d subs, conf=%d < %d)",
                            v.get("channel_title", "?"), ch_subs, conf,
                            DEFAMATORY_BIG_CHANNEL_MIN_CONFIDENCE,
                        )
                        legitimate_count += 1
                        continue

            if cls == "DEFAMATORY" and conf >= min_conf:
                v["platform"] = v.get("platform") or "youtube"
                v["review_status"] = v.get("review_status") or "pending"
                defamatory.append(v)
                db.save_detected_video(scan_id, {**v, "entity_name": entity_name})
                if v.get("channel_id"):
                    db.upsert_channel(v["channel_id"], {
                        "channel_title": v.get("channel_title", ""),
                        "platform": "youtube",
                        "last_offence": datetime.utcnow().isoformat() + "Z",
                    })
            elif cls == "PIRATED" and conf >= min_conf:
                v["platform"] = v.get("platform") or "youtube"
                v["review_status"] = v.get("review_status") or "pending"
                pirated.append(v)
                db.save_detected_video(scan_id, {**v, "entity_name": entity_name})
                if v.get("channel_id"):
                    db.upsert_channel(v["channel_id"], {
                        "channel_title": v.get("channel_title", ""),
                        "platform": "youtube",
                        "last_offence": datetime.utcnow().isoformat() + "Z",
                    })
            else:
                legitimate_count += 1

        # Phase C bridge: harvest Telegram seeds from piracy-relevant descriptions
        tg_seed_count = 0
        if want_piracy:
            bridge_pool = pirated + [
                v for v in scored
                if (v.get("heuristic") or {}).get("heuristic_score", 0) >= 40
            ]
            tg_seeds = seeds_from_videos(bridge_pool or scored)
            if tg_seeds:
                db.save_telegram_seeds(entity_name, tg_seeds, scan_id=scan_id)
                tg_seed_count = len(tg_seeds)
                logger.info("Phase C bridge: harvested %d Telegram seeds", tg_seed_count)

        # Phase D: alert on first-seen hits
        all_flagged = defamatory + pirated
        fresh = filter_truly_new_hits(entity_name, all_flagged, scan_id)
        alerts_created = emit_new_hit_alerts(scan_id, entity_name, fresh, platform="youtube")

        summary = {
            "total_analyzed": len(scored),
            "defamatory_found": len(defamatory),
            "pirated_found": len(pirated),
            "legitimate_filtered": legitimate_count,
            "catalog_videos": len(catalog.get("video_ids") or []),
            "deep_crawl_enabled": bool(enable_deep_crawl and scan_type in ("both", "piracy")),
            "fingerprint": fp_summary,
            "telegram_seeds_harvested": tg_seed_count,
            "new_alerts": alerts_created,
        }
        db.complete_scan(scan_id, summary)

        logger.info("=== Scan %s done: %d defamatory, %d pirated, %d legitimate ===",
                     scan_id, len(defamatory), len(pirated), legitimate_count)

    except Exception as exc:
        logger.error("Scan %s failed: %s\n%s", scan_id, exc, traceback.format_exc())
        db.complete_scan(scan_id, {"error": str(exc)})


def _do_telegram_scan_work(
    scan_id: str,
    entity_name: str,
    manual_seeds: Any,
    official_channel_ids: List[str],
    official_playlist_ids: List[str],
    enable_fingerprinting: bool,
    bridge_youtube_scan_id: Optional[str],
    min_conf: int,
) -> None:
    """Background Telegram scan (Phase C)."""
    try:
        logger.info("=== Telegram scan %s started: %s ===", scan_id, entity_name)
        db.update_scan_progress(scan_id, "Preparing Telegram seeds...")

        youtube_videos: List[dict] = []
        if bridge_youtube_scan_id:
            youtube_videos = db.get_detected_videos(bridge_youtube_scan_id)
            db.update_scan_progress(
                scan_id,
                f"Bridging {len(youtube_videos)} YouTube hits → Telegram seeds...",
            )

        catalog_fp_index: List[dict] = []
        if enable_fingerprinting and (official_channel_ids or official_playlist_ids):
            db.update_scan_progress(scan_id, "Building official fingerprint index for TG media...")
            catalog = ingest_official_catalog(
                yt_client,
                official_channel_ids=official_channel_ids,
                official_playlist_ids=official_playlist_ids,
            )
            catalog_fp_index = build_catalog_fingerprints(catalog)

        def _prog(msg: str) -> None:
            db.update_scan_progress(scan_id, msg)

        result = run_telegram_scan(
            entity_name=entity_name,
            manual_seeds=manual_seeds,
            youtube_videos=youtube_videos,
            catalog_fp_index=catalog_fp_index or None,
            enable_fingerprinting=enable_fingerprinting,
            progress_cb=_prog,
        )

        if result.get("seeds"):
            db.save_telegram_seeds(entity_name, result["seeds"], scan_id=scan_id)
        if result.get("discovered_seeds"):
            db.save_telegram_seeds(entity_name, result["discovered_seeds"], scan_id=scan_id)

        hits = result.get("hits") or []
        saved = 0
        saved_hits: List[dict] = []
        for hit in hits:
            if int(hit.get("confidence") or 0) < min_conf:
                continue
            # Normalize into detected_videos shape
            record = {
                **hit,
                "entity_name": entity_name,
                "platform": "telegram",
                "review_status": "pending",
                "score_result": {
                    "classification": hit.get("classification", "PIRATED"),
                    "confidence": hit.get("confidence", 0),
                    "severity": hit.get("severity", "medium"),
                    "reason": hit.get("reason", ""),
                    "final_score": float(hit.get("confidence") or 0),
                    "fingerprint_match": hit.get("fingerprint_match"),
                },
            }
            db.save_detected_video(scan_id, record)
            saved_hits.append(record)
            if hit.get("channel_id"):
                db.upsert_channel(str(hit["channel_id"]), {
                    "channel_title": hit.get("channel_title", ""),
                    "platform": "telegram",
                    "last_offence": datetime.utcnow().isoformat() + "Z",
                })
            saved += 1

        fresh = filter_truly_new_hits(entity_name, saved_hits, scan_id)
        alerts_created = emit_new_hit_alerts(
            scan_id, entity_name, fresh, platform="telegram"
        )

        summary = {
            "platform": "telegram",
            "mode": result.get("mode"),
            "total_analyzed": (result.get("stats") or {}).get("messages_scanned", 0),
            "defamatory_found": 0,
            "pirated_found": saved,
            "legitimate_filtered": max(len(hits) - saved, 0),
            "new_alerts": alerts_created,
            "telegram": {
                "seeds": len(result.get("seeds") or []),
                "discovered_seeds": len(result.get("discovered_seeds") or []),
                "channels_scanned": (result.get("stats") or {}).get("channels_scanned", 0),
                "fingerprint_matches": (result.get("stats") or {}).get("fingerprint_matches", 0),
                "youtube_bridge_seeds": result.get("youtube_bridge_seeds", 0),
                "warning": result.get("warning"),
                "error": result.get("error"),
                "top_channels": [
                    {
                        "title": (c.get("meta") or {}).get("title"),
                        "username": (c.get("meta") or {}).get("username"),
                        "url": (c.get("meta") or {}).get("url") or (c.get("seed") or {}).get("url"),
                        "risk_score": (c.get("risk") or {}).get("risk_score"),
                        "severity": (c.get("risk") or {}).get("severity"),
                        "signals": (c.get("risk") or {}).get("signals"),
                        "hits_count": c.get("hits_count", 0),
                    }
                    for c in (result.get("channels") or [])[:20]
                ],
            },
        }
        if result.get("error") and result.get("mode") != "seeds_only":
            summary["error"] = result["error"]

        db.complete_scan(scan_id, summary)
        logger.info(
            "=== Telegram scan %s done: mode=%s hits=%d saved=%d ===",
            scan_id, result.get("mode"), len(hits), saved,
        )
    except Exception as exc:
        logger.error("Telegram scan %s failed: %s\n%s", scan_id, exc, traceback.format_exc())
        db.complete_scan(scan_id, {"error": str(exc), "platform": "telegram"})


def _start_scan(scan_type: str) -> tuple[Response, int]:
    """Parse request, create scan record, kick off background thread, return immediately."""
    data: dict = request.get_json(force=True)

    entity_name = data.get("entity_name", "").strip()
    if not entity_name:
        return jsonify({"ok": False, "error": "Missing 'entity_name'"}), 400

    entity_type = data.get("entity_type", "generic")
    max_results = min(int(data.get("max_results", DEFAULT_MAX_RESULTS)), 500)
    min_conf = int(data.get("min_confidence", MIN_CONFIDENCE))
    date_from = _parse_date(data.get("date_from"))
    date_to = _parse_date(data.get("date_to"))
    known_channels: List[str] = data.get("known_channels_to_monitor", []) or []
    do_thumbnails: bool = data.get("analyze_thumbnails", THUMBNAIL_ANALYSIS_ENABLED)

    # Phase A inputs – official catalog + deep crawl
    official_channel_ids: List[str] = data.get("official_channel_ids", []) or []
    official_playlist_ids: List[str] = data.get("official_playlist_ids", []) or []
    # Also accept comma/newline-separated strings from the dashboard
    if isinstance(official_channel_ids, str):
        official_channel_ids = [x.strip() for x in official_channel_ids.replace("\n", ",").split(",") if x.strip()]
    if isinstance(official_playlist_ids, str):
        official_playlist_ids = [x.strip() for x in official_playlist_ids.replace("\n", ",").split(",") if x.strip()]
    if isinstance(known_channels, str):
        known_channels = [x.strip() for x in known_channels.replace("\n", ",").split(",") if x.strip()]

    enable_deep_crawl: bool = bool(data.get("enable_deep_crawl", True))
    enable_fingerprinting: bool = bool(data.get("enable_fingerprinting", FINGERPRINT_ENABLED))

    scan_id = db.create_scan(entity_name, entity_type, scan_type, {
        "max_results": max_results,
        "min_confidence": min_conf,
        "date_from": date_from,
        "date_to": date_to,
        "official_channel_ids": official_channel_ids,
        "official_playlist_ids": official_playlist_ids,
        "enable_deep_crawl": enable_deep_crawl,
        "enable_fingerprinting": enable_fingerprinting,
        "known_channels_to_monitor": known_channels,
    })

    thread = threading.Thread(
        target=_do_scan_work,
        args=(
            scan_id, entity_name, entity_type, scan_type,
            max_results, min_conf, date_from, date_to,
            known_channels, do_thumbnails,
            official_channel_ids, official_playlist_ids, enable_deep_crawl,
            enable_fingerprinting,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "ok": True,
        "scan_id": scan_id,
        "entity_name": entity_name,
        "status": "running",
        "message": "Scan started. Poll GET /scan/status/<scan_id> for progress.",
    }), 202


def _serialize_video(v: dict) -> dict:
    """Flatten a video dict for the JSON response."""
    sr = v.get("score_result", {})
    heur = v.get("heuristic") or {}
    return {
        "video_id": v.get("video_id"),
        "title": v.get("title"),
        "url": f"https://www.youtube.com/watch?v={v.get('video_id', '')}",
        "channel_title": v.get("channel_title"),
        "channel_id": v.get("channel_id"),
        "channel_url": f"https://www.youtube.com/channel/{v.get('channel_id', '')}",
        "published_at": v.get("published_at"),
        "views": v.get("view_count"),
        "likes": v.get("like_count"),
        "comments": v.get("comment_count"),
        "duration": v.get("duration_formatted"),
        "thumbnail_url": v.get("thumbnail_url"),
        "classification": sr.get("classification"),
        "confidence": sr.get("confidence"),
        "severity": sr.get("severity"),
        "reason": sr.get("reason"),
        "final_score": sr.get("final_score"),
        "thumbnail_analysis": sr.get("thumbnail_analysis", {}).get("description", ""),
        "sub_scores": sr.get("sub_scores"),
        "heuristic_score": heur.get("heuristic_score"),
        "heuristic_signals": heur.get("signals"),
        "catalog_match": heur.get("catalog_match"),
        "discovery_source": v.get("source"),
        "fingerprint_match": v.get("fingerprint_match") or sr.get("fingerprint_match"),
        "platform": v.get("platform") or "youtube",
        "message_id": v.get("message_id"),
        "review_status": v.get("review_status") or "pending",
        "review_note": v.get("review_note") or "",
        "scan_id": v.get("scan_id"),
    }


def _serialize_list(videos: List[dict]) -> List[dict]:
    return [_serialize_video(v) for v in videos]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.route("/scan", methods=["POST"])
def scan_both():
    """Unified scan – detects both piracy and defamatory content (async)."""
    resp, code = _start_scan("both")
    return resp, code


@app.route("/scan/defamatory", methods=["POST"])
def scan_defamatory():
    """Scan only for defamatory / meme / troll content (async)."""
    resp, code = _start_scan("defamatory")
    return resp, code


@app.route("/scan/piracy", methods=["POST"])
def scan_piracy():
    """Scan only for pirated / unauthorized re-uploads (async)."""
    resp, code = _start_scan("piracy")
    return resp, code


@app.route("/scan/telegram", methods=["POST"])
def scan_telegram():
    """Phase C – Telegram piracy scan (async).

    Body:
      entity_name (required)
      telegram_seeds: list/string of @channels / t.me links
      bridge_youtube_scan_id: optional prior YouTube scan to harvest t.me seeds
      official_channel_ids / official_playlist_ids: for fingerprint proof
      enable_fingerprinting: bool
      min_confidence: int
    """
    data: dict = request.get_json(force=True)
    entity_name = (data.get("entity_name") or "").strip()
    if not entity_name:
        return jsonify({"ok": False, "error": "Missing 'entity_name'"}), 400

    manual_seeds = data.get("telegram_seeds") or data.get("seeds") or []
    official_channel_ids = data.get("official_channel_ids", []) or []
    official_playlist_ids = data.get("official_playlist_ids", []) or []
    if isinstance(official_channel_ids, str):
        official_channel_ids = [x.strip() for x in official_channel_ids.replace("\n", ",").split(",") if x.strip()]
    if isinstance(official_playlist_ids, str):
        official_playlist_ids = [x.strip() for x in official_playlist_ids.replace("\n", ",").split(",") if x.strip()]

    enable_fingerprinting = bool(data.get("enable_fingerprinting", FINGERPRINT_ENABLED))
    bridge_youtube_scan_id = (data.get("bridge_youtube_scan_id") or "").strip() or None
    min_conf = int(data.get("min_confidence", MIN_CONFIDENCE))

    # Also pull stored seeds for this entity if requested
    if data.get("include_stored_seeds", True):
        stored = db.list_telegram_seeds(entity_name)
        if stored:
            if isinstance(manual_seeds, str):
                manual_seeds = [manual_seeds] + [s.get("url") or s.get("value") for s in stored]
            elif isinstance(manual_seeds, list):
                manual_seeds = list(manual_seeds) + [s.get("url") or s.get("value") for s in stored]

    scan_id = db.create_scan(entity_name, data.get("entity_type", "educator"), "telegram", {
        "min_confidence": min_conf,
        "enable_fingerprinting": enable_fingerprinting,
        "bridge_youtube_scan_id": bridge_youtube_scan_id,
        "official_channel_ids": official_channel_ids,
        "official_playlist_ids": official_playlist_ids,
    })

    thread = threading.Thread(
        target=_do_telegram_scan_work,
        args=(
            scan_id,
            entity_name,
            manual_seeds,
            official_channel_ids,
            official_playlist_ids,
            enable_fingerprinting,
            bridge_youtube_scan_id,
            min_conf,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "ok": True,
        "scan_id": scan_id,
        "entity_name": entity_name,
        "status": "running",
        "message": "Telegram scan started. Poll GET /scan/status/<scan_id>.",
        "telegram": telegram_status(),
    }), 202


@app.route("/telegram/seeds", methods=["GET"])
def telegram_seeds():
    """List harvested Telegram seeds (optionally filter by entity_name)."""
    entity_name = request.args.get("entity_name")
    limit = request.args.get("limit", 200, type=int)
    seeds = db.list_telegram_seeds(entity_name, limit=limit)
    return jsonify({"ok": True, "seeds": seeds, "telegram": telegram_status()})


@app.route("/scan/status/<scan_id>", methods=["GET"])
def scan_status(scan_id: str):
    """Poll scan progress. Returns status, progress message, and summary when done."""
    scan = db.get_scan(scan_id)
    if not scan:
        return jsonify({"ok": False, "error": "Scan not found"}), 404

    result: dict = {
        "ok": True,
        "scan_id": scan_id,
        "status": scan.get("status", "running"),
        "progress": scan.get("progress", "Starting..."),
    }

    if scan.get("status") == "completed":
        result["summary"] = scan.get("summary", {})

    return jsonify(result)


@app.route("/results/<scan_id>", methods=["GET"])
def get_results(scan_id: str):
    """Retrieve results of a previous scan."""
    scan = db.get_scan(scan_id)
    if not scan:
        return jsonify({"ok": False, "error": "Scan not found"}), 404
    videos = db.get_detected_videos(scan_id)
    return jsonify({"ok": True, "scan": scan, "detected_videos": videos})


@app.route("/history", methods=["GET"])
def history():
    """List recent scans."""
    limit = request.args.get("limit", 50, type=int)
    scans = db.list_scans(limit)
    return jsonify({"ok": True, "scans": scans})


@app.route("/channels", methods=["GET"])
def channels():
    """List channels that have been flagged for offending content."""
    limit = request.args.get("limit", 50, type=int)
    chs = db.list_channels(limit)
    return jsonify({"ok": True, "channels": chs})


@app.route("/report/takedown", methods=["POST"])
def takedown_report():
    """Generate DMCA + defamation takedown reports (YouTube + Telegram).

    Body: { "scan_id": "...", "entity_name": "...", "complainant_name": "...",
            "contact_email": "...", "entity_type": "...",
            "require_confirmed": bool (optional) }
    """
    data = request.get_json(force=True)
    scan_id = data.get("scan_id")
    if not scan_id:
        return jsonify({"ok": False, "error": "Missing 'scan_id'"}), 400

    videos = db.get_detected_videos(scan_id)
    if not videos:
        return jsonify({"ok": False, "error": "No detected videos for this scan"}), 404

    require_confirmed = bool(data.get("require_confirmed", TAKEDOWN_REQUIRE_CONFIRMED))
    report = generate_takedown_report(
        videos=videos,
        entity_name=data.get("entity_name", ""),
        entity_type=data.get("entity_type", "individual"),
        complainant_name=data.get("complainant_name", ""),
        contact_email=data.get("contact_email", ""),
        require_confirmed=require_confirmed,
    )

    report_id = db.save_takedown_report({
        "scan_id": scan_id,
        **report,
    })
    report["report_id"] = report_id

    return jsonify({"ok": True, **report})


# ---------------------------------------------------------------------------
# Phase D – monitors, alerts, review queue
# ---------------------------------------------------------------------------


@app.route("/monitors", methods=["GET"])
def monitors_list():
    return jsonify({"ok": True, "monitors": db.list_monitors()})


@app.route("/monitors", methods=["POST"])
def monitors_create():
    data = request.get_json(force=True)
    entity_name = (data.get("entity_name") or "").strip()
    if not entity_name:
        return jsonify({"ok": False, "error": "Missing entity_name"}), 400

    platforms = data.get("platforms") or ["youtube"]
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]

    monitor_id = create_entity_monitor(
        entity_name=entity_name,
        entity_type=data.get("entity_type", "educator"),
        platforms=platforms,
        interval_hours=float(data.get("interval_hours", MONITOR_DEFAULT_INTERVAL_HOURS)),
        scan_params=data.get("scan_params") or {
            "official_channel_ids": data.get("official_channel_ids") or [],
            "official_playlist_ids": data.get("official_playlist_ids") or [],
            "known_channels_to_monitor": data.get("known_channels_to_monitor") or [],
            "telegram_seeds": data.get("telegram_seeds") or [],
            "max_results": data.get("max_results", 100),
            "min_confidence": data.get("min_confidence", MIN_CONFIDENCE),
            "enable_deep_crawl": data.get("enable_deep_crawl", True),
            "enable_fingerprinting": data.get("enable_fingerprinting", True),
        },
        enabled=bool(data.get("enabled", True)),
    )
    return jsonify({"ok": True, "monitor_id": monitor_id, "monitor": db.get_monitor(monitor_id)}), 201


@app.route("/monitors/<monitor_id>", methods=["PATCH"])
def monitors_update(monitor_id: str):
    data = request.get_json(force=True) or {}
    allowed = {
        k: data[k]
        for k in (
            "enabled", "interval_hours", "platforms", "scan_params",
            "entity_type", "next_run_at",
        )
        if k in data
    }
    if not allowed:
        return jsonify({"ok": False, "error": "No updatable fields"}), 400
    mon = db.update_monitor(monitor_id, allowed)
    if not mon:
        return jsonify({"ok": False, "error": "Monitor not found"}), 404
    return jsonify({"ok": True, "monitor": mon})


@app.route("/monitors/<monitor_id>", methods=["DELETE"])
def monitors_delete(monitor_id: str):
    if not db.delete_monitor(monitor_id):
        return jsonify({"ok": False, "error": "Monitor not found"}), 404
    return jsonify({"ok": True})


@app.route("/monitors/<monitor_id>/run", methods=["POST"])
def monitors_run_now(monitor_id: str):
    """Force a monitor due immediately (scheduler picks it up on next tick)."""
    mon = db.get_monitor(monitor_id)
    if not mon:
        return jsonify({"ok": False, "error": "Monitor not found"}), 404
    db.update_monitor(monitor_id, {
        "next_run_at": datetime.utcnow().isoformat() + "Z",
        "enabled": True,
    })
    return jsonify({"ok": True, "message": "Monitor queued for next scheduler tick"})


@app.route("/alerts", methods=["GET"])
def alerts_list():
    unread_only = request.args.get("unread", "false").lower() in ("1", "true", "yes")
    limit = request.args.get("limit", 100, type=int)
    return jsonify({"ok": True, "alerts": db.list_alerts(unread_only=unread_only, limit=limit)})


@app.route("/alerts/read", methods=["POST"])
def alerts_mark_read():
    data = request.get_json(force=True) or {}
    if data.get("all"):
        n = db.mark_all_alerts_read()
        return jsonify({"ok": True, "marked": n})
    alert_id = data.get("alert_id")
    if not alert_id:
        return jsonify({"ok": False, "error": "Missing alert_id"}), 400
    alert = db.mark_alert_read(alert_id, read=bool(data.get("read", True)))
    if not alert:
        return jsonify({"ok": False, "error": "Alert not found"}), 404
    return jsonify({"ok": True, "alert": alert})


@app.route("/review", methods=["GET"])
def review_queue():
    status = request.args.get("status", "pending")
    entity_name = request.args.get("entity_name")
    limit = request.args.get("limit", 100, type=int)
    items = db.list_review_queue(status=status, entity_name=entity_name, limit=limit)
    return jsonify({"ok": True, "items": [_serialize_video(v) for v in items]})


@app.route("/review", methods=["POST"])
def review_decide():
    """Confirm or reject a detected hit.

    Body: { scan_id, video_id, decision: confirmed|false_positive, note? }
    """
    data = request.get_json(force=True) or {}
    scan_id = data.get("scan_id")
    video_id = data.get("video_id")
    decision = data.get("decision") or data.get("review_status")
    if not scan_id or not video_id or decision not in ("confirmed", "false_positive", "pending"):
        return jsonify({
            "ok": False,
            "error": "Need scan_id, video_id, decision in confirmed|false_positive|pending",
        }), 400
    updated = db.update_review_status(
        scan_id, video_id, decision, note=data.get("note") or ""
    )
    if not updated:
        return jsonify({"ok": False, "error": "Hit not found"}), 404
    return jsonify({"ok": True, "item": _serialize_video(updated)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "Unified Content Detection System",
        "database": db.db_status(),
        "youtube_api": yt_client.quota_status,
        "telegram": telegram_status(),
        "monitors": len(db.list_monitors()),
        "alerts_unread": len(db.list_alerts(unread_only=True, limit=500)),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "error": "Endpoint not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"ok": False, "error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 64)
    print("  UNIFIED CONTENT DETECTION SYSTEM")
    print("  Piracy + Defamatory Content Detection")
    print("=" * 64)
    print(f"  Database  : {db.db_status()}")
    print(f"  YT Keys   : {len(yt_client.api_keys)}")
    print("=" * 64)
    print("  POST /scan               – full scan (piracy + defamatory) [async]")
    print("  POST /scan/defamatory    – defamatory-only [async]")
    print("  POST /scan/piracy        – piracy-only [async]")
    print("  POST /scan/telegram      – Telegram piracy scan [async]")
    print("  GET  /scan/status/<id>   – poll scan progress")
    print("  GET  /results/<scan_id>  – past scan results")
    print("  GET  /telegram/seeds     – harvested Telegram seeds")
    print("  GET  /history            – list recent scans")
    print("  GET  /channels           – flagged channels")
    print("  POST /report/takedown    – generate takedown report")
    print("  GET/POST /monitors       – continuous monitors")
    print("  GET  /alerts             – alert feed")
    print("  GET/POST /review         – false-positive review queue")
    print("  GET  /health             – service health")
    print("=" * 64)
    print(f"  Telegram : {telegram_status()}")
    print("  Starting on http://0.0.0.0:5000 ...")
    print("=" * 64)

    start_scheduler(_do_scan_work, _do_telegram_scan_work)
    app.run(host="0.0.0.0", port=5000, debug=False)


# Also start scheduler when imported under a WSGI server
start_scheduler(_do_scan_work, _do_telegram_scan_work)
