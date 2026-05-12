# azalio_tech_summary

Hourly tech-news digest bot. Pulls headlines from ~15 sources, deduplicates them with embeddings, asks an LLM to write a structured Russian-language digest, and posts to a Telegram channel.

Channel: [@azalio_tech_summary](https://t.me/azalio_tech_summary).

## What it does

Every hour the bot:

1. **Collects** ~150-300 headlines in parallel from RSS, REST APIs, and HTML pages.
2. **Deduplicates** them in two layers — exact URL match (SQLite, 30-day TTL) and semantic clustering on multilingual sentence embeddings (`intfloat/multilingual-e5-small`).
3. **Summarises** what's left with an LLM (Gemini CLI, with Codex CLI as fallback) under a strict editorial prompt: DevOps/SRE → AI/ML → Security → Science → Politics, no preamble, no editorial commentary.
4. **Posts** the digest to a Telegram channel, splitting by section if it overflows the 4096-char limit.

Output goes to ~30 buckets per day, ~5-15 bullets per digest after dedup.

## Sources

- **Reddit** — ~30 tech subreddits via Reddit API (programming, kubernetes, MachineLearning, ClaudeAI, ...).
- **Telegram channels** — last 10 text posts per channel via Telethon/MTProto (optional, channel list in `TELEGRAM_CHANNELS`).
- **HackerNews** — front page via Algolia.
- **Tech press (RSS)** — TechCrunch, Ars Technica, The Verge, Wired, MIT Tech Review, IEEE Spectrum, The Register.
- **AI research** — HuggingFace Daily Papers (upvotes ≥ 100), ArXiv RSS (cs.AI, cs.LG, cs.CL).
- **Infra/DevOps** — Kubernetes, CNCF, AWS, Cloudflare, CISA Alerts.
- **Science/Space** — NASA, Nature, ScienceDaily, SpaceNews, ESO, ESA, Chandra X-ray, Phys.org.
- **Global news** — BBC, Al Jazeera, DW.
- **Google News** — search-based RSS.
- **NewsAPI** — AI / DevOps / Cybersecurity categories (optional, needs API key).
- **Finnhub** — general financial news (optional, needs API key).
- **Habr** — top daily articles (score ≥ 100).
- **Claude Platform release notes** — direct `.md` fetch from `platform.claude.com`.
- **GitHub Trending** — top 5 daily by stars-today (≥ 200), with first ~400 chars of README via `raw.githubusercontent.com`.

## Architecture

```
collectors.py ──► dedup.py ──► main.py (LLM call) ──► core.py (Telegram)
   RSS/API           E5 model      VIBE_PROMPT          HTML format
   ~15 sources       SQLite        gemini/codex CLI     auto-split
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
- **An LLM CLI** — at least one of:
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) — needs Node.js ≥ 20.
    Install: `npm install -g @google/gemini-cli`. Authenticate once interactively (`gemini` → Google login). Auth lives in `~/.gemini/oauth_creds.json`.
  - [Codex CLI](https://github.com/openai/codex) — `brew install --cask codex`. Authenticate via `codex login`. Auth lives in `~/.codex/auth.json`.
  - Either both, or just one; if both present, Gemini is tried first.
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

Optional (collectors silently skip when unset): `FINNHUB_API_KEY`, `NEWSAPI_KEY`, `GEMINI_BIN`, `CODEX_BIN`, `RU_NEWS_SCRIPT`, `MARKET_NEWS_SCRIPT`, `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_PHONE` / `TELEGRAM_CHANNELS` (Telegram channel collector). See `env.example` for the full list.

If the LLM CLIs are not on your `$PATH` (e.g. cron has a minimal PATH), pin them explicitly: `GEMINI_BIN=/home/you/.npm-global/bin/gemini`.

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

```cron
15 * * * * cd /home/you/azalio_tech_summary && .venv/bin/python main.py >> /var/log/azalio_tech_summary.log 2>&1
```

Cron has a minimal `$PATH`, so the LLM CLI may not be found by name. Pin it via `GEMINI_BIN` (or `CODEX_BIN`) inside `.env`, or prepend the directory to the cron line's `PATH`.

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

43 tests. The first run downloads the E5 model (~470 MB) into HuggingFace cache.

## Why subprocess-based LLM, not the SDK?

Because Gemini and Codex CLIs handle authentication, model selection, and rate-limiting on their own — calling them via `subprocess` is one less moving part than juggling SDKs across providers. The trade-off is that the bot can't stream tokens or get structured output; for a once-an-hour batch job that's fine.

## Status

Personal project. Runs on one box, posts to one channel. No tests for the LLM step (it's an external CLI), no metrics, no multi-tenant config. PRs and issues welcome but I might not get to them quickly.
