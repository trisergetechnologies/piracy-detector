"""Multi-source search engine: yt-dlp, YouTube Data API, RSS, Phase A deep discovery."""

from __future__ import annotations

import contextvars
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import requests

from config import (
    CATALOG_QUERY_TITLES,
    DEEP_CRAWL_MAX_CHANNELS,
    DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL,
    PIRACY_HINGLISH_TEMPLATES,
    PIRACY_OBFUSCATED_TEMPLATES,
    PIRACY_QUERY_TEMPLATES,
    QUERY_MAX_NAME_VARIANTS,
    QUERY_TEMPLATES,
    QUERY_VARIANT_SUFFIXES,
    YOUTUBE_API_QUERY_TEMPLATES,
    YOUTUBE_SEARCH_BUDGET_PER_SCAN,
    YOUTUBE_SEARCH_COST,
    YOUTUBE_SEARCH_WINDOWS_DAYS,
    YTDLP_DATE_SEARCH_QUERIES,
    YTDLP_SEARCH_WORKERS,
)
from core import funnel
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


_PHONETIC_SWAPS = (("ph", "f"), ("ee", "i"), ("oo", "u"), ("w", "v"), ("sh", "s"), ("ck", "k"), ("y", "i"))


def name_variants(name: str, limit: int = QUERY_MAX_NAME_VARIANTS) -> List[str]:
    """Spellings pirates use to dodge brand searches: joined words, Hinglish
    phonetic swaps, a dropped vowel, collapsed double letters."""
    low = " ".join((name or "").lower().split())
    if len(low) < 4:
        return []
    cands: List[str] = []
    if " " in low:
        cands.append(low.replace(" ", ""))
    for a, b in _PHONETIC_SWAPS:
        if a in low:
            cands.append(low.replace(a, b))
    collapsed = "".join(ch for i, ch in enumerate(low) if i == 0 or ch != low[i - 1] or ch == " ")
    cands.append(collapsed)
    for i in range(1, len(low) - 1):
        if low[i] in "aeiou" and low[i - 1] != " " and low[i + 1] != " ":
            cands.append(low[:i] + low[i + 1:])
            break
    out: List[str] = []
    for c in cands:
        if c != low and c not in out:
            out.append(c)
    return out[:limit]


def expand_piracy_queries(entity_name: str) -> List[str]:
    """Hindi/Hinglish phrasings plus misspelled-name variants (lists live in config)."""
    name = entity_name.strip()
    out = [tpl.format(name=name) for tpl in PIRACY_HINGLISH_TEMPLATES]
    for variant in name_variants(name):
        out.extend(f"{variant} {suffix}".strip() for suffix in QUERY_VARIANT_SUFFIXES)
    return out


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
    by_date: bool = False,
) -> List[dict]:
    """Search YouTube via yt-dlp scraping. Returns lightweight video dicts.

    by_date=True uses upload-date ordering, which surfaces fresh low-view
    uploads that relevance ranking buries.
    """
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
            prefix = "ytsearchdate" if by_date else "ytsearch"
            data = ydl.extract_info(f"{prefix}{max_results}:{query}", download=False)
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
                        source="ytdlp_date" if by_date else "ytdlp",
                    )
                )
                # Duration is free here and lets us rank before enrichment.
                try:
                    results[-1]["duration_seconds"] = int(entry.get("duration") or 0)
                except (TypeError, ValueError):
                    pass
    except Exception as exc:
        logger.error("yt-dlp search failed for query '%s': %s", query, exc)
        funnel.current().error("ytdlp_search", f"{query}: {exc}")

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
    errors: Optional[List[str]] = None,
) -> List[dict]:
    """Search via the official YouTube Data API. Returns lightweight dicts.

    If *errors* is given, a failure appends its message there so the caller
    can stop spending quota.
    """
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
        kind = "youtube_api_quota" if ("quota" in str(exc).lower() or "429" in str(exc)) else "youtube_api_search"
        funnel.current().error(kind, f"{query}: {exc}")
        if errors is not None:
            errors.append(f"{kind}: {exc}")

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
                funnel.current().unreachable(f"rss:{cid}", f"HTTP {resp.status_code}")
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
            if not uploads:
                # Zero-quota fallback: the channel's public RSS feed (latest ~15).
                uploads = monitor_rss_channels([cid])
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
            if not uploads:
                funnel.current().unreachable(f"channel:{cid}", "no uploads returned")
        except Exception as exc:
            logger.error("Deep-crawl failed for %s: %s", cid, exc)
            funnel.current().error("deep_crawl", f"{cid}: {exc}")
            fallback = [v for v in monitor_rss_channels([cid]) if v["video_id"] not in exclude_video_ids]
            for v in fallback:
                v["source"] = "deep_crawl_rss"
            results.extend(fallback)
            if not fallback:
                funnel.current().unreachable(f"channel:{cid}", f"deep-crawl error: {exc}")
    return results


