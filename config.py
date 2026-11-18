import os

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
# Only include confirmed hits in takedown packs by default (pending still optional)
TAKEDOWN_REQUIRE_CONFIRMED = os.environ.get(
    "TAKEDOWN_REQUIRE_CONFIRMED", "false"
).lower() in ("1", "true", "yes")
