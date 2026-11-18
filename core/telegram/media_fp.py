"""Convert Telegram media files to WAV samples and fingerprint them."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    FINGERPRINT_MATCH_THRESHOLD,
    FINGERPRINT_SAMPLE_SECONDS,
    FINGERPRINT_STRONG_THRESHOLD,
    TELEGRAM_CACHE_DIR,
)
from core.fingerprint import extract_fingerprint_from_wav, fingerprint_similarity

logger = logging.getLogger(__name__)


def media_to_wav_sample(
    media_path: str,
    sample_seconds: int = FINGERPRINT_SAMPLE_SECONDS,
) -> Optional[str]:
    """Use ffmpeg to extract a mono 16kHz WAV sample from video/audio file."""
    if not media_path or not os.path.exists(media_path):
        return None

    out_dir = Path(TELEGRAM_CACHE_DIR) / "wav"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(media_path).stem
    out_path = out_dir / f"{stem}_{sample_seconds}s.wav"
    if out_path.exists() and out_path.stat().st_size > 1000:
        return str(out_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", media_path,
        "-t", str(sample_seconds),
        "-ac", "1",
        "-ar", "16000",
        "-vn",
        str(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not out_path.exists():
            logger.warning("ffmpeg failed for %s: %s", media_path, r.stderr[-400:])
            return None
        return str(out_path)
    except FileNotFoundError:
        logger.error("ffmpeg not found – cannot fingerprint Telegram media")
        return None
    except Exception as exc:
        logger.warning("media_to_wav failed: %s", exc)
        return None


def fingerprint_local_media(media_path: str) -> Optional[Dict[str, Any]]:
    wav = media_to_wav_sample(media_path)
    if not wav:
        return None
    return extract_fingerprint_from_wav(wav, video_id=None)


def match_media_to_catalog(
    media_fp: Dict[str, Any],
    catalog_index: List[Dict[str, Any]],
    threshold: float = FINGERPRINT_MATCH_THRESHOLD,
) -> Optional[Dict[str, Any]]:
    """Same comparison used for YouTube Phase B, for Telegram files."""
    values = media_fp.get("values") or []
    if not values or not catalog_index:
        return None

    backend = media_fp.get("backend")
    best = None
    best_sim = 0.0
    for item in catalog_index:
        item_fp = item.get("fingerprint") or {}
        if backend and item_fp.get("backend") and item_fp.get("backend") != backend:
            continue
        other = item_fp.get("values") or []
        sim = fingerprint_similarity(values, other)
        if sim > best_sim:
            best_sim = sim
            best = {
                "official_video_id": item.get("video_id"),
                "official_title": item.get("title", ""),
                "similarity": sim,
                "is_match": sim >= threshold,
                "is_strong": sim >= FINGERPRINT_STRONG_THRESHOLD,
                "backend": backend,
                "platform": "telegram",
            }
    if best and best["is_match"]:
        return best
    return best
