"""Telethon client wrapper for Phase C.

Requires TELEGRAM_API_ID + TELEGRAM_API_HASH and a logged-in session
(see scripts/telegram_login.py). Degrades gracefully when unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_CACHE_DIR,
    TELEGRAM_FLOOD_MAX_WAIT,
    TELEGRAM_PARTIAL_DOWNLOAD_MB,
    TELEGRAM_SESSION,
    TELEGRAM_SESSION_STRING,
)

logger = logging.getLogger(__name__)


def telegram_configured() -> bool:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        return False
    if TELEGRAM_SESSION_STRING:
        return True
    # Session file may exist with or without .session suffix depending on Telethon usage
    base = TELEGRAM_SESSION
    return (
        os.path.exists(base)
        or os.path.exists(base + ".session")
        or os.path.exists(str(Path(base).with_suffix(".session")))
    )


def telegram_status() -> dict:
    return {
        "configured": telegram_configured(),
        "api_id_set": bool(TELEGRAM_API_ID),
        "api_hash_set": bool(TELEGRAM_API_HASH),
        "session_string": bool(TELEGRAM_SESSION_STRING),
        "session_path": TELEGRAM_SESSION,
    }


def _build_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # Telethon sleeps through FloodWaits up to this many seconds by itself;
    # longer ones raise FloodWaitError, which the scanner turns into a clean stop.
    opts = {"flood_sleep_threshold": TELEGRAM_FLOOD_MAX_WAIT}
    if TELEGRAM_SESSION_STRING:
        return TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
            **opts,
        )
    Path(TELEGRAM_SESSION).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH, **opts)


async def _ensure_connected(client) -> bool:
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(
            "Telegram session not authorized. Run: python scripts/telegram_login.py"
        )
        return False
    return True


def run_async(coro):
    """Run a coroutine from sync Flask worker threads safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Nested: create a fresh loop in this thread
            return asyncio.new_event_loop().run_until_complete(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


class FloodStop(Exception):
    """Telegram asked us to wait longer than TELEGRAM_FLOOD_MAX_WAIT."""

    def __init__(self, seconds: int, where: str = "") -> None:
        super().__init__(f"FloodWait {seconds}s at {where}")
        self.seconds = int(seconds)
        self.where = where


async def resolve_public(client, seed: dict) -> Tuple[Optional[Any], str]:
    """Resolve a PUBLIC channel/group by username -> (entity | None, reason).

    Public content only: private invite links are never imported and channels
    are never joined. Public channels can be read without membership.
    Raises FloodStop when Telegram demands a long wait.
    """
    from telethon.errors import FloodWaitError

    if seed.get("kind") == "invite":
        return None, "private_invite_not_joined"
    username = seed.get("value")
    if not username:
        return None, "empty_seed"

    for attempt in (1, 2):
        try:
            entity = await client.get_entity(username)
            break
        except FloodWaitError as exc:
            if attempt == 1 and exc.seconds <= TELEGRAM_FLOOD_MAX_WAIT:
                logger.warning("FloodWait %ss resolving %s – sleeping", exc.seconds, username)
                await asyncio.sleep(exc.seconds + 1)
                continue
            raise FloodStop(exc.seconds, f"resolve:{username}")
        except Exception as exc:
            logger.warning("Resolve failed for %s: %s", username, exc)
            return None, f"resolve_failed: {str(exc)[:120]}"

    # Users and bots are not content channels.
    if not (getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)
            or getattr(entity, "gigagroup", False)):
        return None, "not_a_public_channel"
    if not getattr(entity, "username", None) and not getattr(entity, "usernames", None):
        return None, "not_public"
    return entity, ""


async def resolve_and_join(client, seed: dict) -> Optional[Any]:
    """Backwards-compatible name. No longer joins anything (public read only)."""
    entity, _reason = await resolve_public(client, seed)
    return entity


async def search_public_channels(client, query: str, limit: int = 20) -> List[dict]:
    """Telegram's global search for public channels/groups by name. Returns seeds."""
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.contacts import SearchRequest

    try:
        res = await client(SearchRequest(q=query, limit=limit))
    except FloodWaitError as exc:
        if exc.seconds <= TELEGRAM_FLOOD_MAX_WAIT:
            await asyncio.sleep(exc.seconds + 1)
            try:
                res = await client(SearchRequest(q=query, limit=limit))
            except FloodWaitError as exc2:
                raise FloodStop(exc2.seconds, f"search:{query}")
        else:
            raise FloodStop(exc.seconds, f"search:{query}")

    seeds: List[dict] = []
    for chat in getattr(res, "chats", []) or []:
        username = getattr(chat, "username", None)
        if not username:
            continue
        seeds.append({
            "kind": "public",
            "value": username.lower(),
            "url": f"https://t.me/{username}",
            "source": f"search:{query}",
            "title": getattr(chat, "title", "") or "",
        })
    return seeds


