import os
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Optional[str] = None) -> None:
    """Load KEY=VALUE pairs from a local .env into os.environ (no override)."""
    env_path = Path(path or Path(__file__).resolve().parent / ".env")
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_dotenv()

# =============================================================================
# YouTube Data API
# =============================================================================

# Set via environment only — never commit real keys.
YOUTUBE_API_KEYS = [
    k.strip()
    for k in os.environ.get("YOUTUBE_API_KEYS", "").split(",")
    if k.strip()
]

YOUTUBE_QUOTA_PER_KEY = 10_000
YOUTUBE_SEARCH_COST = 100
YOUTUBE_LIST_COST = 1

# =============================================================================
# OpenAI
# =============================================================================

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = "gpt-4o-mini"
VISION_MODEL = "gpt-4o"

# =============================================================================
# MongoDB
# =============================================================================

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB_NAME = "content_detector"

# =============================================================================
# Detection thresholds
# =============================================================================

MIN_CONFIDENCE = 60
DEFAULT_MAX_RESULTS = 200
THUMBNAIL_ANALYSIS_ENABLED = True

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# Channels with more subscribers than this are considered established media /
# news outlets and are heavily penalised (or filtered) for DEFAMATORY detection.
DEFAMATORY_BIG_CHANNEL_THRESHOLD = 100_000
DEFAMATORY_BIG_CHANNEL_MIN_CONFIDENCE = 85

# =============================================================================
# Entity-type-specific search query templates
# Each template uses {name} and optional {first} / {last} placeholders.
# =============================================================================

QUERY_TEMPLATES = {
    "educator": [
        '"{name}" meme',
        '"{name}" roast',
        '"{name}" troll',
        '"{name}" exposed',
        '"{name}" funny',
        '"{name}" scam',
        '"{name}" fraud',
        '"{name}" shorts',
        '"{name}" viral',
        "{name} meme compilation",
        "{name} roast compilation",
        "{name} sir meme",
        "{name} teacher meme",
        "{name} lecture funny",
        # piracy
        "{name} full lecture",
        "{name} full course",
        "{name} batch leaked",
        "{name} class recording",
        "{name} free download",
        "{name} complete course",
    ],
    "politician": [
        '"{name}" meme',
        '"{name}" roast',
        '"{name}" troll',
        '"{name}" exposed',
        '"{name}" funny',
        '"{name}" controversy',
        '"{name}" scandal',
        '"{name}" fail',
        '"{name}" cringe',
        '"{name}" viral',
        "{name} meme compilation",
        "{name} savage reply",
        "{name} getting trolled",
        "{name} speech funny",
        "{name} parliament meme",
        "{name} parody",
    ],
    "influencer": [
        '"{name}" meme',
        '"{name}" roast',
        '"{name}" troll',
        '"{name}" exposed',
        '"{name}" funny',
        '"{name}" cringe',
        '"{name}" reality',
        '"{name}" fake',
        '"{name}" scam',
        '"{name}" shorts',
        "{name} meme compilation",
        "{name} roast video",
        "{name} exposed truth",
        "{name} vs",
        "{name} parody",
        "{name} drama",
    ],
    "generic": [
        '"{name}" meme',
        '"{name}" roast',
        '"{name}" troll',
        '"{name}" exposed',
        '"{name}" funny',
        '"{name}" scam',
        '"{name}" viral',
        '"{name}" shorts',
        '"{name}" cringe',
        "{name} meme compilation",
        "{name} funny moments",
        "{name} roast compilation",
        "{name} parody",
        "{name} truth exposed",
        "{name} controversy",
    ],
}

