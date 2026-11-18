"""YouTube Data API client with key rotation, quota tracking, batching, and caching."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    YOUTUBE_API_KEYS,
    YOUTUBE_LIST_COST,
    YOUTUBE_QUOTA_PER_KEY,
    YOUTUBE_SEARCH_COST,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(duration_str: str) -> int:
    """Parse ISO 8601 duration string to total seconds."""
    if not duration_str:
        return 0
    m = _ISO_DUR.match(duration_str)
    if not m:
        return 0
    return int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0)


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


_UC_ID = re.compile(r"UC[\w-]{22}")


def parse_channel_ref(ref: str) -> tuple:
    """Classify a channel reference -> (kind, value); kind: id|handle|user|name."""
    ref = (ref or "").strip().rstrip("/")
    if not ref:
        return "name", ""
    if _UC_ID.fullmatch(ref):
        return "id", ref
    m = re.search(r"/channel/(UC[\w-]{22})", ref)
    if m:
        return "id", m.group(1)
    m = re.search(r"(?:^|/)@([\w.\-]+)", ref)
    if m:
        return "handle", m.group(1)
    m = re.search(r"/user/([\w.\-]+)", ref)
    if m:
        return "user", m.group(1)
    m = re.search(r"youtube\.com/(?:c/)?([\w.\-]+)$", ref)
    if m:
        return "name", m.group(1)
    return "name", ref if re.fullmatch(r"[\w.\-]+", ref) else ""


def parse_playlist_ref(ref: str) -> str:
    """Accept a playlist id or URL containing list=… and return the id ('' if none)."""
    ref = (ref or "").strip()
    m = re.search(r"[?&]list=([\w-]+)", ref)
    if m:
        return m.group(1)
    return ref if re.fullmatch(r"[\w-]{12,}", ref) else ""


def _resolve_channel_via_ytdlp(url: str) -> Optional[str]:
    """Zero-quota fallback: read the channel id from the channel page."""
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "playlist_items": "0", "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        cid = info.get("channel_id") or info.get("uploader_id") or ""
        return cid if _UC_ID.fullmatch(cid) else None
    except Exception as exc:
        logger.info("yt-dlp channel resolve failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# YouTube API Manager
# ---------------------------------------------------------------------------


class YouTubeClient:
    """Manages multiple YouTube API keys with automatic rotation."""

    def __init__(self, api_keys: Optional[List[str]] = None):
        self.api_keys = api_keys or YOUTUBE_API_KEYS
        self._idx = 0
        self._exhausted: set[int] = set()
        self._quota_used: Dict[int, int] = {i: 0 for i in range(len(self.api_keys))}
        self._clients: Dict[int, Any] = {}
        self._channel_cache: Dict[str, dict] = {}
        self._cache_expiry: Dict[str, datetime] = {}
        self._resolved: Dict[str, str] = {}

        for i, key in enumerate(self.api_keys):
            try:
                self._clients[i] = build("youtube", "v3", developerKey=key)
            except Exception as exc:
                logger.error("Failed to init YouTube client for key #%d: %s", i + 1, exc)

    # -- internal helpers ---------------------------------------------------

    def _current_client(self) -> Any:
        """Return a usable client, rotating if the current key is exhausted."""
        if len(self._exhausted) >= len(self.api_keys):
            logger.warning("All API keys exhausted – resetting flags")
            self._exhausted.clear()

        tried = 0
        while tried < len(self.api_keys):
            if self._idx not in self._exhausted and self._idx in self._clients:
                return self._clients[self._idx]
            self._idx = (self._idx + 1) % len(self.api_keys)
            tried += 1

        raise RuntimeError("No usable YouTube API key available")

    def _rotate(self) -> None:
        self._exhausted.add(self._idx)
        logger.warning("Key #%d quota-exhausted, rotating", self._idx + 1)
        self._idx = (self._idx + 1) % len(self.api_keys)

    def _call(self, fn, cost: int, retries: int = 0):
        """Execute *fn(client)* with automatic key rotation on quota errors."""
        max_retries = retries or len(self.api_keys)
        for attempt in range(max_retries):
            client = self._current_client()
            try:
                result = fn(client)
                self._quota_used[self._idx] = self._quota_used.get(self._idx, 0) + cost
                return result
            except HttpError as exc:
                if "quotaExceeded" in str(exc) or "dailyLimitExceeded" in str(exc):
                    self._rotate()
                    if attempt < max_retries - 1:
                        time.sleep(0.5)
                        continue
                raise
        raise RuntimeError("YouTube API call failed after all retries")

    # -- public search ------------------------------------------------------

    def search(self, **kwargs) -> dict:
        return self._call(
            lambda c: c.search().list(**kwargs).execute(),
            YOUTUBE_SEARCH_COST,
        )

    # -- public video helpers -----------------------------------------------

    def get_video_details(self, video_ids: List[str]) -> List[dict]:
        """Fetch full metadata for up to 50 video IDs at a time."""
        all_items: list[dict] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = self._call(
                lambda c, ids=batch: c.videos()
                .list(part="contentDetails,statistics,snippet", id=",".join(ids))
                .execute(),
                YOUTUBE_LIST_COST,
            )
            all_items.extend(resp.get("items", []))
        return all_items

    # -- public channel helpers ---------------------------------------------

    def resolve_channel_id(self, ref: str) -> Optional[str]:
        """Accept a UC… id, @handle, or any channel URL and return the channel id."""
        kind, value = parse_channel_ref(ref)
        if kind == "id":
            return value
        if not value:
            return None
        cached = self._resolved.get(value.lower())
        if cached:
            return cached
        cid: Optional[str] = None
        attempts = [("forUsername", value)] if kind == "user" else [("forHandle", "@" + value)]
        if kind == "name":
            attempts.append(("forUsername", value))
        for param, arg in attempts:
            try:
                resp = self._call(
                    lambda c, p=param, a=arg: c.channels().list(part="id", **{p: a}).execute(),
                    YOUTUBE_LIST_COST,
                )
                items = resp.get("items") or []
                if items:
                    cid = items[0]["id"]
                    break
            except Exception as exc:
                logger.info("channels.list %s=%s failed: %s", param, arg, exc)
        if not cid:
            cid = _resolve_channel_via_ytdlp(ref if "youtube.com" in ref else f"https://www.youtube.com/@{value}")
        if cid:
            self._resolved[value.lower()] = cid
        return cid

    def get_channel_info(self, channel_id: str) -> Optional[dict]:
        """Get channel info with 1-hour TTL cache."""
        now = datetime.now()
        if channel_id in self._channel_cache:
            if self._cache_expiry.get(channel_id, datetime.min) > now:
                return self._channel_cache[channel_id]

        resp = self._call(
            lambda c: c.channels()
            .list(part="snippet,statistics,contentDetails", id=channel_id)
            .execute(),
            YOUTUBE_LIST_COST,
        )
        items = resp.get("items")
        if not items:
            return None
        data = items[0]
        self._channel_cache[channel_id] = data
        self._cache_expiry[channel_id] = now + timedelta(hours=1)
        return data

    def get_channels_batch(self, channel_ids: List[str]) -> Dict[str, dict]:
        """Batch-fetch channel info; returns {channel_id: data}."""
        result: Dict[str, dict] = {}
        to_fetch: list[str] = []
        now = datetime.now()

        for cid in channel_ids:
            if cid in self._channel_cache and self._cache_expiry.get(cid, datetime.min) > now:
                result[cid] = self._channel_cache[cid]
            else:
                to_fetch.append(cid)

        for i in range(0, len(to_fetch), 50):
            batch = to_fetch[i : i + 50]
            resp = self._call(
                lambda c, ids=batch: c.channels()
                .list(part="snippet,statistics,contentDetails", id=",".join(ids))
                .execute(),
                YOUTUBE_LIST_COST,
            )
            for item in resp.get("items", []):
                cid = item["id"]
                self._channel_cache[cid] = item
                self._cache_expiry[cid] = now + timedelta(hours=1)
                result[cid] = item

        return result

    # -- playlist / uploads enumeration -------------------------------------

    def get_playlist_items(
        self,
        playlist_id: str,
        max_results: int = 200,
    ) -> List[dict]:
        """Paginate playlistItems.list. Returns lightweight video dicts."""
        items: List[dict] = []
        page_token: Optional[str] = None

        while len(items) < max_results:
            page_size = min(50, max_results - len(items))

            def _fetch(c, token=page_token, size=page_size, pid=playlist_id):
                kwargs: Dict[str, Any] = {
                    "part": "snippet,contentDetails",
                    "playlistId": pid,
                    "maxResults": size,
                }
                if token:
                    kwargs["pageToken"] = token
                return c.playlistItems().list(**kwargs).execute()

            try:
                resp = self._call(_fetch, YOUTUBE_LIST_COST)
            except Exception as exc:
                logger.error("playlistItems failed for %s: %s", playlist_id, exc)
                break

            for entry in resp.get("items", []):
                snip = entry.get("snippet", {})
                cd = entry.get("contentDetails", {})
                vid = cd.get("videoId") or snip.get("resourceId", {}).get("videoId")
                if not vid:
                    continue
                items.append(
                    {
                        "video_id": vid,
                        "title": snip.get("title", ""),
                        "channel_id": snip.get("channelId", ""),
                        "channel_title": snip.get("channelTitle", ""),
                        "published_at": cd.get("videoPublishedAt") or snip.get("publishedAt", ""),
                        "thumbnail_url": snip.get("thumbnails", {}).get("high", {}).get("url", ""),
                        "description": snip.get("description", ""),
                        "source": "playlist",
                        "playlist_id": playlist_id,
                    }
                )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return items

    def get_uploads_playlist_id(self, channel_id: str) -> Optional[str]:
        """Return the channel's uploads playlist id (UU...)."""
        info = self.get_channel_info(channel_id)
        if not info:
            return None
        return (
            info.get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )

    def get_channel_uploads(
        self,
        channel_id: str,
        max_results: int = 50,
    ) -> List[dict]:
        """Enumerate recent uploads for a channel via its uploads playlist."""
        uploads_id = self.get_uploads_playlist_id(channel_id)
        if not uploads_id:
            logger.warning("No uploads playlist for channel %s", channel_id)
            return []
        videos = self.get_playlist_items(uploads_id, max_results=max_results)
        for v in videos:
            v["source"] = "channel_uploads"
            v["channel_id"] = v.get("channel_id") or channel_id
        return videos

    # -- quota info ---------------------------------------------------------

    @property
    def quota_status(self) -> dict:
        return {
            "keys_total": len(self.api_keys),
            "keys_exhausted": len(self._exhausted),
            "per_key_usage": {
                i: {"used": self._quota_used.get(i, 0), "limit": YOUTUBE_QUOTA_PER_KEY}
                for i in range(len(self.api_keys))
            },
        }
