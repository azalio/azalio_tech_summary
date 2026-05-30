#!/usr/bin/env python3
"""Verify one or more Telegram channels are resolvable & readable by the bot's
user session — the same session standalone_telegram_digest.py fetches with.

Run from the project dir so the default workspace/session paths resolve (or set
VIBE_WORKSPACE / TELEGRAM_SESSION_PATH). Reads api_id/api_hash from .env.

Usage: check_channel.py <channel> [<channel> ...]
Exit code is non-zero if any channel failed to resolve.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

# Load .env from the dir we're run in (project root) first, then fall back to a
# project-root-relative path; works whether run in place or scp'd to /tmp.
load_dotenv(os.path.join(os.getcwd(), ".env"))
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

WORKSPACE = Path(os.path.expanduser(os.environ.get("VIBE_WORKSPACE", "./workspace")))
SESSION_FILE = Path(
    os.environ.get("TELEGRAM_SESSION_PATH", str(WORKSPACE / "memory" / "telegram.session"))
)


def norm(name):
    name = name.strip()
    for p in ("https://t.me/", "http://t.me/"):
        if name.startswith(p):
            name = name.split("t.me/", 1)[1]
    if name.startswith("@"):
        name = name[1:]
    return name.split("/", 1)[0].lower()


async def main(channels):
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    failed = 0
    async with TelegramClient(str(SESSION_FILE), api_id, api_hash) as client:
        if not await client.is_user_authorized():
            sys.exit("❌ No authorized session — run auth-start/auth-complete first.")
        for ch in channels:
            try:
                ent = await client.get_entity(norm(ch))
                title = getattr(ent, "title", None) or getattr(ent, "first_name", "?")
                username = getattr(ent, "username", None)
                # Pull the most recent message to confirm read access, not just resolve.
                latest = await client.get_messages(ent, limit=1)
                if latest:
                    m = latest[0]
                    age = datetime.now(timezone.utc) - m.date
                    days = age.total_seconds() / 86400
                    preview = " ".join((m.message or "[media/no text]").split())[:70]
                    print(f"✅ {ch} -> «{title}» @{username} | last post {days:.1f}d ago: {preview}")
                else:
                    print(f"⚠️ {ch} -> «{title}» @{username} | resolves but no readable messages")
            except Exception as e:
                failed += 1
                print(f"❌ {ch}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(failed)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: check_channel.py <channel> [<channel> ...]")
    asyncio.run(main(sys.argv[1:]))
