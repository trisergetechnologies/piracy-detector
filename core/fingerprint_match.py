"""Content verification vs the official catalog (cheap -> expensive).

Order per scan:
  1. transcript sketch overlap (captions; no media download)
  2. audio fingerprint of a short sample (only for suspects still unproven)

Every match carries a `tier`:
  confirmed – content match strong enough to act on
  likely    – content match that needs a human look
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from config import (
    FINGERPRINT_CANDIDATE_SAMPLE_SECONDS,
    FINGERPRINT_MATCH_THRESHOLD,
    FINGERPRINT_MAX_CANDIDATES,
    FINGERPRINT_MAX_CATALOG,
    FINGERPRINT_SAMPLE_SECONDS,
    FINGERPRINT_SPECTRAL_THRESHOLD,
    FINGERPRINT_STRONG_THRESHOLD,
    PIRACY_MIN_DURATION_SECONDS,
    TRANSCRIPT_ENABLED,
)
from core import funnel
from core.fingerprint import (
    extract_fingerprint_from_wav,
    fingerprint_similarity,
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
    # A cached print from a shorter sample is recomputed (one item is ~0.1 s).
    if existing and int(existing.get("length") or 0) >= sample_seconds * 6:
        return existing

    wav = download_audio_sample(video_id, sample_seconds=sample_seconds)
    if not wav:
        return existing  # a shorter cached print is better than none
    return extract_fingerprint_from_wav(wav, video_id=video_id) or existing


def _priority_official_ids(candidates: List[dict]) -> List[str]:
    """Official videos already pointed at by title/duration/transcript evidence."""
    ids: List[str] = []
    for v in candidates:
        h = v.get("heuristic") or {}
        refs = [
            (h.get("catalog_match") or {}).get("official_video_id"),
            (v.get("fingerprint_match") or {}).get("official_video_id"),
            (v.get("transcript_near_miss") or {}).get("official_video_id"),
        ]
        refs += [s.split(":", 1)[1] for s in h.get("signals") or []
                 if s.startswith("duration_equals_official:")]
        for ref in refs:
            if ref and ref not in ids:
                ids.append(ref)
    return ids


def build_catalog_fingerprints(
    catalog: dict,
    max_videos: int = FINGERPRINT_MAX_CATALOG,
    progress_cb: ProgressCb = None,
    priority_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Fingerprint up to *max_videos* official entries.

    Entries that a suspect already resembles (priority_ids) are fingerprinted
    first, so the cap is spent where a comparison is actually pending.
    """
    entries = list(catalog.get("entries") or [])
    if not entries:
        return []

    rank = {vid: i for i, vid in reversed(list(enumerate(priority_ids or [])))}
    entries.sort(key=lambda e: rank.get(e.get("video_id"), len(rank) + 1))
    selected = entries[:max_videos]
    if len(entries) > len(selected):
        funnel.current().note("fingerprint_catalog_cap", {
            "used": len(selected), "catalog": len(entries),
        })
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
            funnel.current().unreachable(f"official:{vid}", "audio fingerprint failed")
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
    """Suspects still lacking content proof, strongest evidence first."""
    ranked = []
    for v in videos:
        fm = v.get("fingerprint_match") or {}
        if fm.get("tier") == "confirmed":
            continue  # already proven by transcript
        h = v.get("heuristic") or {}
        score = float(h.get("heuristic_score") or 0)
        duration = int(v.get("duration_seconds") or 0)
        if duration and duration < 60:
            score *= 0.4  # too short to carry a lecture
        elif duration >= PIRACY_MIN_DURATION_SECONDS:
            score += 10
        if h.get("catalog_match"):
            score += 15
        if fm.get("tier") == "likely" or v.get("transcript_near_miss"):
            score += 20  # audio can settle a borderline transcript result
        ranked.append((score, v))

    ranked.sort(key=lambda x: x[0], reverse=True)
    for _, v in ranked[max_candidates:]:
        funnel.current().drop(v.get("video_id"), "verify_audio", "candidate_cap", final=False)
    return [v for _, v in ranked[:max_candidates]]


