"""Download short audio samples from YouTube via yt-dlp for fingerprinting."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from config import FINGERPRINT_CACHE_DIR, FINGERPRINT_SAMPLE_SECONDS

logger = logging.getLogger(__name__)


def _ensure_cache_dir() -> Path:
    path = Path(FINGERPRINT_CACHE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    (path / "audio").mkdir(exist_ok=True)
    return path


def audio_cache_path(video_id: str, sample_seconds: int = FINGERPRINT_SAMPLE_SECONDS) -> Path:
    root = _ensure_cache_dir()
    return root / "audio" / f"{video_id}_{sample_seconds}s.wav"


def download_audio_sample(
    video_id: str,
    sample_seconds: int = FINGERPRINT_SAMPLE_SECONDS,
    force: bool = False,
) -> Optional[str]:
    """Download the first *sample_seconds* of audio as WAV. Returns file path or None."""
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp not installed – cannot download audio for fingerprinting")
        return None

    out_path = audio_cache_path(video_id, sample_seconds)
    if out_path.exists() and out_path.stat().st_size > 1000 and not force:
        return str(out_path)

    url = f"https://www.youtube.com/watch?v={video_id}"
    tmp_dir = tempfile.mkdtemp(prefix="fp_dl_")
    # yt-dlp adds extension; we request wav via postprocessor
    outtmpl = os.path.join(tmp_dir, f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
        "noplaylist": True,
        # Only fetch the opening window – critical for speed on long lectures.
        # (The CLI-only "download_sections" key used before was ignored by the
        # Python API, so whole lectures were being downloaded.)
        "download_ranges": yt_dlp.utils.download_range_func(None, [(0, sample_seconds)]),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            }
        ],
        "postprocessor_args": {
            "ffmpeg": ["-ac", "1", "-ar", "16000"],
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find produced wav
        produced = None
        for name in os.listdir(tmp_dir):
            if name.lower().endswith(".wav"):
                produced = os.path.join(tmp_dir, name)
                break
        if not produced or not os.path.exists(produced):
            # Sometimes extract leaves m4a/webm if ffmpeg missing
            logger.warning("No WAV produced for %s (is ffmpeg installed?)", video_id)
            return None

        shutil.move(produced, str(out_path))
        logger.info("Audio sample ready: %s (%d bytes)", out_path.name, out_path.stat().st_size)
        return str(out_path)
    except Exception as exc:
        logger.error("Audio download failed for %s: %s", video_id, exc)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