# Piracy-specific templates appended when scan_type includes piracy.
# Pirates rarely put the official brand name first -- they use course names,
# subjects, "free" bait, and telegram/drive links.
PIRACY_QUERY_TEMPLATES = [
    # Direct re-upload patterns
    "{name} full video",
    "{name} full lecture",
    "{name} complete course",
    "{name} complete chapter",
    "{name} all lectures",
    "{name} one shot",

    # Telegram / file-sharing -- the #1 piracy signal per real-world data
    "{name} telegram",
    "{name} telegram link",
    "{name} telegram group",
    "{name} telegram channel",
    "{name} free telegram",
    "{name} course telegram",
    "{name} lecture telegram",
    "{name} drive link",
    "{name} google drive",
    "{name} mega link",
    "{name} free download",

    # "Free course" bait patterns
    "{name} free course",
    "{name} paid course free",
    "{name} premium free",
    "{name} course free",
    "{name} free lectures",
    "{name} leaked",
    "{name} leaked video",

    # Batch / class patterns (how pirates actually title things)
    "{name} batch",
    "{name} batch 2024",
    "{name} batch 2025",
    "{name} batch 2026",
    "{name} class recording",

    # Educational board / exam patterns
    "{name} class 11",
    "{name} class 12",
    "{name} class 9",
    "{name} class 10",
    "{name} IIT JEE",
    "{name} NEET",
    "{name} JEE Advanced",
    "{name} JEE Mains",

    # Subject-level patterns
    "{name} physics lecture",
    "{name} chemistry lecture",
    "{name} mathematics lecture",
    "{name} biology lecture",
    "{name} organic chemistry",

    # Platform shorthand
    "{name} PW",
    "{name} unacademy",
    "{name} vedantu",
]

# Obfuscated / brand-light piracy queries. Pirates often drop the brand or
# misspell it; these expand discovery beyond what a human typically types.
PIRACY_OBFUSCATED_TEMPLATES = [
    "{first} sir full lecture",
    "{first} sir batch",
    "{first} sir telegram",
    "{first} sir class recording",
    "{first} lectures free",
    "{name} part 1",
    "{name} part 2",
    "{name} full chapter",
    "{name} notes telegram",
    "{name} pdf telegram",
    "{name} recorded class",
    "{name} live class recording",
    "{name} crash course free",
    "{name} marathon lecture",
    "{name} revision lecture free",
]

# =============================================================================
# Phase A – deep discovery (catalog + anti-viral + channel crawl)
# =============================================================================

# Prefer date/relevance over viewCount so low-view mirrors can surface.
PIRACY_SEARCH_ORDERS = ("date", "relevance")

# Max videos to pull from each official channel/playlist into the catalog.
CATALOG_MAX_PER_SOURCE = 200

# After seed hits, deep-crawl this many suspicious channels' full uploads.
DEEP_CRAWL_MAX_CHANNELS = 25
DEEP_CRAWL_MAX_VIDEOS_PER_CHANNEL = 50

# Prefer crawling channels under this subscriber count (pirate dumps).
DEEP_CRAWL_MAX_SUBSCRIBERS = 50_000

# Minimum duration (seconds) to treat as lecture-like for piracy heuristics.
PIRACY_MIN_DURATION_SECONDS = 600  # 10 min

# Title similarity threshold vs official catalog (0-1 Jaccard).
CATALOG_TITLE_MATCH_THRESHOLD = 0.45

# =============================================================================
# Phase B – audio fingerprint matching vs official catalog
# =============================================================================

FINGERPRINT_ENABLED = os.environ.get("FINGERPRINT_ENABLED", "true").lower() in (
    "1", "true", "yes",
)

# How many seconds of audio to sample from the start of each video.
FINGERPRINT_SAMPLE_SECONDS = int(os.environ.get("FINGERPRINT_SAMPLE_SECONDS", "90"))

# Cap fingerprint work per scan (downloads are expensive).
FINGERPRINT_MAX_CATALOG = int(os.environ.get("FINGERPRINT_MAX_CATALOG", "40"))
FINGERPRINT_MAX_CANDIDATES = int(os.environ.get("FINGERPRINT_MAX_CANDIDATES", "25"))

