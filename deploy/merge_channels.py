#!/usr/bin/env python3
"""Idempotently merge Telegram channels into TELEGRAM_CHANNELS in a .env file.

Adds only channels whose normalized (lowercase, @/t.me-stripped) form isn't
already present, preserving the rest of the file. Accepts channels as @name,
bare name, or full https://t.me/name URLs.

Usage: merge_channels.py <env_path> <channel> [<channel> ...]
"""
import sys


def clean(name):
    """Strip @/t.me prefix and any trailing /msg_id; preserve case for display."""
    name = name.strip()
    for p in ("https://t.me/", "http://t.me/"):
        if name.startswith(p):
            name = name.split("t.me/", 1)[1]
    if name.startswith("@"):
        name = name[1:]
    return name.split("/", 1)[0]


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: merge_channels.py <env_path> <channel> [<channel> ...]")
    env_path = sys.argv[1]
    candidates = [c for c in sys.argv[2:] if c.strip()]

    with open(env_path, encoding="utf-8") as f:
        lines = f.readlines()

    key = "TELEGRAM_CHANNELS="
    for i, line in enumerate(lines):
        body = line.lstrip()
        if not (body.startswith(key) or body.startswith("export " + key)):
            continue
        prefix, _, value = line.partition("=")
        value = value.rstrip("\n")
        quote = ""
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            quote, value = value[0], value[1:-1]
        existing = [c for c in value.split(",") if c.strip()]
        seen = {clean(c).lower() for c in existing}
        added = []
        for cand in candidates:
            disp = clean(cand)
            if disp.lower() not in seen:
                existing.append("@" + disp)
                seen.add(disp.lower())
                added.append(disp)
        lines[i] = f"{prefix}={quote}{','.join(existing)}{quote}\n"
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"added {len(added)}: {', '.join(added) if added else '(none)'}")
        print(f"total channels now: {len(existing)}")
        return
    sys.exit("TELEGRAM_CHANNELS line not found in " + env_path)


if __name__ == "__main__":
    main()
