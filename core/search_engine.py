"""Multi-source search engine: yt-dlp, YouTube Data API, RSS, Phase A deep discovery."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set

import requests

from config import (
    DEEP_CRAWL_MAX_CHANNELS,
    DEEP_CRAWL_MAX_SUBSCRIBERS,
    DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL,
    PIRACY_OBFUSCATED_TEMPLATES,
    PIRACY_QUERY_TEMPLATES,
    PIRACY_SEARCH_ORDERS,
    QUERY_TEMPLATES,
)
from core.catalog import catalog_derived_queries
from core.heuristics import select_channels_for_deep_crawl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight video dict returned by all search backends
# ---------------------------------------------------------------------------
# Keys: video_id, title, channel_id, channel_title, published_at,
#        thumbnail_url, description, source


def _video_dict(
    video_id: str,
    title: str = "",
    channel_id: str = "",
    channel_title: str = "",
    published_at: str = "",
    thumbnail_url: str = "",
    description: str = "",
    source: str = "unknown",
) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "published_at": published_at,
        "thumbnail_url": thumbnail_url,
        "description": description,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------


def generate_queries(
    entity_name: str,
    entity_type: str = "generic",
    scan_type: str = "both",
) -> List[str]:
    """Return search queries tailored to the entity and scan type.

    Piracy mode includes brand + obfuscated (brand-light) templates.
    """
    defamatory_templates = QUERY_TEMPLATES.get(entity_type, QUERY_TEMPLATES["generic"])

    if scan_type == "defamatory":
        templates = list(defamatory_templates)
    elif scan_type == "piracy":
        templates = list(PIRACY_QUERY_TEMPLATES) + list(PIRACY_OBFUSCATED_TEMPLATES)
    else:
        templates = (
            list(defamatory_templates)
            + list(PIRACY_QUERY_TEMPLATES)
            + list(PIRACY_OBFUSCATED_TEMPLATES)
        )

    parts = entity_name.strip().split()
    first = parts[0] if parts else entity_name
    last = parts[-1] if len(parts) > 1 else ""

    seen: Set[str] = set()
    queries: List[str] = []
    for tpl in templates:
        q = tpl.format(name=entity_name.strip(), first=first, last=last)
        if q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


# ---------------------------------------------------------------------------
# yt-dlp search (zero API-quota cost)
# ---------------------------------------------------------------------------

def _ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def search_ytdlp(
    query: str,
    max_results: int = 30,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[dict]:
    """Search YouTube via yt-dlp scraping. Returns lightweight video dicts."""
    if not _ytdlp_available():
        logger.warning("yt-dlp not installed – skipping scrape search")
        return []

    import yt_dlp

    ydl_opts: Dict[str, Any] = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }

    if date_from or date_to:
        dr_kwargs: Dict[str, Any] = {}
        if date_from:
            dr_kwargs["start"] = date_from.replace("-", "")[:8]
        if date_to:
            dr_kwargs["end"] = date_to.replace("-", "")[:8]
        ydl_opts["daterange"] = yt_dlp.utils.DateRange(**dr_kwargs)

    results: List[dict] = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            if not data:
                return results

            for entry in data.get("entries") or []:
                if not entry:
                    continue
                vid = entry.get("id") or entry.get("url", "").split("=")[-1]
                if not vid or len(vid) < 5:
                    continue
                results.append(
                    _video_dict(
                        video_id=vid,
                        title=entry.get("title", ""),
                        channel_id=entry.get("channel_id", ""),
                        channel_title=entry.get("uploader", "") or entry.get("channel", ""),
                        published_at=entry.get("upload_date", ""),
                        thumbnail_url=entry.get("thumbnail", "")
                            or (entry.get("thumbnails") or [{}])[0].get("url", ""),
                        description=entry.get("description", "") or "",
                        source="ytdlp",
                    )
                )
    except Exception as exc:
        logger.error("yt-dlp search failed for query '%s': %s", query, exc)

    return results


# ---------------------------------------------------------------------------
# YouTube Data API search (fallback – costs 100 quota units per call)
# ---------------------------------------------------------------------------


def search_youtube_api(
    yt_client,
    query: str,
    max_results: int = 50,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    order: str = "relevance",
    video_duration: Optional[str] = None,
) -> List[dict]:
    """Search via the official YouTube Data API. Returns lightweight dicts."""
    params: Dict[str, Any] = {
        "q": query,
        "part": "id,snippet",
        "type": "video",
        "maxResults": min(max_results, 50),
        "safeSearch": "none",
        "order": order,
    }
    if date_from:
        ts = date_from if date_from.endswith("Z") else date_from + "Z"
        params["publishedAfter"] = ts
    if date_to:
        ts = date_to if date_to.endswith("Z") else date_to + "Z"
        params["publishedBefore"] = ts
    if video_duration:
        params["videoDuration"] = video_duration

    results: List[dict] = []
    try:
        resp = yt_client.search(**params)
        for item in resp.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            snip = item.get("snippet", {})
            results.append(
                _video_dict(
                    video_id=vid,
                    title=snip.get("title", ""),
                    channel_id=snip.get("channelId", ""),
                    channel_title=snip.get("channelTitle", ""),
                    published_at=snip.get("publishedAt", ""),
                    thumbnail_url=snip.get("thumbnails", {}).get("high", {}).get("url", ""),
                    description=snip.get("description", ""),
                    source="youtube_api",
                )
            )
    except Exception as exc:
        logger.error("YouTube API search failed for '%s': %s", query, exc)

    return results


# ---------------------------------------------------------------------------
# RSS channel monitor (zero cost)
# ---------------------------------------------------------------------------

_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def monitor_rss_channels(channel_ids: List[str]) -> List[dict]:
    """Fetch the latest videos from given channels via RSS (last ~15 per channel)."""
    results: List[dict] = []
    for cid in channel_ids:
        try:
            resp = requests.get(_RSS_URL.format(channel_id=cid), timeout=10)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            for entry in root.findall("atom:entry", ns):
                vid_el = entry.find("yt:videoId", ns)
                title_el = entry.find("atom:title", ns)
                published_el = entry.find("atom:published", ns)
                author_el = entry.find("atom:author/atom:name", ns)
                if vid_el is None:
                    continue
                results.append(
                    _video_dict(
                        video_id=vid_el.text or "",
                        title=title_el.text if title_el is not None else "",
                        channel_id=cid,
                        channel_title=author_el.text if author_el is not None else "",
                        published_at=published_el.text if published_el is not None else "",
                        source="rss",
                    )
                )
        except Exception as exc:
            logger.error("RSS fetch failed for channel %s: %s", cid, exc)
    return results


# ---------------------------------------------------------------------------
# Phase A helpers
# ---------------------------------------------------------------------------


def deep_crawl_channels(
    yt_client,
    channel_ids: List[str],
    max_videos_per_channel: int = DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL,
    exclude_video_ids: Optional[Set[str]] = None,
) -> List[dict]:
    """Pull full recent upload lists from suspicious channels."""
    exclude_video_ids = exclude_video_ids or set()
    results: List[dict] = []
    for cid in channel_ids:
        try:
            uploads = yt_client.get_channel_uploads(cid, max_results=max_videos_per_channel)
            for row in uploads:
                vid = row.get("video_id")
                if not vid or vid in exclude_video_ids:
                    continue
                results.append(
                    _video_dict(
                        video_id=vid,
                        title=row.get("title", ""),
                        channel_id=row.get("channel_id") or cid,
                        channel_title=row.get("channel_title", ""),
                        published_at=row.get("published_at", ""),
                        thumbnail_url=row.get("thumbnail_url", ""),
                        description=row.get("description", ""),
                        source="deep_crawl",
                    )
                )
            logger.info("Deep-crawl %s → %d uploads", cid, len(uploads))
        except Exception as exc:
            logger.error("Deep-crawl failed for %s: %s", cid, exc)
    return results


# ---------------------------------------------------------------------------
# Orchestrator – combine all sources, deduplicate
# ---------------------------------------------------------------------------


def discover_videos(
    entity_name: str,
    entity_type: str = "generic",
    scan_type: str = "both",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    max_results_per_query: int = 30,
    known_channel_ids: Optional[List[str]] = None,
    yt_client=None,
    catalog: Optional[dict] = None,
    enable_deep_crawl: bool = True,
    channel_map_seed: Optional[Dict[str, dict]] = None,
) -> List[dict]:
    """
    Run the full discovery pipeline:
    1. Generate brand + obfuscated queries
    2. yt-dlp search (primary, free)
    3. Anti-viral YouTube API search (date/relevance + long duration; NO viewCount)
    4. Catalog-derived subject queries
    5. RSS for known pirate channels
    6. Deep-crawl suspicious small channels from seed hits
    7. Deduplicate; exclude official catalog video IDs
    """
    queries = generate_queries(entity_name, entity_type, scan_type)
    catalog = catalog or {}
    official_video_ids: Set[str] = set(catalog.get("video_ids") or set())
    official_channel_ids: Set[str] = set(catalog.get("channel_ids") or set())

    # Extra queries from official lecture titles
    if scan_type in ("both", "piracy") and catalog.get("title_index"):
        extra = catalog_derived_queries(catalog["title_index"], entity_name, max_queries=20)
        for q in extra:
            if q not in queries:
                queries.append(q)

    logger.info("Discovery: %d queries for '%s' (%s)", len(queries), entity_name, entity_type)

    seen_ids: Set[str] = set(official_video_ids)  # skip official from the start
    all_videos: List[dict] = []

    def _add(videos: List[dict]):
        for v in videos:
            vid = v["video_id"]
            if not vid or vid in seen_ids:
                continue
            # Never treat the rights-holder's own channel as a suspect seed
            if v.get("channel_id") and v["channel_id"] in official_channel_ids:
                continue
            seen_ids.add(vid)
            all_videos.append(v)

    # 1. yt-dlp (primary – free)
    ytdlp_ok = _ytdlp_available()
    for q in queries:
        if ytdlp_ok:
            hits = search_ytdlp(q, max_results=max_results_per_query, date_from=date_from, date_to=date_to)
            _add(hits)
        elif yt_client:
            hits = search_youtube_api(
                yt_client, q, max_results=max_results_per_query,
                date_from=date_from, date_to=date_to, order="date",
            )
            _add(hits)

    # 2. Sparse fallback: top queries by date + relevance (not viewCount)
    if len(all_videos) < 20 and yt_client:
        for q in queries[:5]:
            for order in ("date", "relevance"):
                hits = search_youtube_api(
                    yt_client, q, max_results=50,
                    date_from=date_from, date_to=date_to, order=order,
                )
                _add(hits)

    # 3. Anti-viral piracy search: long videos, date/relevance only
    #    viewCount ordering intentionally removed — it buries low-view mirrors.
    if scan_type in ("both", "piracy") and yt_client:
        raw_piracy_focus = [
            tpl.format(
                name=entity_name.strip(),
                first=entity_name.strip().split()[0],
                last=entity_name.strip().split()[-1]
                if len(entity_name.strip().split()) > 1
                else "",
            )
            for tpl in list(PIRACY_QUERY_TEMPLATES)[:12] + list(PIRACY_OBFUSCATED_TEMPLATES)[:8]
        ]
        seen_q: Set[str] = set()
        piracy_focus: List[str] = []
        for q in raw_piracy_focus:
            if q not in seen_q:
                seen_q.add(q)
                piracy_focus.append(q)

        for q in piracy_focus:
            for order in PIRACY_SEARCH_ORDERS:
                hits = search_youtube_api(
                    yt_client, q, max_results=50,
                    date_from=date_from, date_to=date_to,
                    order=order, video_duration="long",
                )
                _add(hits)
        logger.info("Anti-viral long-video search → %d unique so far", len(all_videos))

    # 4. RSS monitoring for known bad channels
    if known_channel_ids:
        rss_hits = monitor_rss_channels(known_channel_ids)
        _add(rss_hits)

    # 5. Phase A deep-crawl: expand seed hits into full channel catalogs
    if (
        enable_deep_crawl
        and scan_type in ("both", "piracy")
        and yt_client
        and all_videos
    ):
        # Need channel stats to prefer small dump channels
        seed_cids = list({v["channel_id"] for v in all_videos if v.get("channel_id")})
        channel_map = dict(channel_map_seed or {})
        missing = [c for c in seed_cids if c not in channel_map]
        if missing:
            try:
                channel_map.update(yt_client.get_channels_batch(missing))
            except Exception as exc:
                logger.error("Channel batch for deep-crawl failed: %s", exc)

        crawl_targets = select_channels_for_deep_crawl(
            all_videos,
            channel_map,
            official_channel_ids=official_channel_ids,
            max_channels=DEEP_CRAWL_MAX_CHANNELS,
            max_subscribers=DEEP_CRAWL_MAX_SUBSCRIBERS,
        )
        # Also always crawl explicitly monitored pirate channels
        for cid in known_channel_ids or []:
            if cid and cid not in crawl_targets and cid not in official_channel_ids:
                crawl_targets.append(cid)

        if crawl_targets:
            crawled = deep_crawl_channels(
                yt_client,
                crawl_targets,
                max_videos_per_channel=DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL,
                exclude_video_ids=seen_ids,
            )
            _add(crawled)
            logger.info(
                "Deep-crawl complete: +%d videos from %d channels (total %d)",
                len(crawled),
                len(crawl_targets),
                len(all_videos),
            )

    logger.info("Discovery complete: %d unique suspect videos", len(all_videos))
    return all_videos