def match_against_catalog(
    fingerprint: Dict[str, Any],
    catalog_index: List[Dict[str, Any]],
    threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Best catalog match (also returned below threshold, with is_match False).

    Chromaprint: >= strong threshold is `confirmed`, >= match threshold `likely`.
    The spectral fallback (no fpcalc) is coarse, so it needs a higher threshold
    and never yields more than `likely`.
    """
    values = fingerprint.get("values") or []
    if not values or not catalog_index:
        return None

    backend = fingerprint.get("backend")
    spectral = backend != "chromaprint"
    if threshold is None:
        threshold = FINGERPRINT_SPECTRAL_THRESHOLD if spectral else FINGERPRINT_MATCH_THRESHOLD
    best: Optional[Dict[str, Any]] = None
    best_sim = 0.0

    for item in catalog_index:
        item_fp = item.get("fingerprint") or {}
        other = item_fp.get("values") or []
        # Spectral and chromaprint encodings are incompatible
        if backend and item_fp.get("backend") and item_fp.get("backend") != backend:
            continue
        sim = fingerprint_similarity(values, other)
        if sim > best_sim:
            best_sim = sim
            is_match = sim >= threshold
            is_strong = is_match and not spectral and sim >= FINGERPRINT_STRONG_THRESHOLD
            best = {
                "official_video_id": item["video_id"],
                "official_title": item.get("title", ""),
                "official_channel_id": item.get("channel_id", ""),
                "similarity": sim,
                "is_match": is_match,
                "is_strong": is_strong,
                "backend": backend,
                "tier": ("confirmed" if is_strong else "likely") if is_match else None,
            }
    return best


def _audio_stage(
    catalog: dict,
    candidates: List[dict],
    max_catalog: int,
    max_candidates: int,
    progress_cb: ProgressCb,
    summary: Dict[str, Any],
) -> None:
    fn = funnel.current()
    selected = select_fingerprint_candidates(candidates, max_candidates=max_candidates)
    if not selected:
        return
    catalog_index = build_catalog_fingerprints(
        catalog, max_videos=max_catalog, progress_cb=progress_cb,
        priority_ids=_priority_official_ids(selected),
    )
    summary["catalog_fingerprinted"] = len(catalog_index)
    if not catalog_index:
        summary["skipped_reason"] = "catalog_fingerprint_failed"
        fn.error("fingerprint", "no official audio could be fingerprinted")
        return
    summary["backend"] = (catalog_index[0].get("fingerprint") or {}).get("backend")

    for i, video in enumerate(selected):
        vid = video.get("video_id")
        if not vid:
            continue
        _progress(progress_cb, f"Fingerprinting suspect ({i + 1}/{len(selected)}): {vid}")
        # Longer sample than the official one: finds it behind an added intro.
        fp = fingerprint_video(vid, sample_seconds=FINGERPRINT_CANDIDATE_SAMPLE_SECONDS)
        summary["candidates_tested"] += 1
        if not fp:
            fn.drop(vid, "verify_audio", "fingerprint_failed", final=False)
            video.setdefault("fingerprint_match", {
                "similarity": 0.0, "is_match": False, "error": "fingerprint_failed",
            })
            continue

        video["fingerprint"] = {
            "backend": fp.get("backend"),
            "length": fp.get("length"),
            "hash": fp.get("hash"),
        }
        match = match_against_catalog(fp, catalog_index)
        prior = video.get("fingerprint_match") or {}
        if match and match.get("is_match"):
            summary["audio_matches"] += 1
            if prior.get("is_match") and prior.get("backend") == "transcript":
                # Two independent content checks agree -> confirmed.
                prior.update(tier="confirmed", is_strong=True,
                             audio_similarity=match["similarity"], audio_backend=match["backend"])
            else:
                video["fingerprint_match"] = match
        elif not prior.get("is_match"):
            video["fingerprint_match"] = match or {"similarity": 0.0, "is_match": False}


def run_fingerprint_matching(
    catalog: dict,
    candidates: List[dict],
    max_catalog: int = FINGERPRINT_MAX_CATALOG,
    max_candidates: int = FINGERPRINT_MAX_CANDIDATES,
    progress_cb: ProgressCb = None,
    audio: bool = True,
) -> Dict[str, Any]:
    """Verify candidates against official content. Mutates candidate dicts.

    *candidates* must already be in evidence order (strongest first).
    audio=False runs only the transcript stage (no media download).
    """
    summary: Dict[str, Any] = {
        "catalog_fingerprinted": 0,
        "candidates_tested": 0,
        "matches": 0,
        "strong_matches": 0,
        "audio_matches": 0,
        "enabled": True,
        "skipped_reason": None,
    }

    if not catalog.get("entries"):
        summary["enabled"] = False
        summary["skipped_reason"] = "no_official_catalog"
        logger.info("Content matching skipped: no official catalog")
        return summary

    if TRANSCRIPT_ENABLED:
        try:
            from core.transcript_match import run_transcript_matching

            summary["transcript"] = run_transcript_matching(
                catalog, candidates, progress_cb=progress_cb
            )
        except Exception as exc:  # never lose the scan to a verification error
            logger.exception("Transcript matching failed")
            funnel.current().error("transcript_matching", str(exc))
            summary["transcript"] = {"enabled": False, "skipped_reason": f"error: {exc}"}

    try:
        if audio:
            _audio_stage(catalog, candidates, max_catalog, max_candidates, progress_cb, summary)
        else:
            summary["skipped_reason"] = "audio_fingerprinting_disabled"
    except Exception as exc:
        logger.exception("Audio fingerprint stage failed")
        funnel.current().error("fingerprint", str(exc))
        summary["skipped_reason"] = f"audio_error: {exc}"

    for v in candidates:
        fm = v.get("fingerprint_match") or {}
        if fm.get("is_match"):
            summary["matches"] += 1
            if fm.get("tier") == "confirmed":
                summary["strong_matches"] += 1

    logger.info("Content matching done: %s", {k: v for k, v in summary.items() if k != "transcript"})
    return summary
