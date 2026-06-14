#!/usr/bin/env python3
"""X/Twitter collector for azalio_tech_summary — free/low-cost acquisition cascade.

Thin CLI over :mod:`x_acquire`. Two output modes plus a self-check, matching the
``standalone_*_digest.py`` convention (subprocess that writes JSON for
``collectors.py`` to consume):

  (default)  fetch  → write recent posts to {workspace}/memory/x_raw.json. Does
             NOT apply its own seen-dedup; collectors.py owns digest dedup (URL +
             semantic), exactly like the Telegram/Reddit collectors.
  --out F    JSONL pipeline → emit only NEW normalized items (one per line) to F,
             recording them in the cascade's own seen_items table. This is the
             standalone, digest-independent export.
  --check    diagnostics: sources loaded, providers enabled, mirror/RSSHub/Nitter
             reachability, SQLite writable, output schema valid. Secrets redacted.

Sources come from a YAML/JSON file (``--sources`` / ``X_SOURCES`` /
``x_sources.yaml``) or the ``X_HANDLES`` env fallback. Provider config + secrets
come from the environment only — never the command line or chat. See
docs/x_acquisition.md.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import x_acquire

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

WORKSPACE = Path(os.path.expanduser(os.environ.get("VIBE_WORKSPACE", "./workspace")))
RAW_JSON_PATH = WORKSPACE / "memory" / "x_raw.json"
STATE_DB_PATH = os.environ.get("X_STATE_DB", str(WORKSPACE / "memory" / "x_state.db"))
DEFAULT_SINCE = os.environ.get("X_SINCE", "24h")
DEFAULT_MAX_PER_SOURCE = int(os.environ.get("X_MAX_PER_SOURCE", "8") or "8")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_fetch(args):
    """Default mode: pull recent posts and hand them to the digest via x_raw.json."""
    raw_path = Path(args.raw_json)
    # Truncate up front so a crash mid-fetch leaves an empty file rather than
    # stale data the digest would re-ingest (same guard as the Telegram fetcher).
    _write_json(raw_path, [])

    sources = x_acquire.load_sources(args.sources)
    if not sources:
        print("⚠️  No X sources configured (no sources file, no X_HANDLES) — nothing to do.")
        return 0

    since_dt = None
    delta = x_acquire.parse_since(args.since)
    if delta:
        since_dt = datetime.now(timezone.utc) - delta

    state = x_acquire.XState(STATE_DB_PATH)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        items, errors = x_acquire.acquire(
            sources, since_dt=since_dt, per_source_cap=args.max_per_source,
            state=state,
        )
        if args.out:
            # Standalone JSONL pipeline: only genuinely new items, recorded as seen.
            new_items = state.filter_new(items)
            state.mark_seen(new_items)
            _write_jsonl(args.out, new_items)
            print(f"✅ {len(new_items)} new item(s) → {args.out} "
                  f"({len(items) - len(new_items)} already seen, {errors} provider error(s))")
        else:
            # Digest mode: hand every in-window item to collectors.py (it dedups).
            _write_json(raw_path, items)
            print(f"✅ {len(items)} item(s) → {raw_path} ({errors} provider error(s))")
        state.record_run(started, len(items), errors)
        _print_provider_summary(items, sources)
    finally:
        state.close()
    return 0


def _write_jsonl(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _print_provider_summary(items, sources):
    """Operational note: which providers actually served, what degraded."""
    by_provider = {}
    served_sources = set()
    for it in items:
        by_provider[it["provider"]] = by_provider.get(it["provider"], 0) + 1
        served_sources.add(it["source"])
    if by_provider:
        parts = ", ".join(f"{p}={n}" for p, n in sorted(by_provider.items()))
        print(f"   providers: {parts}")
    degraded = [s.id for s in sources if s.id not in served_sources]
    if degraded:
        print(f"   degraded (no items): {', '.join(degraded)}")


def cmd_check(args):
    """Diagnostics. Exit 0 if the pipeline can run (sources + SQLite OK), else 1."""
    print("X acquisition self-check")
    print("=" * 40)
    hard_ok = True

    # .env presence (informational — env may come from the parent process).
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    print(f"[i] .env file: {'present' if os.path.exists(env_path) else 'absent (using process env)'}")

    # Sources.
    sources = x_acquire.load_sources(args.sources)
    if sources:
        print(f"[ok] sources: {len(sources)} loaded")
        for s in sources:
            extras = []
            if s.mirrors:
                extras.append(f"{len(s.mirrors)} mirror(s)")
            if s.bluesky or s.bluesky_list:
                extras.append("bluesky")
            tag = f" ({', '.join(extras)})" if extras else ""
            print(f"       • {s.id} [{s.kind}] {('@'+s.handle) if s.handle else ''}{tag}")
    else:
        print("[FAIL] sources: none configured (no file, no X_HANDLES)")
        hard_ok = False

    # Providers enabled for the current config.
    providers = x_acquire.build_providers(os.environ)
    enabled = [p.name for p in providers if any(p.supports(s) for s in sources)]
    disabled = [p.name for p in providers if p.name not in enabled]
    print(f"[i] providers enabled for these sources: {enabled or '(none)'}")
    if disabled:
        print(f"[i] providers idle (not configured / unsupported): {disabled}")

    # Reachability probes (soft — failures warn, don't fail the check).
    _probe_rss(sources)
    _probe_bluesky(sources)
    _probe_rsshub()
    _probe_nitter()

    # Browser state file, if the browser provider is configured.
    state_path = os.environ.get("X_BROWSER_STATE", "")
    if state_path:
        ok = os.path.exists(state_path)
        print(f"[{'ok' if ok else 'WARN'}] browser storage state: "
              f"{'found' if ok else 'configured but missing'} {x_acquire.redact(state_path)}")

    # SQLite writable.
    try:
        st = x_acquire.XState(STATE_DB_PATH)
        st.record_run(datetime.now(timezone.utc).isoformat(timespec="seconds"), 0, 0)
        st.close()
        print(f"[ok] SQLite state writable: {STATE_DB_PATH}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] SQLite state: {x_acquire.redact(e)}")
        hard_ok = False

    # Output schema self-test.
    sample = x_acquire.normalize_item(
        url="https://x.com/test/status/1", author="@test", text="hello",
        source="selfcheck", provider="rss",
    )
    problems = x_acquire.validate_item(sample)
    if problems:
        print(f"[FAIL] output schema: {problems}")
        hard_ok = False
    else:
        print("[ok] output schema valid")

    print("=" * 40)
    print("RESULT:", "OK — pipeline can run" if hard_ok else "FAIL — see above")
    return 0 if hard_ok else 1


def _probe_rss(sources):
    url = next((m["url"] for s in sources for m in s.mirrors if m.get("url")), "")
    if not url and sources and sources[0].kind == "rss":
        url = sources[0].url
    if not url:
        print("[i] rss: no mirror feeds configured to probe")
        return
    try:
        x_acquire._http_get(url, timeout=10)
        print(f"[ok] rss mirror reachable: {url}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] rss mirror probe failed ({url}): {x_acquire.redact(e)}")


def _probe_bluesky(sources):
    if not any(s.bluesky or s.bluesky_list or s.kind == "bluesky" for s in sources):
        print("[i] bluesky: no sources mapped to Bluesky")
        return
    try:
        x_acquire._http_get_json(
            f"{x_acquire.BlueskyProvider.API}/com.atproto.identity.resolveHandle"
            "?handle=bsky.app", timeout=10)
        print("[ok] bluesky public API reachable")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] bluesky public API probe failed: {x_acquire.redact(e)}")


def _probe_rsshub():
    base = os.environ.get("X_RSSHUB_URL", "")
    if not base:
        print("[i] rsshub: not configured (X_RSSHUB_URL unset)")
        return
    try:
        x_acquire._http_get(base.rstrip("/") + "/", timeout=10)
        print(f"[ok] rsshub reachable: {x_acquire.redact(base)}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] rsshub probe failed: {x_acquire.redact(e)}")


def _probe_nitter():
    instances = os.environ.get("X_NITTER_INSTANCES", "")
    if not instances:
        print("[i] nitter: not configured (X_NITTER_INSTANCES unset)")
        return
    inst = instances.split(",")[0].strip().rstrip("/")
    try:
        x_acquire._http_get(inst + "/", timeout=10)
        print(f"[ok] nitter instance reachable: {inst}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] nitter probe failed ({inst}): {x_acquire.redact(e)}")


def main():
    parser = argparse.ArgumentParser(description="X/Twitter free acquisition cascade")
    parser.add_argument("--sources", default=None,
                        help="path to sources.yaml/.json (default: X_SOURCES or x_sources.yaml)")
    parser.add_argument("--out", default=None,
                        help="write NEW items as JSONL to this path (standalone export mode)")
    parser.add_argument("--raw-json", default=str(RAW_JSON_PATH),
                        help="digest-mode output path (default: workspace/memory/x_raw.json)")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="only items newer than this (30m/6h/2d/1w; default 24h)")
    parser.add_argument("--max-per-source", type=int, default=DEFAULT_MAX_PER_SOURCE,
                        help="cap items kept per source (default 8)")
    parser.add_argument("--check", action="store_true",
                        help="run diagnostics and exit")
    args = parser.parse_args()

    try:
        if args.check:
            sys.exit(cmd_check(args))
        sys.exit(cmd_fetch(args))
    except ValueError as e:  # bad --since etc.
        sys.exit(f"❌ {x_acquire.redact(e)}")


if __name__ == "__main__":
    main()
