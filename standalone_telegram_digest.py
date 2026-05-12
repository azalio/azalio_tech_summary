#!/usr/bin/env python3
"""
Telegram channels collector for azalio_tech_summary.

Pulls latest N posts from configured channels via Telethon (MTProto, user account
— separate from the bot token used to post the digest) and writes them to
{workspace}/memory/telegram_raw.json for collectors.py to consume.

Channels are configured via TELEGRAM_CHANNELS env var (comma-separated, with or
without leading "@" or "https://t.me/" prefix).

Auth: TELEGRAM_API_ID + TELEGRAM_API_HASH always; TELEGRAM_PHONE only for the
one-off `auth-start` step (after that the session file is enough).
Session file: {workspace}/memory/telegram.session.

Subcommands:
  fetch        (default) — pull latest posts from every channel
  auth-start   — send SMS code for first-time login
  auth-complete CODE [--password PW] — confirm code (+ 2FA password if enabled)
  whoami       — verify session and list configured channels
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from telethon import TelegramClient  # type: ignore[import-not-found]
from telethon.errors import (  # type: ignore[import-not-found]
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User as TLUser  # type: ignore[import-not-found]

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

POSTS_PER_CHANNEL = 10
TEXT_TRUNCATE = 1200
MAX_AGE_DAYS = 7

WORKSPACE = Path(os.path.expanduser(os.environ.get("VIBE_WORKSPACE", "./workspace")))
SESSION_FILE = Path(
    os.environ.get("TELEGRAM_SESSION_PATH", str(WORKSPACE / "memory" / "telegram.session"))
)
AUTH_PENDING_FILE = SESSION_FILE.parent / ".telegram-auth-pending.json"
RAW_JSON_PATH = WORKSPACE / "memory" / "telegram_raw.json"


def load_config() -> dict:
    """Phone is optional here — only `auth-start` actually needs it; fetch/whoami
    run off the existing session file.
    """
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    phone = os.environ.get("TELEGRAM_PHONE", "")
    channels = [
        c.strip()
        for c in os.environ.get("TELEGRAM_CHANNELS", "").split(",")
        if c.strip()
    ]
    missing = []
    if not api_id:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if missing:
        sys.exit(
            f"❌ Missing env vars: {missing}\n"
            f"   Get api_id/api_hash from https://my.telegram.org/apps"
        )
    try:
        api_id_int = int(api_id)
    except ValueError:
        sys.exit(f"❌ TELEGRAM_API_ID must be an integer, got: {api_id!r}")
    return {
        "api_id": api_id_int,
        "api_hash": api_hash,
        "phone": phone,
        "channels": channels,
    }


def now_iso_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def total_reactions(reactions) -> int:
    if not reactions or not getattr(reactions, "results", None):
        return 0
    return sum(r.count for r in reactions.results)


def normalize_channel(name: str) -> str:
    """Strip '@' / 't.me/' prefix and any trailing /msg_id; lowercase optional."""
    name = name.strip()
    if name.startswith(("https://t.me/", "http://t.me/")):
        name = name.split("t.me/", 1)[1]
    if name.startswith("@"):
        name = name[1:]
    return name.split("/", 1)[0]


def first_line_title(text: str, max_len: int = 80) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            line = re.sub(r"\s+", " ", line)
            return line[:max_len]
    return ""


def detect_media(msg) -> list[str]:
    """Classify attached media so the LLM knows the post has visuals it can't see.

    Order matters: msg.document is a catch-all for video/gif/audio/file, so the
    specific accessors come first.
    """
    media: list[str] = []
    if msg.photo:
        media.append("photo")
    if msg.video:
        media.append("video")
    elif msg.gif:
        media.append("gif")
    elif msg.voice:
        media.append("voice")
    elif msg.audio:
        media.append("audio")
    elif msg.document:
        media.append("file")
    return media


async def cmd_auth_start(cfg: dict) -> None:
    if not cfg["phone"]:
        sys.exit(
            "❌ TELEGRAM_PHONE is required for auth-start (initial login).\n"
            "   Set it in .env, e.g. TELEGRAM_PHONE=+71234567890"
        )
    print("🔐 Telegram auth (step 1): sending SMS code")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"])
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            assert isinstance(me, TLUser)
            uname = me.username or "no_username"
            print(f"✅ Already authorized as {me.first_name} (@{uname}) id={me.id}")
            print(f"   To switch accounts: delete {SESSION_FILE} and rerun.")
            return
        sent = await client.send_code_request(cfg["phone"])
        AUTH_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUTH_PENDING_FILE.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "phone": cfg["phone"],
                    "phone_code_hash": sent.phone_code_hash,
                    "sent_at": now_iso_local(),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"✅ Code sent to {cfg['phone']}.")
        print("   Check the 'Telegram' chat (id 777000) in your account.")
        print("   Next: python3 standalone_telegram_digest.py auth-complete <CODE>")
        print("   If 2FA enabled: ... auth-complete <CODE> --password <PW>")
    finally:
        await client.disconnect()  # type: ignore[misc]


async def cmd_auth_complete(cfg: dict, code: str, password: str | None) -> None:
    if not AUTH_PENDING_FILE.exists():
        sys.exit("❌ No pending auth — run `auth-start` first.")
    with AUTH_PENDING_FILE.open(encoding="utf-8") as f:
        pending = json.load(f)
    print("🔐 Telegram auth (step 2): confirming code")
    client = TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"])
    await client.connect()
    try:
        try:
            await client.sign_in(  # type: ignore[misc]
                phone=pending["phone"],
                code=code,
                phone_code_hash=pending["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            if not password:
                sys.exit(
                    "❌ 2FA enabled. Re-run: auth-complete <CODE> --password <PW>"
                )
            await client.sign_in(password=password)  # type: ignore[misc]
        except PhoneCodeInvalidError:
            sys.exit("❌ Code invalid. Re-run auth-complete with the correct code.")
        except PhoneCodeExpiredError:
            AUTH_PENDING_FILE.unlink(missing_ok=True)
            sys.exit("❌ Code expired. Run auth-start again.")
        me = await client.get_me()
        assert isinstance(me, TLUser)
        AUTH_PENDING_FILE.unlink(missing_ok=True)
        uname = me.username or "no_username"
        print(f"✅ Authorized as {me.first_name} (@{uname}) id={me.id}")
        print(f"   Session saved: {SESSION_FILE}")
    finally:
        await client.disconnect()  # type: ignore[misc]


async def cmd_whoami(cfg: dict) -> None:
    async with TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"]) as client:
        if not await client.is_user_authorized():
            sys.exit("❌ No session — run `auth-start` + `auth-complete` first.")
        me = await client.get_me()
        assert isinstance(me, TLUser)
        print(f"✅ User: {me.first_name} id={me.id}")
        print(f"   Channels configured ({len(cfg['channels'])}):")
        for ch in cfg["channels"]:
            try:
                entity = await client.get_entity(normalize_channel(ch))
                title = getattr(entity, "title", None) or getattr(entity, "first_name", "?")
                username = getattr(entity, "username", None)
                display = f"@{username}" if username else "(no public @username)"
                print(f"    • {title} — {display}")
            except Exception as e:
                print(f"    • {ch}: ❌ {e}")


async def cmd_fetch(cfg: dict) -> None:
    RAW_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Truncate immediately so a crash mid-fetch leaves an empty file rather than
    # stale data from the previous run that collect_telegram() would re-ingest.
    with RAW_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump([], f)

    if not cfg["channels"]:
        print("⚠️ TELEGRAM_CHANNELS is empty — nothing to fetch.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    # Cap to bound API usage on channels heavy on media-only posts; the
    # fetched-counter and age cutoff are the real stop conditions.
    iter_cap = POSTS_PER_CHANNEL * 5

    async with TelegramClient(str(SESSION_FILE), cfg["api_id"], cfg["api_hash"]) as client:
        if not await client.is_user_authorized():
            sys.exit("❌ No Telegram session. Run `auth-start` + `auth-complete`.")

        raw_data: list[dict] = []
        for ch in cfg["channels"]:
            ch_norm = normalize_channel(ch)
            try:
                entity = await client.get_entity(ch_norm)
            except Exception as e:
                print(f"  ❌ skip {ch}: {e}")
                continue
            ch_title = getattr(entity, "title", None) or ch_norm
            print(f"  Fetching @{ch_norm} ({ch_title})...")
            fetched = 0
            try:
                async for msg in client.iter_messages(entity, limit=iter_cap):
                    if fetched >= POSTS_PER_CHANNEL:
                        break
                    # iter_messages is newest-first; older than cutoff → stop
                    if msg.date and msg.date < cutoff:
                        break
                    text = (msg.message or "").strip()
                    if not text:
                        continue  # skip media-only / service messages
                    raw_data.append(
                        {
                            "channel": ch_norm,
                            "channel_title": ch_title,
                            "msg_id": msg.id,
                            "url": f"https://t.me/{ch_norm}/{msg.id}",
                            "title": first_line_title(text, max_len=80),
                            "text": text[:TEXT_TRUNCATE],
                            "date": msg.date.isoformat() if msg.date else "",
                            "views": msg.views or 0,
                            "forwards": msg.forwards or 0,
                            "reactions": total_reactions(msg.reactions),
                            "media": detect_media(msg),
                        }
                    )
                    fetched += 1
            except FloodWaitError as e:
                print(f"  ⏳ FloodWait on @{ch_norm}: {e.seconds}s, skipping channel")
                await asyncio.sleep(min(e.seconds, 60))
                continue
            except Exception as e:
                print(f"  ❌ error fetching @{ch_norm}: {e}")
                continue
            print(f"    +{fetched} text posts")

        with RAW_JSON_PATH.open("w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, indent=2)
        print(f"✅ {len(raw_data)} posts → {RAW_JSON_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram channels collector")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("fetch", help="(default) pull latest posts from channels")
    sub.add_parser("auth-start", help="step 1: send SMS code")
    p_complete = sub.add_parser("auth-complete", help="step 2: confirm SMS code")
    p_complete.add_argument("code", type=str, help="5-digit code from the 'Telegram' chat")
    p_complete.add_argument("--password", type=str, default=None, help="2FA password if enabled")
    sub.add_parser("whoami", help="verify session and list channels")

    args = parser.parse_args()
    cfg = load_config()
    cmd = args.cmd or "fetch"

    if cmd == "fetch":
        asyncio.run(cmd_fetch(cfg))
    elif cmd == "auth-start":
        asyncio.run(cmd_auth_start(cfg))
    elif cmd == "auth-complete":
        asyncio.run(cmd_auth_complete(cfg, args.code, args.password))
    elif cmd == "whoami":
        asyncio.run(cmd_whoami(cfg))


if __name__ == "__main__":
    main()