# ---------------------------------------------------------------------------
# Orchestrator – combine all sources, deduplicate
# ---------------------------------------------------------------------------


def _ytdlp_batch(
    queries: List[str],
    max_results: int,
    date_from: Optional[str],
    date_to: Optional[str],
    by_date: bool = False,
) -> List[tuple]:
    """Run yt-dlp searches on a small thread pool. Returns [(query, hits)] in order."""
    if not queries:
        return []

    def _one(q: str) -> List[dict]:
        return search_ytdlp(q, max_results=max_results, date_from=date_from,
                            date_to=date_to, by_date=by_date)

    out: List[tuple] = []
    with ThreadPoolExecutor(max_workers=max(YTDLP_SEARCH_WORKERS, 1)) as pool:
        # copy_context() so worker threads report into this scan's funnel
        futures = [pool.submit(contextvars.copy_context().run, _one, q) for q in queries]
        for q, fut in zip(queries, futures):
            try:
                out.append((q, fut.result()))
            except Exception as exc:
                funnel.current().error("ytdlp_search", f"{q}: {exc}")
                out.append((q, []))
    return out


def _api_date_windows(date_from: Optional[str], date_to: Optional[str]) -> List[tuple]:
    """Disjoint (publishedAfter, publishedBefore) windows, newest first.

    search.list returns at most 50 results per call; separate windows keep old
    and new low-view uploads from being crowded out by one busy period.
    """
    if date_from or date_to:
        return [(date_from, date_to)]
    now = datetime.utcnow()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    days = sorted(d for d in YOUTUBE_SEARCH_WINDOWS_DAYS if d > 0)
    windows: List[tuple] = []
    upper: Optional[str] = None
    for d in days:
        lower = (now - timedelta(days=d)).strftime(fmt)
        windows.append((lower, upper))
        upper = lower
    if 0 in YOUTUBE_SEARCH_WINDOWS_DAYS or not windows:
        windows.append((None, upper))
    return windows


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
    channel_map_seed: Optional[Dict[str, dict]] = None,  # unused; kept for call compatibility
    extra_queries: Optional[List[str]] = None,
) -> List[dict]:
    """
    Discovery pipeline (free sources first, paid API inside a budget):
      1. Queries: brand + obfuscated + Hinglish/misspelling variants +
         official-title queries + titles of earlier confirmed hits
      2. yt-dlp relevance search for every query (free)
      3. yt-dlp upload-date search for the top queries (free; low-view/new uploads)
      4. search.list, order=date, disjoint date windows, capped by budget
      5. RSS for known pirate channels (free)
      6. Deep-crawl channels chosen by evidence (no subscriber filter)
    Nothing is filtered or ranked by views, subscribers or likes.
    """
    fn = funnel.current()
    catalog = catalog or {}
    piracy = scan_type in ("both", "piracy")
    official_video_ids: Set[str] = set(catalog.get("video_ids") or set())
    official_channel_ids: Set[str] = set(catalog.get("channel_ids") or set())

    queries = generate_queries(entity_name, entity_type, scan_type)
    title_queries: List[str] = []
    if piracy:
        queries += [q for q in expand_piracy_queries(entity_name) if q not in queries]
        if catalog.get("title_index"):
            title_queries = catalog_derived_queries(
                catalog["title_index"], entity_name, max_queries=CATALOG_QUERY_TITLES * 3
            )
        title_queries += [q for q in (extra_queries or []) if q and q not in title_queries]
        queries += [q for q in title_queries if q not in queries]

    logger.info("Discovery: %d queries for '%s' (%s)", len(queries), entity_name, entity_type)

    seen_ids: Set[str] = set(official_video_ids)  # skip official from the start
    all_videos: List[dict] = []
    official_seen: Set[str] = set()

    def _add(videos: List[dict]) -> int:
        added = 0
        for v in videos:
            vid = v.get("video_id")
            if not vid:
                continue
            if vid in official_video_ids or (
                v.get("channel_id") and v["channel_id"] in official_channel_ids
            ):
                # The rights-holder's own upload: never a suspect.
                if vid not in official_seen:
                    official_seen.add(vid)
                    fn.drop(vid, "filter", "official_content")
                continue
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            all_videos.append(v)
            added += 1
        return added

    # 1+2. yt-dlp relevance search (free)
    ytdlp_ok = _ytdlp_available()
    if ytdlp_ok:
        for q, hits in _ytdlp_batch(queries, max_results_per_query, date_from, date_to):
            fn.query("ytdlp", q, len(hits), _add(hits))
        # 3. Upload-date ordering: official titles first, then brand piracy queries
        date_queries = (title_queries + queries)[: max(YTDLP_DATE_SEARCH_QUERIES, 0)] if piracy else []
        for q, hits in _ytdlp_batch(date_queries, max_results_per_query, date_from, date_to, by_date=True):
            fn.query("ytdlp_date", q, len(hits), _add(hits))
    else:
        fn.error("ytdlp_missing", "yt-dlp not installed; free search skipped")
    fn.stage("after_free_search", len(all_videos))

    # 4. Paid search.list inside a per-scan budget
    if yt_client:
        calls_left = max(YOUTUBE_SEARCH_BUDGET_PER_SCAN // YOUTUBE_SEARCH_COST, 0)
        api_queries = (
            [tpl.format(name=entity_name.strip()) for tpl in YOUTUBE_API_QUERY_TEMPLATES]
            if piracy else queries[:5]
        )
        if not ytdlp_ok:
            api_queries = queries  # API is the only search source left
        planned = [(q, w) for w in _api_date_windows(date_from, date_to) for q in api_queries]
        stop_reason = ""
        done = 0
        for q, (after, before) in planned:
            if calls_left <= 0:
                stop_reason = "budget"
                break
            errs: List[str] = []
            hits = search_youtube_api(
                yt_client, q, max_results=50, date_from=after, date_to=before,
                order="date", errors=errs,
            )
            calls_left -= 1
            done += 1
            fn.query("youtube_api", f"{q} [{after or 'start'}..{before or 'now'}]",
                     len(hits), _add(hits), error=errs[0] if errs else None)
            if errs:
                stop_reason = "quota" if "quota" in errs[0].lower() or "429" in errs[0] else "api_error"
                break
        if stop_reason:
            fn.note("api_search_stopped", {
                "reason": stop_reason, "calls_done": done,
                "calls_skipped": len(planned) - done,
            })
        logger.info("API search: %d calls, %d unique so far%s", done, len(all_videos),
                    f" (stopped: {stop_reason})" if stop_reason else "")

    # 5. Known pirate channels (incl. channels of earlier confirmed hits)
    known: List[str] = []
    for ref in known_channel_ids or []:
        cid = None
        try:
            cid = yt_client.resolve_channel_id(ref) if yt_client else None
        except Exception:
            cid = None
        cid = cid or (ref if str(ref).startswith("UC") else None)
        if not cid:
            fn.unreachable(ref, "known channel could not be resolved")
        elif cid not in official_channel_ids and cid not in known:
            known.append(cid)
    if known:
        rss_hits = monitor_rss_channels(known)
        fn.query("rss", f"{len(known)} known channels", len(rss_hits), _add(rss_hits))

    fn.stage("after_search", len(all_videos))

    # 6. Deep-crawl: full recent uploads of the channels with the most evidence
    if enable_deep_crawl and piracy and yt_client and (all_videos or known):
        crawl_targets = list(known)
        for cid in select_channels_for_deep_crawl(
            all_videos,
            official_channel_ids=official_channel_ids,
            max_channels=DEEP_CRAWL_MAX_CHANNELS,
            catalog=catalog,
            entity_name=entity_name,
        ):
            if cid not in crawl_targets:
                crawl_targets.append(cid)

        if crawl_targets:
            crawled = deep_crawl_channels(
                yt_client,
                crawl_targets,
                max_videos_per_channel=DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL,
                exclude_video_ids=seen_ids,
            )
            added = _add(crawled)
            fn.query("deep_crawl", f"{len(crawl_targets)} channels", len(crawled), added)
            logger.info(
                "Deep-crawl complete: +%d videos from %d channels (total %d)",
                added, len(crawl_targets), len(all_videos),
            )

    fn.stage("queries", len(queries))
    fn.stage("raw_results", len(all_videos))
    logger.info("Discovery complete: %d unique suspect videos", len(all_videos))
    return all_videos


