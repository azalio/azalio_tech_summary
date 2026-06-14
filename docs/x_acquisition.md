# X / Twitter free acquisition cascade

Reads X/Twitter sources into the digest **without** the paid X API. X regularly
breaks every unofficial path, so this never bets on one scraper: each source is
read through an ordered **cascade** of providers and the *first one that yields
items wins*. Failures trip a per-(provider, source) **circuit breaker**, and a
source no provider can serve simply degrades — it never crashes the digest.

> Don't read X at any cost. Build a fault-tolerant acquisition layer where X is
> one channel, not the only mast on the ship.

Files: `x_acquire.py` (library), `standalone_x_digest.py` (CLI / subprocess
fetcher), `collectors.py::collect_x` (digest integration), `x_sources.example.yaml`.

## The cascade

Default order (override with `X_PROVIDER_ORDER`):

| # | provider | what it reads | needs | durability |
|---|----------|---------------|-------|-----------|
| L0 | `rss` | official blog / GitHub-releases / Substack / YouTube / Mastodon RSS mirrors of the account | a `mirrors:` URL or `kind: rss` | ★★★★★ |
| L0.5 | `bluesky` | AT-Protocol public API (`getAuthorFeed` / `getListFeed`) | a `bluesky:` handle/DID per source | ★★★★★ |
| L1 | `rsshub` | self-hosted RSSHub `/twitter/user/:h`, `/twitter/list/:id` | `X_RSSHUB_URL` (+ token on the RSSHub host) | ★★★☆☆ |
| L2 | `nitter` | public Nitter / xcancel `/<h>/rss` | `X_NITTER_INSTANCES` | ★★☆☆☆ |
| L3 | `twscrape` | unofficial session scraper | `X_TWSCRAPE_ENABLED` + accounts | ★★☆☆☆ |
| L4 | `browser` | logged-in Playwright DOM scrape | `X_BROWSER_STATE` | ★☆☆☆☆ |
| L5 | `email` | X notification emails over IMAP | `X_EMAIL_*` | ★★☆☆☆ |

**Strategy that survives X's crackdowns:** give every handle a non-X identity
(official RSS, GitHub releases, YouTube, Substack, Mastodon, and especially a
Bluesky account) and let the durable L0/L0.5 layers serve it. X-native scraping
(L1–L4) is the fallback for accounts with no free mirror. This was the unanimous
recommendation of an LLM-council review: *stop treating X as the source of truth;
map each handle to its cross-platform identities.*

L0–L0.5 are fully tested against live endpoints. **L3 (twscrape), L4 (browser),
L5 (email) are opt-in and untested in CI** — they stay `supports()==False` until
their env vars are set, and fail closed (a runtime error trips the breaker and
the cascade falls through), so they can never break a run.

## Quick start (free, no X account)

```bash
cp x_sources.example.yaml x_sources.yaml   # edit: add handles + mirrors/bluesky
python3 standalone_x_digest.py --check     # verify config + reachability
python3 standalone_x_digest.py --since 7d  # fetch → workspace/memory/x_raw.json
```

That's it — `main.py`'s `collect_x()` picks up `x_raw.json` on the next hourly
run and the posts flow into the digest (deduped + ranked like any other source).
With only L0/L0.5 you already cover any account that mirrors to RSS or Bluesky.

## Adding sources

Edit `x_sources.yaml` (never code). One entry per account:

```yaml
sources:
  - id: science_news_x          # unique, stable (used in dedup/state)
    kind: x_user                # x_user | x_list | rss | bluesky
    handle: NewsfromScience     # X handle, no @
    priority: 9                 # higher = fetched & ranked first
    bluesky: science.org        # explicit bsky handle/DID — x→bsky is NOT 1:1
    mirrors:
      - kind: rss
        url: https://www.science.org/rss/news_current.xml
```

No-YAML fallback for a quick test: `export X_HANDLES=OpenAI,garrytan` (handles
only, no mirrors → they degrade unless an X-native provider is configured).

**Find a handle's Bluesky DID** (proves the account is real *and active* — a
handle can resolve to a parked, empty account):

```bash
curl -s "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=<handle>&limit=3" | jq '.feed[].post.record.createdAt'
```

