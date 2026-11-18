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


_chromaprint_ok: Optional[bool] = None


def chromaprint_available() -> bool:
    """True if the `fpcalc` binary works (checked once per process)."""
    global _chromaprint_ok
    if _chromaprint_ok is None:
        try:
            r = subprocess.run(
                ["fpcalc", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            _chromaprint_ok = r.returncode == 0
        except Exception:
            _chromaprint_ok = False
    return _chromaprint_ok


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


def _fp_bits(a: Sequence[int], b: Sequence[int]) -> int:
    """Chromaprint items are 32-bit; the spectral fallback uses 15 bits."""
    return 32 if max(max(a), max(b)) >= (1 << 16) else 15


_POPCOUNT8 = None


def fingerprint_similarity(
    a: Sequence[int],
    b: Sequence[int],
    max_offset: Optional[int] = None,
    min_overlap: int = 400,
) -> float:
    """Best bit agreement in [0, 1] over every alignment of the two prints.

    One item is ~0.1 s. The prints slide fully across each other as long as
    at least *min_overlap* items (~40 s) overlap, so a 90 s official sample is
    found anywhere inside a longer suspect sample (added intro), and two
    equal-length samples tolerate a shifted start. The previous version did
    not slide equal-length samples at all.
    """
    if not a or not b or min(len(a), len(b)) < 8:
        return 0.0

    import numpy as np

    global _POPCOUNT8
    if _POPCOUNT8 is None:
        _POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)

    bits = _fp_bits(a, b)
    mask = (1 << bits) - 1
    x = np.asarray([v & mask for v in a], dtype=np.uint32)
    y = np.asarray([v & mask for v in b], dtype=np.uint32)
    need = min(min_overlap, len(x), len(y))
    best = 0.0
    lo, hi = -(len(y) - need), len(x) - need
    if max_offset is not None:
        lo, hi = max(lo, -max_offset), min(hi, max_offset)
    for off in range(lo, hi + 1):
        # off >= 0: x[off:] against y;  off < 0: x against y[-off:]
        xs = x[off:] if off >= 0 else x
        ys = y if off >= 0 else y[-off:]
        n = min(len(xs), len(ys))
        if n < need:
            continue
        diff = np.bitwise_xor(xs[:n], ys[:n])
        wrong = int(_POPCOUNT8[diff.view(np.uint8)].sum())
        sim = 1.0 - wrong / float(n * bits)
        if sim > best:
            best = sim
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
    max_seconds: int = 600,
) -> Optional[Dict[str, Any]]:
    """Extract fingerprint dict: {backend, values, duration_est, hash}."""
    backend = "spectral"
    values: Optional[Fingerprint] = None

    if prefer_chromaprint and chromaprint_available():
        values = chromaprint_fingerprint(wav_path, length=max_seconds)
        if values:
            backend = "chromaprint"

    if values is None:
        try:
            samples, rate = read_wav_mono(wav_path)
            # Cap processing time on very long files
            max_samples = rate * max_seconds
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
    """Return cached fingerprint or compute from wav.

    A cached spectral print is only reused while chromaprint is unavailable:
    the two encodings cannot be compared, so mixing them would silently skip
    comparisons once fpcalc gets installed.
    """
    backends = ("chromaprint",) if chromaprint_available() else ("chromaprint", "spectral")
    for backend in backends:
        cached = load_cached_fingerprint(video_id, backend)
        if cached and cached.get("values"):
            return cached
    if not wav_path:
        return None
    return extract_fingerprint_from_wav(wav_path, video_id=video_id)
