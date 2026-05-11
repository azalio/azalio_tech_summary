# azalio_tech_summary Architecture

## Overview

`azalio_tech_summary` is a personal hourly technology-news digest bot. It
collects candidate headlines from RSS feeds, REST APIs, Reddit, Hacker News,
Habr, GitHub Trending, HuggingFace Daily Papers, arXiv, and optional external
collector scripts. It removes repeated stories with URL and semantic event
deduplication, asks an LLM CLI to write a compact Russian digest, and posts the
result to Telegram.

The repository is intentionally small: the production path is a single Python
process started from `main.py`, with persistent local state under
`${VIBE_WORKSPACE}/memory/`.

## Scope

In scope:

- Collecting tech, AI/ML, security, science, finance, and global-news items
  from configured public sources.
- Filtering already-seen URLs and semantically duplicated events before the LLM
  prompt is built.
- Calling an installed LLM CLI, preferring Gemini and falling back to Codex.
- Formatting Markdown-like LLM output as Telegram-compatible HTML and splitting
  long digests into multiple Telegram messages.
- Deploying the source files to a single remote host via `make deploy`.

Out of scope:

- Multi-tenant operation, user-facing administration, or channel management.
- Streaming LLM responses or SDK-based model calls.
- Durable metrics, alerting, or tracing beyond local stdout diagnostics.
- End-to-end tests for external APIs, LLM CLIs, and Telegram delivery.

## Quality Goals

- Keep the hourly digest concise, factual, and focused on DevOps/SRE, AI/ML,
  security, and science.
- Avoid repeating stories already published in the previous digest or captured
  by another source.
- Allow optional collectors to fail or be disabled without stopping the whole
  run.
- Keep deployment simple enough for a cron-driven personal server.
- Preserve local state across runs so deduplication and prompt context work
  across digest windows.

## System Context

The bot runs as a scheduled Python command on one host. External dependencies
are public feed/API endpoints, optional API keys, an authenticated LLM CLI, and
the Telegram Bot API. Runtime configuration comes from `.env` and environment
variables documented in `env.example`.

The default output channel is `TELEGRAM_DIGEST_CHAT`; `TELEGRAM_DEFAULT_CHAT_ID`
is used as the fallback chat. `VIBE_WORKSPACE` controls the directory that holds
SQLite databases, raw collector handoff JSON, and the previous digest text.

## Core Structure

- `main.py` orchestrates the run. It loads `.env`, initializes `EventDedup` and
  `Collectors`, calls each collector in order, builds `VIBE_PROMPT`, invokes the
  LLM through `VibeCore`, posts the result, and stores the latest summary.
- `collectors.py` owns source integrations. It contains RSS helpers, URL
  normalization, `sent_posts` URL deduplication, optional API-key collectors,
  and source-specific formatting for the prompt payload.
- `dedup.py` owns semantic event clustering. It uses
  `intfloat/multilingual-e5-small` embeddings, token extraction, entity aliasing,
  Jaccard overlap, SQLite persistence, TTL cleanup, matching windows, and
  cluster-size guards.
- `core.py` owns LLM CLI execution and Telegram delivery. It discovers Gemini or
  Codex from explicit env vars or PATH, converts basic Markdown to Telegram
  HTML, and falls back to plain text if Telegram rejects HTML.
- `standalone_reddit_digest.py` is the bundled Reddit fetcher used by
  `Collectors.collect_reddit`.
- `test_dedup.py` covers headline normalization, token extraction,
  canonicalization, event clustering behavior, TTL handling, and collector
  recap filtering.
- `Makefile` provides `install`, `test`, and `deploy` targets.

## Runtime Flows

### Hourly Digest Run

1. `main.py` loads configuration and creates `${VIBE_WORKSPACE}/memory/`.
2. `EventDedup` opens `semantic_dedup/events.db`, expires old clusters, and
   loads active clusters into memory.
3. `Collectors` opens or creates `memory/reddit_sent.db` and removes URL entries
   older than the configured TTL.
4. Each collector returns formatted source lines or an empty string. Optional
   collectors skip themselves when their API key or external script is missing.
5. URL deduplication stores normalized links in `sent_posts`. Semantic
   deduplication creates or updates event clusters for non-duplicate titles.
6. `main.py` inserts the previous digest and new source data into `VIBE_PROMPT`.
7. In dry-run mode, the prompt is printed. Otherwise `VibeCore.ask_llm` calls
   Gemini first and then Codex if Gemini is unavailable or fails.
8. `VibeCore.send_tg` formats the digest, posts it to Telegram, and splits it by
   topic section when it exceeds the Telegram message limit.
9. The posted text is written to `last_intel_summary.txt` for the next run.

### Deployment Flow

`make deploy` creates the target directory over SSH and copies the Python source
files, tests, and `requirements.txt` with `scp`. Runtime `.env` and scheduler
setup remain host-local responsibilities.

## Source of Truth

- Collector behavior and source list: `collectors.py`.
- Prompt policy and orchestration order: `main.py`.
- Semantic deduplication rules and schema: `dedup.py`.
- Telegram and LLM CLI integration: `core.py`.
- Required and optional runtime configuration: `env.example`.
- Setup, deployment, and operational notes: `README.md`.

## Cross-cutting Concepts

- Two-stage deduplication: normalized URL history catches exact repeats, while
  event clusters catch semantically repeated stories across sources.
- Local state: all runtime memory is file-backed under `VIBE_WORKSPACE`, mostly
  through SQLite plus the previous digest text file.
- Graceful degradation: missing optional API keys, missing optional scripts, and
  individual source fetch failures usually return an empty collector result
  instead of aborting the run.
- CLI-based LLM boundary: model authentication and rate-limit handling are
  delegated to installed Gemini or Codex CLIs rather than SDK credentials.
- Telegram formatting boundary: generated Markdown-like output is transformed to
  Telegram HTML at the edge, with plain-text fallback on delivery errors.

## Deployment/Operations

The expected production mode is a cron job that runs `python main.py` hourly on
a host with a Python virtualenv, `.env`, Telegram credentials, and at least one
authenticated LLM CLI. The first semantic-dedup run downloads the E5 embedding
model into the HuggingFace cache.

`make test` runs the focused deduplication test suite. `make deploy` copies
source files to a remote server but does not provision `.env`, system packages,
cron, or secrets.

## Known Risks/Gaps

- LLM and Telegram paths are not covered by automated tests because they depend
  on authenticated external CLIs and Telegram credentials.
- Source fetch failures are mostly printed and skipped, so a cron run can
  silently produce a smaller prompt unless logs are reviewed.
- Semantic deduplication depends on an in-process E5 model and local SQLite
  state; the first run is heavy and state loss resets duplicate memory.
- HTML formatting uses regular expressions, which can fail on malformed or
  unexpected Markdown from the LLM.
- Deployment is file-copy based and leaves scheduler, dependency installation,
  secrets, and rollback mechanics outside the repository.

## ADR Links

No ADR documents are present in this repository as of this review.

## Freshness

Reviewed on 2026-05-11 against repository evidence in `README.md`, `main.py`,
`collectors.py`, `dedup.py`, `core.py`, `test_dedup.py`, `env.example`, and
`Makefile`.
