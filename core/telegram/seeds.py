"""Extract and normalize Telegram seeds from text / YouTube bridges."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set, Union

# Public channel/user: t.me/name or telegram.me/name
_PUBLIC_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][\w\d_]{3,})"
    r"(?:/\d+)?",
    re.IGNORECASE,
)
# Private invite: t.me/+hash or t.me/joinchat/hash
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w\-]+)",
    re.IGNORECASE,
)
# Bare @username mentions that look like channels
_AT_RE = re.compile(r"(?<!\w)@([A-Za-z][\w\d_]{3,})\b")


def _norm_username(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def extract_telegram_seeds(
    text: str,
    source: str = "text",
) -> List[dict]:
    """Parse Telegram links/usernames from free text.

    Returns list of:
      {kind: public|invite, value, url, source}
    """
    if not text:
        return []

    found: List[dict] = []
    seen: Set[str] = set()

    for m in _INVITE_RE.finditer(text):
        token = m.group(1)
        key = f"invite:{token}"
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "kind": "invite",
                "value": token,
                "url": f"https://t.me/+{token}",
                "source": source,
            }
        )

    for m in _PUBLIC_RE.finditer(text):
        name = _norm_username(m.group(1))
        # Skip invite-like path segments already handled
        if name in ("joinchat", "addstickers", "share", "socks", "proxy"):
            continue
        key = f"public:{name}"
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "kind": "public",
                "value": name,
                "url": f"https://t.me/{name}",
                "source": source,
            }
        )

    for m in _AT_RE.finditer(text):
        name = _norm_username(m.group(1))
        key = f"public:{name}"
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "kind": "public",
                "value": name,
                "url": f"https://t.me/{name}",
                "source": source,
            }
        )

    return found


def seeds_from_videos(videos: Iterable[dict]) -> List[dict]:
    """Harvest Telegram seeds from YouTube video descriptions (bridge)."""
    all_seeds: List[dict] = []
    for v in videos:
        desc = v.get("description") or ""
        title = v.get("title") or ""
        vid = v.get("video_id") or ""
        src = f"youtube:{vid}" if vid else "youtube"
        for s in extract_telegram_seeds(desc, source=src):
            s["bridge_video_id"] = vid
            s["bridge_title"] = title
            all_seeds.append(s)
        # Titles rarely hold invites but check anyway
        for s in extract_telegram_seeds(title, source=src + ":title"):
            s["bridge_video_id"] = vid
            s["bridge_title"] = title
            all_seeds.append(s)
    return merge_seed_lists(all_seeds)


def parse_manual_seeds(raw: Optional[Union[str, List[str]]]) -> List[dict]:
    """Accept newline/comma list of @names, t.me links, or invite hashes."""
    if raw is None:
        return []
    if isinstance(raw, list):
        text = "\n".join(str(x) for x in raw)
    else:
        text = str(raw)

    seeds = extract_telegram_seeds(text, source="manual")
    # Also accept bare usernames one-per-line without @
    for line in re.split(r"[\n,]+", text):
        line = line.strip()
        if not line or " " in line:
            continue
        if line.startswith("http") or line.startswith("@") or "t.me" in line.lower():
            continue
        # bare username
        if re.fullmatch(r"[A-Za-z][\w\d_]{3,}", line):
            seeds.append(
                {
                    "kind": "public",
                    "value": _norm_username(line),
                    "url": f"https://t.me/{_norm_username(line)}",
                    "source": "manual",
                }
            )
    return merge_seed_lists(seeds)


def merge_seed_lists(*lists: List[dict]) -> List[dict]:
    """Dedupe seeds by kind+value; keep first occurrence metadata."""
    seen: Set[str] = set()
    out: List[dict] = []
    for lst in lists:
        for s in lst or []:
            key = f"{s.get('kind')}:{_norm_username(str(s.get('value', '')))}"
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
    return out


def seed_to_telethon_ref(seed: dict) -> str:
    """Convert seed to a Telethon-resolvable string."""
    if seed.get("kind") == "invite":
        return seed.get("url") or f"https://t.me/+{seed.get('value')}"
    return seed.get("value") or ""


def official_telegram_usernames(raw: Optional[Union[str, List[str]]]) -> Set[str]:
    """Normalize official Telegram channels into a lowercase username set.

    Accepts the same formats as parse_manual_seeds (@name, t.me/name, bare name).
    Invite hashes are ignored (cannot whitelist private invites by username).
    """
    seeds = parse_manual_seeds(raw)
    out: Set[str] = set()
    for s in seeds:
        if s.get("kind") != "public":
            continue
        name = _norm_username(str(s.get("value") or ""))
        if name:
            out.add(name)
    return out


def is_official_telegram_channel(
    official: Set[str],
    seed: Optional[dict] = None,
    meta: Optional[dict] = None,
) -> bool:
    """True when seed/meta username is in the official whitelist."""
    if not official:
        return False
    candidates = []
    if seed:
        if seed.get("kind") == "public":
            candidates.append(_norm_username(str(seed.get("value") or "")))
        if seed.get("is_official"):
            return True
    if meta:
        candidates.append(_norm_username(str(meta.get("username") or "")))
    return any(c and c in official for c in candidates)