# Similarity thresholds (0-1). Strong match can override LLM to PIRATED.
FINGERPRINT_MATCH_THRESHOLD = float(os.environ.get("FINGERPRINT_MATCH_THRESHOLD", "0.72"))
FINGERPRINT_STRONG_THRESHOLD = float(os.environ.get("FINGERPRINT_STRONG_THRESHOLD", "0.85"))

# Disk cache for extracted fingerprints / temp audio
FINGERPRINT_CACHE_DIR = os.environ.get(
    "FINGERPRINT_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), ".fp_cache"),
)

# =============================================================================
# Phase C – Telegram (sibling platform; shares catalog + fingerprints)
# =============================================================================

TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION = os.environ.get(
    "TELEGRAM_SESSION",
    os.path.join(os.path.dirname(__file__), ".telegram_session"),
)
# Optional: Telethon StringSession (avoids local session file)
TELEGRAM_SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()

TELEGRAM_ENABLED = bool(
    TELEGRAM_API_ID and TELEGRAM_API_HASH and (
        TELEGRAM_SESSION_STRING or os.path.exists(TELEGRAM_SESSION + ".session")
        or os.path.exists(TELEGRAM_SESSION)
    )
)

# How many recent messages to pull per channel per scan
TELEGRAM_MESSAGES_PER_CHANNEL = int(os.environ.get("TELEGRAM_MESSAGES_PER_CHANNEL", "150"))
# Max channels to deep-scan in one run
TELEGRAM_MAX_CHANNELS = int(os.environ.get("TELEGRAM_MAX_CHANNELS", "30"))
# Max media files to fingerprint per scan
TELEGRAM_MAX_MEDIA_FINGERPRINTS = int(os.environ.get("TELEGRAM_MAX_MEDIA_FINGERPRINTS", "15"))
# Expand invite links found inside scanned channels (1 hop)
TELEGRAM_EXPAND_INVITES = os.environ.get("TELEGRAM_EXPAND_INVITES", "true").lower() in (
    "1", "true", "yes",
)

TELEGRAM_CACHE_DIR = os.environ.get(
    "TELEGRAM_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), ".tg_cache"),
)

# =============================================================================
# Phase D – monitoring, alerts, review queue
# =============================================================================

MONITOR_ENABLED = os.environ.get("MONITOR_ENABLED", "true").lower() in (
    "1", "true", "yes",
)
# Background scheduler wake interval (seconds)
MONITOR_TICK_SECONDS = int(os.environ.get("MONITOR_TICK_SECONDS", "60"))
# Default re-scan interval for a new monitor (hours)
MONITOR_DEFAULT_INTERVAL_HOURS = int(os.environ.get("MONITOR_DEFAULT_INTERVAL_HOURS", "24"))
# =============================================================================
# Detection quality – ranking, budgets, verification, query expansion
# =============================================================================


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# videos.list costs 1 unit per 50 ids, so enrich (almost) everything discovered.
ENRICH_MAX_VIDEOS = _env_int("ENRICH_MAX_VIDEOS", 2000)
# search.list units one scan may spend (100 per call). Free yt-dlp/RSS run first.
YOUTUBE_SEARCH_BUDGET_PER_SCAN = _env_int("YOUTUBE_SEARCH_BUDGET_PER_SCAN", 2000)
# Free date-ordered yt-dlp searches (surface fresh, low-view uploads).
YTDLP_DATE_SEARCH_QUERIES = _env_int("YTDLP_DATE_SEARCH_QUERIES", 20)
# Parallel yt-dlp searches (keep small to stay polite).
YTDLP_SEARCH_WORKERS = _env_int("YTDLP_SEARCH_WORKERS", 4)
# search.list date windows in days back from now (0 = all time), newest uploads first.
YOUTUBE_SEARCH_WINDOWS_DAYS = tuple(
    int(x) for x in os.environ.get("YOUTUBE_SEARCH_WINDOWS_DAYS", "7,30,180,0").split(",") if x.strip().isdigit()
)
# Brand queries sent to the paid search.list API ({name} only; budget-capped).
YOUTUBE_API_QUERY_TEMPLATES = [
    "{name}", "{name} full lecture", "{name} free", "{name} telegram", "{name} batch",
]
# Official titles (spread over the whole catalog) turned into search queries.
CATALOG_QUERY_TITLES = _env_int("CATALOG_QUERY_TITLES", 25)
# A title word found in more than this share of official titles is a common
# course word (exam name, year) and does not identify a specific lecture.
CATALOG_COMMON_TOKEN_RATIO = _env_float("CATALOG_COMMON_TOKEN_RATIO", 0.05)
# Confirmed-hit titles re-used as queries on later scans (feedback).
FEEDBACK_MAX_TITLE_QUERIES = _env_int("FEEDBACK_MAX_TITLE_QUERIES", 10)
# Channels of confirmed hits that are re-crawled on later scans (feedback).
FEEDBACK_MAX_CHANNELS = _env_int("FEEDBACK_MAX_CHANNELS", 15)
# Written by scripts/benchmark.py; shown in the summary of empty runs.
BENCHMARK_RESULT_FILE = os.environ.get(
    "BENCHMARK_RESULT_FILE", os.path.join(os.path.dirname(__file__), "data", "benchmark_last.json")
)

