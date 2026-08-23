# azalio_tech_summary

Hourly tech-news digest bot. Pulls headlines from ~15 sources, deduplicates them with embeddings, asks an LLM to write a structured Russian-language digest, and posts to a Telegram channel.

Channel: [@azalio_tech_summary](https://t.me/azalio_tech_summary).

## What it does

Every hour the bot:

1. **Collects** ~150-300 headlines in parallel from RSS, REST APIs, and HTML pages.
2. **Deduplicates** them in two layers — exact URL match (SQLite, 30-day TTL) and semantic clustering on multilingual sentence embeddings (`intfloat/multilingual-e5-small`).
3. **Summarises** what's left with an LLM, trying Codex CLI → Antigravity CLI (`agy`) → Ollama Cloud, under a strict editorial prompt: DevOps/SRE → AI/ML → Security → Science → Politics, no preamble, no editorial commentary.
4. **Posts** the digest to a Telegram channel, splitting by section if it overflows the 4096-char limit.

Output goes to ~30 buckets per day, ~5-15 bullets per digest after dedup.

## Sources

- **Reddit** — ~30 tech subreddits via Reddit API (programming, kubernetes, MachineLearning, ClaudeAI, ...).
- **Telegram channels** — last 10 text posts per channel via Telethon/MTProto (optional, channel list in `TELEGRAM_CHANNELS`).
- **X / Twitter** — free/low-cost acquisition cascade, no paid X API (optional, handles in `x_sources.yaml`): RSS/blog mirrors → Bluesky public API → self-hosted RSSHub → Nitter → twscrape → browser → email, first-success per source with circuit breakers. See [docs/x_acquisition.md](docs/x_acquisition.md).
- **HackerNews** — front page via Algolia.
- **Tech press (RSS)** — TechCrunch, Ars Technica, The Verge, Wired, MIT Tech Review, IEEE Spectrum, The Register.
- **AI research** — HuggingFace Daily Papers (upvotes ≥ 100), ArXiv RSS (cs.AI, cs.LG, cs.CL).
- **Infra/DevOps** — Kubernetes, CNCF, AWS, Cloudflare, HashiCorp, Datadog, Grafana.
- **Science/Space** — NASA, Nature, ScienceDaily, SpaceNews, ESO, ESA, Chandra X-ray, Phys.org.
- **Global news** — BBC, Al Jazeera, DW.
- **Google News** — search-based RSS.
- **NewsAPI** — AI / DevOps categories (optional, needs API key).
- **Finnhub** — general financial news (optional, needs API key).
- **Habr** — top daily articles (score ≥ 100).
- **Claude Platform release notes** — direct `.md` fetch from `platform.claude.com`.
- **GitHub Trending** — top 5 daily by stars-today (≥ 200), with first ~400 chars of README via `raw.githubusercontent.com`.

## Architecture

```
collectors.py ──► dedup.py ──► main.py (LLM call) ──► core.py (Telegram)
   RSS/API           E5 model      VIBE_PROMPT          HTML format
   ~15 sources       SQLite        codex→agy→ollama     auto-split
```

State lives in `${VIBE_WORKSPACE}/memory/`:
- `events.db` — semantic dedup clusters (centroid + tokens)
- `reddit_sent.db` (a.k.a. `sent_posts`) — URL dedup
- `last_intel_summary.txt` — previous digest, fed back into the next prompt

## Deduplication

Two layers, both must pass:

1. **URL** — normalised (https, lowercase host, sorted params, no UTM) match in `sent_posts`. 30-day TTL.
2. **Semantic** — E5 embedding of the title compared to existing event clusters. Tiered gate:
   - cosine ≥ **0.90** → duplicate (no further check)
   - cosine ≥ 0.80 AND Jaccard token overlap ≥ 0.15 → duplicate
   - else → new event, becomes a fresh cluster

Cluster centroids are **frozen** to the first item's embedding — averaging across additions causes drift over time. Matching window is 48h, storage TTL 7 days.

## Setup

### Prerequisites

- **Python 3.10+** with `python3-venv`
- **An LLM provider** — at least one of:
  - [Codex CLI](https://github.com/openai/codex) — `brew install --cask codex`. Authenticate via `codex login`. Auth lives in `~/.codex/auth.json`.
  - [Antigravity CLI](https://antigravity.google/) — install `agy` and authenticate once interactively.
  - [Ollama Cloud](https://ollama.com/) — set `OLLAMA_API_KEY`.
  - When several are configured, the fixed order is Codex → Antigravity → Ollama Cloud.
- **A Telegram bot** — create one via [@BotFather](https://t.me/BotFather), copy the token, and either invite the bot to your channel as admin or send it `/start` from your account.

### Install

```bash
git clone https://github.com/azalio/azalio_tech_summary.git
cd azalio_tech_summary
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp env.example .env
# Edit .env — at minimum set TELEGRAM_BOT_TOKEN, TELEGRAM_DEFAULT_CHAT_ID,
# and TELEGRAM_DIGEST_CHAT (defaults to @azalio_tech_summary which you don't own).
```

> A virtualenv is strongly recommended on Ubuntu 24.04+ — system pip is locked down by [PEP 668](https://peps.python.org/pep-0668/). Without a venv you'd need `--break-system-packages`.

### Required env vars

| Var | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_DEFAULT_CHAT_ID` | Fallback chat (your user id from [@userinfobot](https://t.me/userinfobot)) |
| `TELEGRAM_DIGEST_CHAT` | Channel for the hourly digest. **Override the default** — `@azalio_tech_summary` belongs to the author. |

Optional (collectors/providers silently skip when unset): `FINNHUB_API_KEY`, `NEWSAPI_KEY`, `CODEX_BIN`, `AGY_BIN`, `OLLAMA_API_KEY`, `OLLAMA_MODEL`, `RU_NEWS_SCRIPT`, `MARKET_NEWS_SCRIPT`, `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_PHONE` / `TELEGRAM_CHANNELS` (Telegram channel collector). See `env.example` for the full list.

If the LLM CLIs are not on your `$PATH` (e.g. cron has a minimal PATH), pin them explicitly with `CODEX_BIN` or `AGY_BIN`.

### Telegram channel collector (optional)

To track posts from Telegram channels you specify in `TELEGRAM_CHANNELS`:

1. Get `api_id` / `api_hash` at [my.telegram.org/apps](https://my.telegram.org/apps) and set them in `.env` along with `TELEGRAM_PHONE` (e.g. `+71234567890`) and `TELEGRAM_CHANNELS` (comma-separated, e.g. `@durov,python_weekly`).
2. One-time login (Telethon session, stored under `workspace/memory/telegram.session`):

   ```bash
   .venv/bin/python standalone_telegram_digest.py auth-start
   # → Telegram sends a code to the "Telegram" chat (id 777000). Grab it, then:
   .venv/bin/python standalone_telegram_digest.py auth-complete 12345
   # If 2FA is enabled: ... auth-complete 12345 --password '<your-2fa-pw>'
   ```

3. Verify: `.venv/bin/python standalone_telegram_digest.py whoami` — prints your user id and resolves each configured channel.

The hourly `main.py` will now pull the latest 10 text posts per channel (media-only posts are skipped). Without these env vars the collector stays silently disabled.

### Run once

```bash
.venv/bin/python main.py            # collects, dedupes, calls LLM, posts to Telegram
.venv/bin/python main.py --dry-run  # everything except LLM + Telegram (prints prompt instead)
```

The first run downloads the multilingual E5 model (~470 MB) into the HuggingFace cache. Subsequent runs are fast.

### Cron (hourly)

After `make deploy`, install the schedule on the server:

```bash
make install-cron
```

This writes two managed entries to the deploy user's crontab (bracketed by `# BEGIN/# END azalio-tech-summary` markers so re-running is idempotent):

```cron
15 * * * * flock -n .cron-main.lock   -c '… main.py'                      >> $REMOTE_DIR/main.log   2>&1
25 * * * * flock -n .cron-reddit.lock -c '… standalone_reddit_digest.py'  >> $REMOTE_DIR/reddit.log 2>&1
```

The hourly run collects and publishes in one pass. A quiet hour ends with the editor returning the no-news sentinel, and `main.py` skips the Telegram post entirely — silence is the expected outcome for most hours.

To switch back to rare issues instead: run the hourly line with `--collect` (accumulates into `pending_intel.txt` without publishing) and add flagless lines for the publishing hours. Cron hours are **UTC** (the VM's system TZ); MSK = UTC+3 year-round. Change the schedule in `deploy/install-cron.sh`, not by hand-editing the crontab — `make install-cron` rewrites the managed block.

Cron has a minimal `$PATH`, so the LLM CLI may not be found by name. `core.py` adds `~/.local/bin`; pin other locations via `CODEX_BIN` or `AGY_BIN` inside `.env`.

### Log rotation

```bash
make install-logrotate
```

Drops `/etc/logrotate.d/azalio-tech-summary` (needs `sudo` on the target) — weekly rotation, 4 generations kept, gzip from generation 2. Covers both `main.log` and `reddit.log`.

### Deploy to a remote server

Stash your host config locally so you don't have to retype it every time:

```bash
cp env.deploy.example .env.deploy
# edit .env.deploy → fill SSH_JUMP, SSH_TARGET, REMOTE_DIR
make deploy
```

`.env.deploy` is gitignored. To deploy without that file, pass the vars inline:

```bash
SSH_JUMP=root@jump SSH_TARGET=user@host REMOTE_DIR=/srv/bot make deploy
```

`make deploy` only scp's source files (`*.py`, `requirements.txt`). It never touches `.env` or `workspace/` on the target — they survive every redeploy.

## Backup and restore

The bot keeps two things outside git: `.env` (secrets + per-deployment config like `REDDIT_MEDIA_SUBS`) and `workspace/memory/` (SQLite dedup state, ~200 MB after a few weeks). Neither is in the repo, so a fresh `git clone` or VM rebuild loses them entirely.

**Snapshot the running deployment** (run from your local machine):

```bash
SSH_TARGET=user@host REMOTE_DIR=/srv/bot make backup
# → backups/YYYY-MM-DD.tgz
```

**Restore onto a fresh VM:**

```bash
SSH_TARGET=user@host REMOTE_DIR=/srv/bot BACKUP=backups/2026-05-11.tgz make restore
```

The archive contains `.env` and `workspace/`; everything else is rebuilt from the repo + `pip install`. Restoring before the first `main.py` run is fine — the bot will pick up the existing dedup clusters and skip whatever's already been published.

## Tests

```bash
python3 -m pytest test_dedup.py -v
```

110 tests. The first run downloads the E5 model (~470 MB) into HuggingFace cache.

## Why CLI-first LLM fallback?

Codex and Antigravity CLIs handle their own authentication and model selection. Ollama Cloud remains an HTTP fallback when neither CLI returns a publishable result. The bot does not stream tokens; the hourly job only needs the final editor response.

## Status

Personal project. Runs on one box, posts to one channel. Provider order and output filtering have unit coverage; live external-provider calls are not part of the suite. No metrics or multi-tenant config.
