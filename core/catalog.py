"""Official content catalog ingest for Phase A discovery.

Builds a reference set of the rights-holder's own videos so we can:
  1. Exclude them from piracy hits
  2. Generate title/duration-based candidate queries
  3. Score suspects via cheap title similarity (pre-fingerprint)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from config import CATALOG_COMMON_TOKEN_RATIO, CATALOG_MAX_PER_SOURCE

logger = logging.getLogger(__name__)

_TITLE_NOISE = re.compile(
    r"\b(full|complete|lecture|class|batch|free|telegram|download|"
    r"part\s*\d+|ep(isode)?\s*\d+|hd|4k|official)\b",
    re.IGNORECASE,
)
# Keep letters/digits of any script (Devanagari incl. vowel signs), drop punctuation.
_NON_ALNUM = re.compile(r"(?:[^\w\s\u0900-\u097F]|_)+")
_WS = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip common piracy/marketing noise, keep content tokens."""
    if not title:
        return ""
    t = title.lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = _NON_ALNUM.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t


def title_tokens(title: str) -> Set[str]:
    return {tok for tok in normalize_title(title).split() if len(tok) > 2}


def title_jaccard(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def ingest_official_catalog(
    yt_client,
    official_channel_ids: Optional[List[str]] = None,
    official_playlist_ids: Optional[List[str]] = None,
    max_per_source: int = CATALOG_MAX_PER_SOURCE,
) -> Dict[str, Any]:
    """Ingest official videos from channels and playlists.

    Returns dict with:
      video_ids: set[str]
      channel_ids: set[str]
      entries: list[dict]  – catalog rows (video_id, title, channel_id, ...)
      title_index: list[dict] – {video_id, title, norm_title, tokens}
    """
    from core import funnel
    from core.youtube_client import parse_duration, parse_playlist_ref

    fn = funnel.current()
    unresolved: List[str] = []

    # Accept UC… ids, @handles and channel URLs (handles used to yield an empty catalog).
    channel_ids: List[str] = []
    for ref in [c.strip() for c in (official_channel_ids or []) if c and c.strip()]:
        cid = None
        try:
            cid = yt_client.resolve_channel_id(ref)
        except Exception as exc:
            logger.error("Channel resolve failed for %s: %s", ref, exc)
        if cid:
            if cid not in channel_ids:
                channel_ids.append(cid)
        else:
            unresolved.append(ref)
            fn.unreachable(ref, "official channel could not be resolved to a YouTube channel id")
            logger.warning("Official channel '%s' could not be resolved – ignored", ref)

    playlist_ids: List[str] = []
    for ref in [p.strip() for p in (official_playlist_ids or []) if p and p.strip()]:
        pid = parse_playlist_ref(ref)
        if pid:
            playlist_ids.append(pid)
        else:
            unresolved.append(ref)
            fn.unreachable(ref, "official playlist reference not understood")

    video_ids: Set[str] = set()
    owned_channels: Set[str] = set(channel_ids)
    entries: List[dict] = []

    def _add(rows: List[dict], source_label: str) -> None:
        for row in rows:
            vid = row.get("video_id")
            if not vid or vid in video_ids:
                continue
            video_ids.add(vid)
            if row.get("channel_id"):
                owned_channels.add(row["channel_id"])
            entry = {
                **row,
                "is_official": True,
                "catalog_source": source_label,
                "norm_title": normalize_title(row.get("title", "")),
            }
            entries.append(entry)

    for cid in channel_ids:
        try:
            uploads = yt_client.get_channel_uploads(cid, max_results=max_per_source)
            _add(uploads, f"channel:{cid}")
            logger.info("Catalog: channel %s → %d uploads", cid, len(uploads))
        except Exception as exc:
            logger.error("Catalog channel ingest failed for %s: %s", cid, exc)

    for pid in playlist_ids:
        try:
            items = yt_client.get_playlist_items(pid, max_results=max_per_source)
            _add(items, f"playlist:{pid}")
            logger.info("Catalog: playlist %s → %d items", pid, len(items))
        except Exception as exc:
            logger.error("Catalog playlist ingest failed for %s: %s", pid, exc)

    # Durations (1 quota unit per 50 videos) – used for title+duration matching.
    duration_map: Dict[str, int] = {}
    try:
        for item in yt_client.get_video_details([e["video_id"] for e in entries]):
            secs = parse_duration(item.get("contentDetails", {}).get("duration", ""))
            if secs:
                duration_map[item["id"]] = secs
    except Exception as exc:
        logger.warning("Catalog duration lookup failed: %s", exc)
        fn.error("catalog_durations", str(exc))
    for e in entries:
        e["duration_seconds"] = duration_map.get(e["video_id"], 0)

    title_index = [
        {
            "video_id": e["video_id"],
            "title": e.get("title", ""),
            "norm_title": e.get("norm_title", ""),
            "tokens": title_tokens(e.get("title", "")),
            "channel_id": e.get("channel_id", ""),
            "duration_seconds": e.get("duration_seconds", 0),
        }
        for e in entries
    ]

    logger.info(
        "Official catalog ready: %d videos across %d channels (%d unresolved refs)",
        len(video_ids),
        len(owned_channels),
        len(unresolved),
    )
    fn.note("catalog", {"videos": len(video_ids), "channels": len(owned_channels),
                        "with_duration": len(duration_map), "unresolved": unresolved})
    return {
        "video_ids": video_ids,
        "channel_ids": owned_channels,
        "entries": entries,
        "title_index": title_index,
        "duration_map": duration_map,
        "unresolved": unresolved,
    }


def best_catalog_match(
    candidate_title: str,
    title_index: List[dict],
    threshold: float = 0.45,
) -> Optional[dict]:
    """Return best official title match above *threshold*, or None."""
    if not candidate_title or not title_index:
        return None

    best = None
    best_score = 0.0
    cand_tokens = title_tokens(candidate_title)
    if not cand_tokens:
        return None

    for entry in title_index:
        ot = entry.get("tokens") or set()
        if not ot:
            continue
        score = len(cand_tokens & ot) / len(cand_tokens | ot)
        if score > best_score:
            best_score = score
            best = entry

    if best is None or best_score < threshold:
        return None
    # Words the rights holder puts in most titles ("UP TGT PGT 2026") are also
    # in every competitor's titles; only words rare inside the catalog say
    # which lecture this is.
    df = _token_df(title_index)
    rare_cut = max(2, int(len(title_index) * CATALOG_COMMON_TOKEN_RATIO))
    distinctive = [t for t in cand_tokens & best["tokens"] if df.get(t, 0) <= rare_cut]
    return {
        "official_video_id": best["video_id"],
        "official_title": best["title"],
        "similarity": round(best_score, 3),
        "distinctive_shared": len(distinctive),
    }


_DF_CACHE: Dict[int, Any] = {}


def _token_df(title_index: List[dict]) -> Dict[str, int]:
    """Number of official titles each token appears in (cached per catalog)."""
    key = id(title_index)
    hit = _DF_CACHE.get(key)
    if hit and hit[0] == len(title_index):
        return hit[1]
    df: Dict[str, int] = {}
    for entry in title_index:
        for tok in entry.get("tokens") or ():
            df[tok] = df.get(tok, 0) + 1
    _DF_CACHE.clear()
    _DF_CACHE[key] = (len(title_index), df)
    return df


def catalog_derived_queries(
    title_index: List[dict],
    entity_name: str,
    max_queries: int = 20,
) -> List[str]:
    """Build search queries from official titles (subject-focused, brand-light).

    Titles are sampled evenly across the whole catalog (previously only the
    first five titles were ever used), and the exact title is searched first
    because lazy re-uploaders keep it.
    """
    from config import CATALOG_QUERY_TITLES

    queries: List[str] = []
    seen: Set[str] = set()
    name_lower = (entity_name or "").lower()

    usable = [
        e for e in title_index
        if len(e.get("norm_title") or normalize_title(e.get("title") or "")) >= 8
    ]
    step = max(len(usable) // max(CATALOG_QUERY_TITLES, 1), 1)

    for entry in usable[::step]:
        title = entry.get("title") or ""
        norm = entry.get("norm_title") or normalize_title(title)

        # Prefer the distinctive middle of the title (drop entity name tokens).
        parts = [p for p in norm.split() if p not in name_lower.split()]
        core = " ".join(parts[:6]).strip()
        if len(core) < 6:
            core = norm[:60]

        variants = [
            title.strip()[:90],
            f"{core} full lecture",
            f"{core} free",
        ]
        for q in variants:
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(q)
            if len(queries) >= max_queries:
                return queries

    return queries