async def fetch_channel_meta(client, entity) -> dict:
    """Normalize channel/group metadata."""
    from telethon.tl.types import Channel, Chat

    title = getattr(entity, "title", None) or getattr(entity, "username", "") or ""
    username = getattr(entity, "username", None) or ""
    about = ""
    participants = 0

    try:
        full = await client.get_entity(entity)
        title = getattr(full, "title", title)
        username = getattr(full, "username", username) or username
    except Exception:
        pass

    try:
        # full channel request for about + participants
        if isinstance(entity, Channel) or getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            from telethon.tl.functions.channels import GetFullChannelRequest
            full_ch = await client(GetFullChannelRequest(entity))
            about = getattr(full_ch.full_chat, "about", "") or ""
            participants = int(getattr(full_ch.full_chat, "participants_count", 0) or 0)
        elif isinstance(entity, Chat):
            participants = int(getattr(entity, "participants_count", 0) or 0)
    except Exception as exc:
        logger.debug("Full channel meta failed: %s", exc)

    return {
        "id": getattr(entity, "id", None),
        "title": title,
        "username": username,
        "about": about,
        "participants_count": participants,
        "url": f"https://t.me/{username}" if username else "",
    }


async def iter_recent_messages(client, entity, limit: int = 150) -> List[dict]:
    """Return lightweight message dicts (text + media hints + nested invites)."""
    results: List[dict] = []
    async for msg in client.iter_messages(entity, limit=limit):
        if not msg:
            continue
        text = msg.message or ""
        media_info = None
        if msg.media:
            media_info = _describe_media(msg)
        results.append(
            {
                "message_id": msg.id,
                "date": msg.date.isoformat() if msg.date else "",
                "text": text,
                "media": media_info,
                "views": getattr(msg, "views", None) or 0,
                "forwards": getattr(msg, "forwards", None) or 0,
                "forwarded_from": _forward_source(msg),
            }
        )
    return results


def _forward_source(msg) -> Optional[dict]:
    """Public channel a message was forwarded from (None if not a forward / not public)."""
    try:
        fwd = getattr(msg, "forward", None)
        chat = getattr(fwd, "chat", None) if fwd else None
        username = getattr(chat, "username", None) if chat else None
        if username:
            return {"username": username.lower(), "title": getattr(chat, "title", "") or ""}
    except Exception:
        pass
    return None


def _describe_media(msg) -> Optional[dict]:
    doc = getattr(msg, "document", None)
    video = getattr(msg, "video", None)
    audio = getattr(msg, "audio", None)
    voice = getattr(msg, "voice", None)

    if doc:
        name = ""
        mime = getattr(doc, "mime_type", "") or ""
        size = int(getattr(doc, "size", 0) or 0)
        for attr in getattr(doc, "attributes", []) or []:
            fn = getattr(attr, "file_name", None)
            if fn:
                name = fn
                break
        return {
            "kind": "document",
            "filename": name,
            "mime": mime,
            "size": size,
            "duration": _media_duration(doc),
        }
    if video:
        return {
            "kind": "video",
            "filename": "",
            "mime": "video/mp4",
            "size": int(getattr(video, "size", 0) or 0),
            "duration": int(getattr(video, "duration", 0) or 0),
        }
    if audio or voice:
        src = audio or voice
        return {
            "kind": "audio",
            "filename": getattr(src, "title", "") or "",
            "mime": getattr(src, "mime_type", "audio/mpeg") or "audio/mpeg",
            "size": int(getattr(src, "size", 0) or 0),
            "duration": int(getattr(src, "duration", 0) or 0),
        }
    return {"kind": "other", "filename": "", "mime": "", "size": 0, "duration": 0}


def _media_duration(doc) -> int:
    for attr in getattr(doc, "attributes", []) or []:
        dur = getattr(attr, "duration", None)
        if dur is not None:
            return int(dur)
    return 0


async def download_media_sample(
    client,
    entity,
    message_id: int,
    dest_dir: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Optional[str]:
    """Download only the first part of a message's media (enough for an audio
    sample). Returns file path or None. Raises FloodStop on a long FloodWait."""
    from telethon.errors import FloodWaitError

    if max_bytes is None:
        max_bytes = TELEGRAM_PARTIAL_DOWNLOAD_MB * 1024 * 1024
    dest = Path(dest_dir or TELEGRAM_CACHE_DIR) / "media"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{entity.id}_{message_id}.part"
    try:
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return None
        written = 0
        with open(path, "wb") as fh:
            async for chunk in client.iter_download(msg.media, request_size=512 * 1024):
                fh.write(chunk)
                written += len(chunk)
                if written >= max_bytes:
                    break
        return str(path) if written else None
    except FloodWaitError as exc:
        raise FloodStop(exc.seconds, f"download:{message_id}")
    except Exception as exc:
        logger.warning("Media download failed msg=%s: %s", message_id, exc)
        return None