## L1 — self-hosted RSSHub (the strongest X-native path)

RSSHub hits X's GraphQL backend with a cookie you supply *on the RSSHub host*.
The token never touches this repo.

```bash
# minimal self-host (Docker); TWITTER_AUTH_TOKEN is your x.com auth_token cookie
docker run -d --name rsshub -p 1200:1200 \
  -e TWITTER_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxx \
  diygod/rsshub
# then point the digest at it (auth token stays on the RSSHub box):
echo 'X_RSSHUB_URL=http://localhost:1200' >> .env
```

Use a **dedicated, read-only** X account's cookie. The token lasts ~60–180 days;
watch RSSHub's GitHub releases for GraphQL-hash fixes. Tip: run **RSS-Bridge**
in parallel on another port for codebase diversity.

## CLI

```bash
standalone_x_digest.py                       # fetch → x_raw.json (digest mode)
standalone_x_digest.py --since 24h           # only items newer than 24h (30m/2d/1w ok)
standalone_x_digest.py --out out/items.jsonl # standalone JSONL export (seen-deduped)
standalone_x_digest.py --check               # diagnostics; exit 0 if runnable
standalone_x_digest.py --sources path.yaml   # explicit sources file
```

Digest mode (`--raw-json`, default) writes every in-window item and lets
`collectors.py` own dedup. JSONL mode (`--out`) emits only items not in the
cascade's own `seen_items` table and marks them seen — a self-contained pipeline.

## Normalized output schema

One JSON object per item (`raw` never contains secrets):

```json
{
  "id": "url:https://x.com/OpenAI/status/123",
  "url": "https://x.com/OpenAI/status/123",
  "author": "@OpenAI",
  "text": "post text",
  "title": "short title",
  "published": "2026-06-13T10:00:00+00:00",
  "source": "openai_x",
  "provider": "bluesky",
  "engagement": 1234,
  "fetched_at": "2026-06-13T10:05:00+00:00",
  "raw": {"provider_id": "at://did:plc:.../app.bsky.feed.post/3k"}
}
```

Dedup id is route-independent: the same tweet via x.com, Nitter, and RSSHub all
collapse to `url:https://x.com/<user>/status/<id>`.

## State (SQLite, `workspace/memory/x_state.db`)

- `seen_items` — dedup keys (JSONL export mode).
- `provider_state` — per-(provider, source) `failure_count` / `last_error`
  (redacted) / `cooldown_until` (the circuit breaker).
- `fetch_runs` — one row per run (`items_count`, `errors_count`).

Circuit breaker: after `BREAKER_THRESHOLD=3` consecutive failures a
(provider, source) pair is skipped until an exponentially-backed-off cooldown
(30m → 60m → … → 6h cap) expires; one success clears it.

## Security

- Secrets (IMAP password, RSSHub access key, auth tokens) live **only** in the
  environment / `.env` (gitignored). They are scrubbed from every log line and
  stored error by `redact()`, and never written into an item's `raw`.
- The pipeline never asks for, prints, or commits a cookie/token.
- `x_sources.yaml` (your real list) is gitignored; only the `.example` is tracked.
- Session-scraper hygiene (if you enable L3/L4) per the council review:
  read-only accounts (never follow/like/post), one account ↔ one stable IP,
  residential/mobile proxies not datacenter, jittered timing, and treat a
  captcha/login-challenge as a **STOP** signal, not something to bypass.

## Limitations

- **Empty Bluesky/Nitter ≠ broken.** A reachable provider that returns 0 items
  in the window is treated as "no content here" and the cascade falls through to
  the next layer (so a parked Bluesky handle correctly degrades to X-native).
- A handle resolving on Bluesky does **not** mean it's active — verify with the
  `getAuthorFeed` curl above before relying on it.
- Public Nitter instances are unreliable by nature; keep a health-checked list.
- L3/L4/L5 are experimental and need out-of-band setup (account / browser state /
  mailbox); they're insurance behind the breaker, not the foundation.
- The official X API is the only *complete* feed; for a genuinely X-only account
  with no mirror you depend on L1+ and accept partial coverage.
```
