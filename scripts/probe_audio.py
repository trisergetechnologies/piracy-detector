"""Manual probe: audio-fingerprint similarity between one official video and others.

Usage:
    python scripts/probe_audio.py OFFICIAL_ID VIDEO_ID [VIDEO_ID ...]

Prints the backend in use (chromaprint if `fpcalc` is installed, else the
spectral fallback) and the similarity of every video to the official one.
Use it to check thresholds on pairs you know are copies / not copies.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core.fingerprint import fingerprint_similarity  # noqa: E402
from core.fingerprint_match import fingerprint_video  # noqa: E402


def main() -> None:
    ids = sys.argv[1:]
    if len(ids) < 2:
        print(__doc__)
        return
    official = fingerprint_video(ids[0])
    if not official:
        print(f"could not fingerprint {ids[0]}")
        return
    backend = official["backend"]
    threshold = (config.FINGERPRINT_MATCH_THRESHOLD if backend == "chromaprint"
                 else config.FINGERPRINT_SPECTRAL_THRESHOLD)
    print(f"backend={backend} items={official['length']} match threshold={threshold}")
    for vid in ids[1:]:
        fp = fingerprint_video(vid, sample_seconds=config.FINGERPRINT_CANDIDATE_SAMPLE_SECONDS)
        if not fp:
            print(f"  {vid}: fingerprint failed")
            continue
        if fp["backend"] != backend:
            print(f"  {vid}: backend {fp['backend']} differs - not comparable")
            continue
        sim = fingerprint_similarity(official["values"], fp["values"])
        print(f"  {vid}: similarity={sim} {'MATCH' if sim >= threshold else ''}")


if __name__ == "__main__":
    main()
