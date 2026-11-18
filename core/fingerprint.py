"""Audio fingerprint extraction and comparison (Phase B).

Backends (auto-selected):
  1. chromaprint via `fpcalc` if installed (best)
  2. spectral band-energy fingerprint from WAV (stdlib + optional numpy)

Fingerprints are compact integer sequences comparable with sliding similarity,
so re-encoded / slightly trimmed lecture re-uploads still match.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import FINGERPRINT_CACHE_DIR, FINGERPRINT_MATCH_THRESHOLD

logger = logging.getLogger(__name__)

Fingerprint = List[int]


# ---------------------------------------------------------------------------
# Disk cache for computed fingerprints
# ---------------------------------------------------------------------------


def _fp_cache_path(video_id: str, backend: str) -> Path:
    root = Path(FINGERPRINT_CACHE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    (root / "prints").mkdir(exist_ok=True)
    return root / "prints" / f"{video_id}_{backend}.json"


def load_cached_fingerprint(video_id: str, backend: str) -> Optional[Dict[str, Any]]:
    path = _fp_cache_path(video_id, backend)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cached_fingerprint(video_id: str, backend: str, payload: Dict[str, Any]) -> None:
    path = _fp_cache_path(video_id, backend)
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not cache fingerprint for %s: %s", video_id, exc)


# ---------------------------------------------------------------------------
# WAV loading
# ---------------------------------------------------------------------------


def read_wav_mono(path: str) -> Tuple[List[float], int]:
    """Return (samples in [-1,1], sample_rate)."""
    with wave.open(path, "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)

    if sw == 1:
        # unsigned 8-bit
        samples = [(b - 128) / 128.0 for b in raw]
    elif sw == 2:
        count = len(raw) // 2
        ints = struct.unpack("<" + "h" * count, raw[: count * 2])
        samples = [i / 32768.0 for i in ints]
    elif sw == 4:
        count = len(raw) // 4
        ints = struct.unpack("<" + "i" * count, raw[: count * 4])
        samples = [i / 2147483648.0 for i in ints]
    else:
        raise ValueError(f"Unsupported sample width: {sw}")

    if nch > 1:
        mono: List[float] = []
        for i in range(0, len(samples), nch):
            frame = samples[i : i + nch]
            mono.append(sum(frame) / len(frame))
        samples = mono

    return samples, rate


# ---------------------------------------------------------------------------
# Spectral fingerprint (portable fallback)
# ---------------------------------------------------------------------------


def _downsample(samples: Sequence[float], factor: int) -> List[float]:
    if factor <= 1:
        return list(samples)
    out: List[float] = []
    for i in range(0, len(samples) - factor + 1, factor):
        chunk = samples[i : i + factor]
        out.append(sum(chunk) / len(chunk))
    return out


def _band_energies_fast(frame: Sequence[float], n_bands: int = 8) -> List[float]:
    """Band energies via numpy FFT if available, else time-domain block RMS."""
    n = len(frame)
    if n < 16:
        return [0.0] * n_bands

    try:
        import numpy as np  # optional

        arr = np.asarray(frame, dtype=np.float32)
        arr = arr * np.hanning(len(arr))
        spec = np.abs(np.fft.rfft(arr))
        edges = np.linspace(1, len(spec), n_bands + 1, dtype=int)
        bands = []
        for i in range(n_bands):
            segment = spec[edges[i] : edges[i + 1]]
            bands.append(float(segment.mean()) if len(segment) else 0.0)
        return bands
    except Exception:
        pass

    # Stdlib fallback: split frame into n_bands time blocks and use RMS
    # (coarse but fast; still useful for identical re-encodes of lectures)
    block = max(n // n_bands, 1)
    bands = []
    for i in range(n_bands):
        chunk = frame[i * block : (i + 1) * block]
        if not chunk:
            bands.append(0.0)
            continue
        bands.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)))
    return bands


def spectral_fingerprint(
    samples: Sequence[float],
    sample_rate: int,
    frame_ms: int = 200,
    hop_ms: int = 100,
    n_bands: int = 8,
) -> Fingerprint:
    """Encode relative band-energy peaks as a compact int sequence."""
    # Work at ~4kHz equivalent for speed
    factor = max(sample_rate // 4000, 1)
    ds = _downsample(samples, factor)
    rate = sample_rate // factor

    frame_len = max(int(rate * frame_ms / 1000), 64)
    hop = max(int(rate * hop_ms / 1000), 32)
    codes: Fingerprint = []

    prev: Optional[List[float]] = None
    for start in range(0, max(len(ds) - frame_len, 0), hop):
        frame = ds[start : start + frame_len]
        bands = _band_energies_fast(frame, n_bands=n_bands)
        # Spectral shape: adjacent-band comparisons (discriminative for different content)
        bits = 0
        for i in range(n_bands - 1):
            if bands[i] > bands[i + 1]:
                bits |= 1 << i
        # Temporal deltas vs previous frame (helps speech/lecture dynamics)
        if prev is not None:
            for i, (cur, old) in enumerate(zip(bands, prev)):
                if cur > old * 1.05:
                    bits |= 1 << (n_bands - 1 + i)
        codes.append(bits)
        prev = bands

    return codes


# ---------------------------------------------------------------------------
# Chromaprint (fpcalc) backend
# ---------------------------------------------------------------------------


def chromaprint_available() -> bool:
    try:
        r = subprocess.run(
            ["fpcalc", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def chromaprint_fingerprint(wav_path: str, length: int = 90) -> Optional[Fingerprint]:
    """Run fpcalc -raw and return integer fingerprint list."""
    try:
        r = subprocess.run(
            ["fpcalc", "-raw", "-length", str(length), wav_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            logger.warning("fpcalc failed: %s", r.stderr.strip())
            return None
        # Output: DURATION=..\nFINGERPRINT=1,2,3,...
        raw = ""
        for line in r.stdout.splitlines():
            if line.startswith("FINGERPRINT="):
                raw = line.split("=", 1)[1].strip()
                break
        if not raw:
            return None
        return [int(x) for x in raw.split(",") if x.strip()]
    except Exception as exc:
        logger.warning("chromaprint error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _hamming32(a: int, b: int) -> int:
    return (a ^ b).bit_count() if hasattr(int, "bit_count") else bin(a ^ b).count("1")


def fingerprint_similarity(
    a: Sequence[int],
    b: Sequence[int],
    max_offset: int = 40,
) -> float:
    """Sliding-window similarity in [0, 1]. Robust to small start-time shifts."""
    if not a or not b:
        return 0.0

    # Prefer comparing the shorter against the longer with offsets
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 8:
        return 0.0

    bits_per = 32  # chromaprint ints; spectral uses <=8 but same scorer works
    best = 0.0
    span = min(max_offset, max(len(long_) - len(short), 0))

    for offset in range(0, span + 1, 2):
        window = long_[offset : offset + len(short)]
        if len(window) < len(short):
            break
        matched_bits = 0
        total_bits = 0
        for x, y in zip(short, window):
            # For spectral (small ints) hamming still fine
            width = max(x.bit_length(), y.bit_length(), 8)
            matched_bits += width - _hamming32(x & ((1 << width) - 1), y & ((1 << width) - 1))
            total_bits += width
        if total_bits:
            best = max(best, matched_bits / total_bits)

    # Also try reverse roles with coarser step if lengths differ a lot
    if abs(len(a) - len(b)) > 20:
        # subsample longer to short length for a quick extra pass
        step = max(len(long_) // len(short), 1)
        sampled = long_[::step][: len(short)]
        if len(sampled) == len(short):
            matched_bits = 0
            total_bits = 0
            for x, y in zip(short, sampled):
                width = max(x.bit_length(), y.bit_length(), 8)
                matched_bits += width - _hamming32(x & ((1 << width) - 1), y & ((1 << width) - 1))
                total_bits += width
            if total_bits:
                best = max(best, matched_bits / total_bits)

    return round(best, 4)


def compare_fingerprints(
    a: Sequence[int],
    b: Sequence[int],
    threshold: float = FINGERPRINT_MATCH_THRESHOLD,
) -> Dict[str, Any]:
    sim = fingerprint_similarity(a, b)
    return {
        "similarity": sim,
        "is_match": sim >= threshold,
    }


# ---------------------------------------------------------------------------
# High-level extract
# ---------------------------------------------------------------------------


def extract_fingerprint_from_wav(
    wav_path: str,
    video_id: Optional[str] = None,
    prefer_chromaprint: bool = True,
) -> Optional[Dict[str, Any]]:
    """Extract fingerprint dict: {backend, values, duration_est, hash}."""
    backend = "spectral"
    values: Optional[Fingerprint] = None

    if prefer_chromaprint and chromaprint_available():
        values = chromaprint_fingerprint(wav_path)
        if values:
            backend = "chromaprint"

    if values is None:
        try:
            samples, rate = read_wav_mono(wav_path)
            # Downsample processing for speed if very long
            max_samples = rate * 120
            if len(samples) > max_samples:
                samples = samples[:max_samples]
            values = spectral_fingerprint(samples, rate)
            backend = "spectral"
        except Exception as exc:
            logger.error("Spectral fingerprint failed for %s: %s", wav_path, exc)
            return None

    if not values:
        return None

    payload = {
        "backend": backend,
        "values": values,
        "length": len(values),
        "hash": hashlib.sha1(",".join(map(str, values[:64])).encode()).hexdigest()[:16],
    }
    if video_id:
        save_cached_fingerprint(video_id, backend, payload)
    return payload


def get_or_compute_fingerprint(
    video_id: str,
    wav_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Return cached fingerprint or compute from wav."""
    for backend in ("chromaprint", "spectral"):
        cached = load_cached_fingerprint(video_id, backend)
        if cached and cached.get("values"):
            return cached
    if not wav_path:
        return None
    return extract_fingerprint_from_wav(wav_path, video_id=video_id)