# Transcript matching (YouTube captions via yt-dlp metadata; no media download)
TRANSCRIPT_ENABLED = os.environ.get("TRANSCRIPT_ENABLED", "true").lower() in ("1", "true", "yes")
TRANSCRIPT_MAX_CATALOG = _env_int("TRANSCRIPT_MAX_CATALOG", 120)
TRANSCRIPT_MAX_CANDIDATES = _env_int("TRANSCRIPT_MAX_CANDIDATES", 80)
TRANSCRIPT_WORKERS = _env_int("TRANSCRIPT_WORKERS", 2)
# YouTube rate-limits caption downloads per IP: pace them, and pause after repeated 429s.
TRANSCRIPT_MIN_INTERVAL_SECONDS = _env_float("TRANSCRIPT_MIN_INTERVAL_SECONDS", 2.0)
TRANSCRIPT_BACKOFF_SECONDS = _env_int("TRANSCRIPT_BACKOFF_SECONDS", 1800)
TRANSCRIPT_LANGS = tuple(
    x.strip() for x in os.environ.get("TRANSCRIPT_LANGS", "hi,en").split(",") if x.strip()
)
# Share of sampled 4-word shingles in common (untuned defaults; tune on benchmark).
TRANSCRIPT_CONFIRMED_OVERLAP = _env_float("TRANSCRIPT_CONFIRMED_OVERLAP", 0.40)
TRANSCRIPT_LIKELY_OVERLAP = _env_float("TRANSCRIPT_LIKELY_OVERLAP", 0.20)
TRANSCRIPT_MIN_SHARED = _env_int("TRANSCRIPT_MIN_SHARED", 12)
# "No captions" is re-checked after this long (auto-captions appear hours after upload).
TRANSCRIPT_NEGATIVE_TTL_HOURS = _env_int("TRANSCRIPT_NEGATIVE_TTL_HOURS", 72)

# LLM circuit breaker: after N auth/billing failures in a row, skip the LLM for a while.
LLM_BREAKER_FAILURES = _env_int("LLM_BREAKER_FAILURES", 3)
LLM_BREAKER_SECONDS = _env_int("LLM_BREAKER_SECONDS", 900)

# Suspects are sampled longer than official videos so the official opening is
# still found when the copy starts with its own intro.
FINGERPRINT_CANDIDATE_SAMPLE_SECONDS = _env_int("FINGERPRINT_CANDIDATE_SAMPLE_SECONDS", 240)
# Spectral fallback (used when fpcalc is not installed): never more than "likely".
# Measured on data/sample_*.csv: unrelated audio <= 0.57, aligned re-uploads >= 0.78.
FINGERPRINT_SPECTRAL_THRESHOLD = _env_float("FINGERPRINT_SPECTRAL_THRESHOLD", 0.70)

