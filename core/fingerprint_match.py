"""Phase B orchestrator: fingerprint official catalog + match suspects."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from config import (
    FINGERPRINT_MATCH_THRESHOLD,
    FINGERPRINT_MAX_CANDIDATES,
    FINGERPRINT_MAX_CATALOG,
    FINGERPRINT_SAMPLE_SECONDS,
    FINGERPRINT_STRONG_THRESHOLD,
    PIRACY_MIN_DURATION_SECONDS,
)
from core.fingerprint import (
    compare_fingerprints,
    get_or_compute_fingerprint,
)
from core.media_audio import download_audio_sample

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str], None]]


def _progress(cb: ProgressCb, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    logger.info(msg)


def fingerprint_video(
    video_id: str,
    sample_seconds: int = FINGERPRINT_SAMPLE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Download audio sample (cached) and return fingerprint payload."""
    existing = get_or_compute_fingerprint(video_id, wav_path=None)
    if existing:
        return existing

    wav = download_audio_sample(video_id, sample_seconds=sample_seconds)
    if not wav:
        return None
    return get_or_compute_fingerprint(video_id, wav_path=wav)


def build_catalog_fingerprints(
    catalog: dict,
    max_videos: int = FINGERPRINT_MAX_CATALOG,
    progress_cb: ProgressCb = None,
) -> List[Dict[str, Any]]:
    """Fingerprint up to *max_videos* official catalog entries.

    Prefers longer lectures (more distinctive) when duration is known.
    """
    entries = list(catalog.get("entries") or [])
    if not entries:
        return []

    # Prefer entries that look like full lectures if duration available later;
    # for now keep catalog order but cap.
    selected = entries[:max_videos]
    index: List[Dict[str, Any]] = []

    for i, entry in enumerate(selected):
        vid = entry.get("video_id")
        if not vid:
            continue
        _progress(
            progress_cb,
            f"Fingerprinting official catalog ({i + 1}/{len(selected)}): {vid}",
        )
        fp = fingerprint_video(vid)
        if not fp or not fp.get("values"):
            continue
        index.append(
            {
                "video_id": vid,
                "title": entry.get("title", ""),
                "channel_id": entry.get("channel_id", ""),
                "fingerprint": fp,
            }
        )

    logger.info("Catalog fingerprint index: %d / %d", len(index), len(selected))
    return index


def select_fingerprint_candidates(
    videos: List[dict],
    max_candidates: int = FINGERPRINT_MAX_CANDIDATES,
) -> List[dict]:
    """Pick top suspects for fingerprinting (heuristic score + lecture shape)."""
    ranked = []
    for v in videos:
        h = v.get("heuristic") or {}
        score = float(h.get("heuristic_score") or 0)
        duration = int(v.get("duration_seconds") or 0)
        if duration and duration < 180:
            # Shorts are rarely full lecture dumps for fingerprint ROI
            score *= 0.4
        elif duration >= PIRACY_MIN_DURATION_SECONDS:
            score += 10
        # Title catalog match already strong → prioritize audio proof
        if h.get("catalog_match"):
            score += 15
        ranked.append((score, v))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in ranked[:max_candidates]]


def match_against_catalog(
    fingerprint: Dict[str, Any],
    catalog_index: List[Dict[str, Any]],
    threshold: float = FINGERPRINT_MATCH_THRESHOLD,
) -> Optional[Dict[str, Any]]:
    """Return best catalog match above threshold, or None."""
    values = fingerprint.get("values") or []
    if not values or not catalog_index:
        return None

    backend = fingerprint.get("backend")
    best: Optional[Dict[str, Any]] = None
    best_sim = 0.0

    for item in catalog_index:
        item_fp = item.get("fingerprint") or {}
        other = item_fp.get("values") or []
        # Spectral and chromaprint encodings are incompatible
        if backend and item_fp.get("backend") and item_fp.get("backend") != backend:
            continue
        result = compare_fingerprints(values, other, threshold=threshold)
        sim = result["similarity"]
        if sim > best_sim:
            best_sim = sim
            best = {
                "official_video_id": item["video_id"],
                "official_title": item.get("title", ""),
                "official_channel_id": item.get("channel_id", ""),
                "similarity": sim,
                "is_match": sim >= threshold,
                "is_strong": sim >= FINGERPRINT_STRONG_THRESHOLD,
                "backend": backend,
            }

    if best and best["is_match"]:
        return best
    # Keep best near-miss for debugging / UI (not counted as match)
    if best:
        return best
    return None


def run_fingerprint_matching(
    catalog: dict,
    candidates: List[dict],
    max_catalog: int = FINGERPRINT_MAX_CATALOG,
    max_candidates: int = FINGERPRINT_MAX_CANDIDATES,
    progress_cb: ProgressCb = None,
) -> Dict[str, Any]:
    """Build catalog FP index and annotate candidates with fingerprint_match.

    Returns summary stats. Mutates candidate dicts in-place.
    """
    summary = {
        "catalog_fingerprinted": 0,
        "candidates_tested": 0,
        "matches": 0,
        "strong_matches": 0,
        "enabled": True,
        "skipped_reason": None,
    }

    if not (catalog.get("entries") or catalog.get("video_ids")):
        summary["enabled"] = False
        summary["skipped_reason"] = "no_official_catalog"
        logger.info("Fingerprint matching skipped: no official catalog")
        return summary

    catalog_index = build_catalog_fingerprints(
        catalog, max_videos=max_catalog, progress_cb=progress_cb
    )
    summary["catalog_fingerprinted"] = len(catalog_index)
    if not catalog_index:
        summary["enabled"] = False
        summary["skipped_reason"] = "catalog_fingerprint_failed"
        return summary

    selected = select_fingerprint_candidates(candidates, max_candidates=max_candidates)
    for i, video in enumerate(selected):
        vid = video.get("video_id")
        if not vid:
            continue
        _progress(
            progress_cb,
            f"Fingerprinting suspect ({i + 1}/{len(selected)}): {vid}",
        )
        fp = fingerprint_video(vid)
        summary["candidates_tested"] += 1
        if not fp:
            video["fingerprint_match"] = {
                "similarity": 0.0,
                "is_match": False,
                "error": "fingerprint_failed",
            }
            continue

        video["fingerprint"] = {
            "backend": fp.get("backend"),
            "length": fp.get("length"),
            "hash": fp.get("hash"),
        }
        match = match_against_catalog(fp, catalog_index)
        if match and match.get("is_match"):
            video["fingerprint_match"] = match
            summary["matches"] += 1
            if match.get("is_strong"):
                summary["strong_matches"] += 1
        else:
            video["fingerprint_match"] = match or {
                "similarity": 0.0,
                "is_match": False,
            }

    logger.info(
        "Fingerprint matching done: %d catalog, %d tested, %d matches (%d strong)",
        summary["catalog_fingerprinted"],
        summary["candidates_tested"],
        summary["matches"],
        summary["strong_matches"],
    )
    return summary
