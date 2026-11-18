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

from config import CATALOG_MAX_PER_SOURCE

logger = logging.getLogger(__name__)

_TITLE_NOISE = re.compile(
    r"\b(full|complete|lecture|class|batch|free|telegram|download|"
    r"part\s*\d+|ep(isode)?\s*\d+|hd|4k|official)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
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
    channel_ids = [c.strip() for c in (official_channel_ids or []) if c and c.strip()]
    playlist_ids = [p.strip() for p in (official_playlist_ids or []) if p and p.strip()]

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

    title_index = [
        {
            "video_id": e["video_id"],
            "title": e.get("title", ""),
            "norm_title": e.get("norm_title", ""),
            "tokens": title_tokens(e.get("title", "")),
            "channel_id": e.get("channel_id", ""),
        }
        for e in entries
    ]

    logger.info(
        "Official catalog ready: %d videos across %d channels",
        len(video_ids),
        len(owned_channels),
    )
    return {
        "video_ids": video_ids,
        "channel_ids": owned_channels,
        "entries": entries,
        "title_index": title_index,
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
    return {
        "official_video_id": best["video_id"],
        "official_title": best["title"],
        "similarity": round(best_score, 3),
    }


def catalog_derived_queries(
    title_index: List[dict],
    entity_name: str,
    max_queries: int = 20,
) -> List[str]:
    """Build search queries from official titles (subject-focused, brand-light)."""
    queries: List[str] = []
    seen: Set[str] = set()
    name_lower = (entity_name or "").lower()

    for entry in title_index:
        title = entry.get("title") or ""
        norm = entry.get("norm_title") or normalize_title(title)
        if not norm or len(norm) < 8:
            continue

        # Prefer the distinctive middle of the title (drop entity name tokens).
        parts = [p for p in norm.split() if p not in name_lower.split()]
        core = " ".join(parts[:6]).strip()
        if len(core) < 6:
            core = norm[:60]

        variants = [
            f"{core} full lecture",
            f"{core} telegram",
            f"{core} free",
            f"{entity_name} {core}".strip() if entity_name else core,
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
