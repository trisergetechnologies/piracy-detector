"""One-time Telegram login for Phase C.

Usage:
  set TELEGRAM_API_ID=...
  set TELEGRAM_API_HASH=...
  python scripts/telegram_login.py

Creates a local session file used by the scan API (non-interactive after this).
Get API credentials at https://my.telegram.org/apps
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TELEGRAM_API_HASH, TELEGRAM_API_ID, TELEGRAM_SESSION


async def main() -> None:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH env vars first.")
        print("Create an app at https://my.telegram.org/apps")
        sys.exit(1)

    from telethon import TelegramClient

    print(f"Session file: {TELEGRAM_SESSION}")
    client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {getattr(me, 'username', None) or me.id}")
    await client.disconnect()
    print("Done. You can now run Telegram scans via POST /scan/telegram")


if __name__ == "__main__":
    asyncio.run(main())
