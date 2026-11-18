"""Test setup: no network, no real DB, temp dirs for every local cache."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="piracy_tests_")
os.environ["MONGODB_URI"] = "mongodb://127.0.0.1:1/"  # unreachable -> in-memory store
os.environ["MONITOR_ENABLED"] = "false"
os.environ["OPENAI_API_KEY"] = ""
os.environ["YOUTUBE_API_KEYS"] = ""
os.environ["FUNNEL_LOG_DIR"] = os.path.join(_TMP, "funnel")
os.environ["FINGERPRINT_CACHE_DIR"] = os.path.join(_TMP, "fp")
os.environ["TELEGRAM_CACHE_DIR"] = os.path.join(_TMP, "tg")
os.environ["TELEGRAM_SEARCH_DELAY_SECONDS"] = "0"
os.environ["BENCHMARK_RESULT_FILE"] = os.path.join(_TMP, "benchmark_last.json")