# Per-run funnel logs (JSON, one file per scan)
FUNNEL_LOG_DIR = os.environ.get(
    "FUNNEL_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs", "funnel")
)

# Hindi / Hinglish piracy phrasings ({name} = brand, course or instructor).
PIRACY_HINGLISH_TEMPLATES = [
    "{name} free me",
    "{name} free mein",
    "{name} poora course",
    "{name} pura course free",
    "{name} sabhi lecture",
    "{name} saare lecture",
    "{name} paid batch free",
    "{name} mirror",
    "{name} download",
    "{name} फ्री",
    "{name} पूरा कोर्स",
    "{name} लेक्चर",
    "{name} बैच",
]
# Title/description words that mark a re-upload (English + Hinglish + Hindi).
PIRACY_TITLE_KEYWORDS = [
    "full lecture", "complete course", "full course", "batch", "leaked", "free course",
    "telegram", "class recording", "one shot", "full chapter", "recorded class",
    "paid course", "free download", "all lectures",
    "free me", "free mein", "pura course", "poora course", "saare lecture", "sabhi lecture",
    "फ्री", "पूरा कोर्स", "लेक्चर", "बैच",
]
# Official vs. suspect duration tolerance (seconds or fraction, whichever is larger).
DURATION_MATCH_TOLERANCE_SECONDS = _env_int("DURATION_MATCH_TOLERANCE_SECONDS", 4)
DURATION_MATCH_TOLERANCE_RATIO = _env_float("DURATION_MATCH_TOLERANCE_RATIO", 0.01)

# Suffixes applied to misspelled / transliterated name variants.
QUERY_VARIANT_SUFFIXES = ["full lecture", "free", "batch", "telegram"]
QUERY_MAX_NAME_VARIANTS = _env_int("QUERY_MAX_NAME_VARIANTS", 4)

# Telegram public search + crawl
TELEGRAM_SEARCH_SUFFIXES = ["", "free", "batch", "lectures", "notes", "leaked", "course", "mirror", "download"]
TELEGRAM_MAX_SEARCH_QUERIES = _env_int("TELEGRAM_MAX_SEARCH_QUERIES", 20)
TELEGRAM_SEARCH_DELAY_SECONDS = _env_float("TELEGRAM_SEARCH_DELAY_SECONDS", 2.0)
# FloodWait up to this many seconds is slept through; longer stops the run (resumes next scan).
TELEGRAM_FLOOD_MAX_WAIT = _env_int("TELEGRAM_FLOOD_MAX_WAIT", 60)
# Only the first N MB of a file are downloaded for verification.
TELEGRAM_PARTIAL_DOWNLOAD_MB = _env_int("TELEGRAM_PARTIAL_DOWNLOAD_MB", 12)
# In a channel that only names the rights holder in its title/about, keep this many
# unverified file hits (the rest are counted in the funnel log, not saved).
TELEGRAM_WEAK_CONTEXT_HITS_PER_CHANNEL = _env_int("TELEGRAM_WEAK_CONTEXT_HITS_PER_CHANNEL", 5)
# Extra redistribution wording for Telegram posts (Hinglish/Hindi + common bait).
TELEGRAM_EXTRA_DUMP_KEYWORDS = [
    "free batch", "paid batch", "free me", "free mein", "pura course", "poora course",
    "saare lecture", "sabhi lecture", "all lectures", "lectures free",
    "direct download", "download link", "फ्री", "पूरा कोर्स",
]
# Words too generic to count as an entity mention.
TELEGRAM_TERM_STOPWORDS = {
    "by", "the", "and", "for", "sir", "mam", "maam", "dr", "of", "with", "official",
    "academy", "classes", "class", "education", "educators", "institute", "coaching",
    "excellence", "tutorials", "study", "learning", "grow", "finance", "commerce",
}

# Only include confirmed hits in takedown packs by default (pending still optional)
TAKEDOWN_REQUIRE_CONFIRMED = os.environ.get(
    "TAKEDOWN_REQUIRE_CONFIRMED", "false"
).lower() in ("1", "true", "yes")
