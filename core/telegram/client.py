"""Telethon client wrapper for Phase C.

Requires TELEGRAM_API_ID + TELEGRAM_API_HASH and a logged-in session
(see scripts/telegram_login.py). Degrades gracefully when unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_CACHE_DIR,
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

    if TELEGRAM_SESSION_STRING:
        return TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )
    Path(TELEGRAM_SESSION).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)


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


async def resolve_and_join(client, seed: dict) -> Optional[Any]:
    """Resolve a public username or try to join an invite. Returns entity or None."""
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.errors import (
        ChannelPrivateError,
        InviteHashExpiredError,
        UserAlreadyParticipantError,
        FloodWaitError,
    )

    try:
        if seed.get("kind") == "invite":
            token = seed.get("value")
            try:
                await client(ImportChatInviteRequest(token))
            except UserAlreadyParticipantError:
                pass
            except InviteHashExpiredError:
                logger.warning("Invite expired: %s", token)
                return None
            # Re-check invite to get chat
            try:
                updates = await client(CheckChatInviteRequest(token))
                chat = getattr(updates, "chat", None)
                if chat:
                    return chat
            except Exception:
                pass
            return None

        username = seed.get("value")
        entity = await client.get_entity(username)
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        except ChannelPrivateError:
            logger.warning("Channel private / cannot join: %s", username)
            # May still read if already a member
        return entity
    except FloodWaitError as exc:
        logger.warning("Flood wait %ss for seed %s", exc.seconds, seed.get("value"))
        return None
    except Exception as exc:
        logger.warning("Resolve failed for %s: %s", seed.get("value"), exc)
        return None


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
            }
        )
    return results


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
) -> Optional[str]:
    """Download a message's media to cache. Returns file path or None."""
    dest = Path(dest_dir or TELEGRAM_CACHE_DIR) / "media"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return None
        path = await client.download_media(msg, file=str(dest / f"{entity.id}_{message_id}"))
        return str(path) if path else None
    except Exception as exc:
        logger.warning("Media download failed msg=%s: %s", message_id, exc)
        return None
