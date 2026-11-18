"""Phase C – Telegram piracy scanner (sibling to YouTube).

Connected: seeds from YouTube descriptions, shared fingerprint/catalog DB.
Separate: Telegram-only dumps that never appear on YouTube.
"""

from core.telegram.scanner import run_telegram_scan
from core.telegram.seeds import extract_telegram_seeds, merge_seed_lists

__all__ = [
    "run_telegram_scan",
    "extract_telegram_seeds",
    "merge_seed_lists",
]
