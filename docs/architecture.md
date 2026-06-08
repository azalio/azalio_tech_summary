# azalio_tech_summary Architecture

## Overview

`azalio_tech_summary` is a personal hourly technology-news digest bot. It
collects candidate headlines from RSS feeds, REST APIs, Reddit, Telegram
channels, Hacker News, Habr, GitHub Trending, HuggingFace Daily Papers, arXiv,
Claude release notes, NVD/CISA/security feeds, Google News, applied AI/SRE
engineering feeds, and optional external collector scripts. It removes repeated
stories with URL and semantic event deduplication, asks an LLM CLI to write a
compact Russian digest, and posts the result to Telegram. Current-run event
clusters are also surfaced into the prompt as ranking-only source-burst signals
so repeated coverage from multiple sources can influence section priority
without becoming an emitted fact by itself.

The repository is intentionally small: the production path is a single Python
process started from `main.py`, with persistent local state under
`${VIBE_WORKSPACE}/memory/`.

## Scope

In scope:

- Collecting tech, AI/ML, security, science, finance, channel, release-note,
  and global-news items from configured public sources.
- Filtering already-seen URLs and semantically duplicated events before the LLM
  prompt is built.
- Surfacing multi-source event bursts to the LLM prompt as ranking hints while
  keeping observations and source counts out of the published digest unless the
  count is itself newsworthy.
- Calling an installed LLM CLI, preferring Gemini and falling back to Codex.
- Refusing to publish raw collector output when the LLM layer returns no digest,
  while notifying the operator and preserving retry state.
- Formatting Markdown-like LLM output as Telegram-compatible HTML and splitting
  long digests into multiple Telegram messages, including line-bounded splits
  for oversized topic sections.
- Skipping Telegram publication when the editor emits the explicit quiet-hour
  sentinel for a genuinely empty news window, while still committing seen URLs.
- Recording append-only editor input/output audit rows for later filter review.
- Deploying the source files to a single remote host via `make deploy`.
- Installing idempotent remote cron entries and logrotate configuration,
  including rotation for the editor audit log.
- Managing Telegram-channel config with helper scripts that merge channel lists
  into `.env` and smoke-check channel readability on the remote host.
- Snapshotting and restoring remote `.env` plus workspace state.

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
  LLM through `VibeCore`, posts the result, and stores the latest summary. It
  also formats current-run `event_signals` from semantic clusters, injects them
  into the prompt as ranking-only context, logs editor input/output rows to
  `digest_runs.jsonl`, and handles the quiet-hour no-post sentinel.
- `collectors.py` owns source integrations. It contains RSS helpers, URL
  normalization, `sent_posts` URL deduplication, optional API-key collectors,
  source-specific formatting for the prompt payload, and handoff points for
  bundled Reddit/Telegram fetcher subprocesses. Every collector also registers a
  structured `ranking.Candidate` per emitted item (with source-native engagement
  where available) and increments per-collector item counts for source-health
  tracking.
- `ranking.py` fuses the structured candidates into a single diversity-capped
  priority index handed to the editor as a ranking-only hint. It normalizes each
  source's engagement metric (HN points, Reddit score, Habr/HF upvotes, GitHub
  stars/day, Telegram views, CVSS) onto a common log axis, applies weighted
  reciprocal-rank fusion across source streams, and enforces hard per-source and
  per-author caps.
- `health.py` keeps a rolling per-collector item-count baseline
  (`source_health.json`) and flags silent failures — a collector that returns
  nothing when it normally yields items — so dead/blocked feeds surface instead
  of quietly thinning the digest.
- `eval_digest.py` is an offline harness over `digest_runs.jsonl` that measures
  editor behavior across history: ArXiv/HF paper keep-rate (the applied-vs-
  fundamental AI/ML filter), no-news rate, item counts, and section presence.
- `dedup.py` owns semantic event clustering. It uses
  `intfloat/multilingual-e5-small` embeddings, token extraction, entity aliasing,
  language-agnostic anchor overlap, year/version conflict checks, running-mean
  centroids with a freeze limit, SQLite persistence, TTL cleanup, matching
  windows, and cluster-size guards. A cheap lexical pre-dedup fast-path
  (token Jaccard vs a cluster's representative title) drops re-syndicated
  identical headlines before paying for an E5 encode. It tracks clusters touched
  during the current process, marks already reported clusters, and exposes
  high/medium source-burst summaries through `event_signals()`.
- `core.py` owns LLM CLI execution and Telegram delivery. It discovers Gemini or
  Codex from explicit env vars or PATH, deduplicates resolved binaries, runs
  Codex through `codex exec --skip-git-repo-check -o <tmpfile> -` so cron can
  capture the final response without entering the TUI, kills timed-out child
  processes, converts basic Markdown to Telegram HTML, and falls back to plain
  text if Telegram rejects HTML.
- `standalone_reddit_digest.py` is the bundled Reddit fetcher used by
  `Collectors.collect_reddit`.
- `standalone_telegram_digest.py` is the optional Telethon collector for
  configured Telegram channels.
- `deploy/install-cron.sh` and `deploy/install-logrotate.sh` install the
  remote scheduler and log rotation entries used by `Makefile` targets.
- `deploy/merge_channels.py` and `deploy/check_channel.py` are remote operator
  helpers for channel-list updates and readability checks.
- `test_dedup.py` covers headline normalization, token extraction,
  canonicalization, event clustering behavior, TTL handling, lexical pre-dedup,
  and collector recap filtering. `test_ranking.py`, `test_health.py`, and
  `test_eval_digest.py` cover the ranking/health/eval modules (pure, no E5 —
  runnable anywhere).
- `Makefile` provides install, test, deploy, cron/logrotate, backup, and
  restore targets.

## Runtime Flows

### Hourly Digest Run

1. `main.py` loads configuration and creates `${VIBE_WORKSPACE}/memory/`.
2. `EventDedup` opens `semantic_dedup/events.db`, expires old clusters, and
   loads active clusters into memory.
3. `Collectors` opens or creates `memory/reddit_sent.db` and removes URL entries
   older than the configured TTL.
4. Each collector returns formatted source lines or an empty string. Optional
   collectors skip themselves when their API key, Telegram auth, or external
   script is missing.
5. URL deduplication queues normalized links in an in-memory pending set;
   they are not written to `sent_posts` until the digest is successfully
   posted or the editor returns an explicit quiet-hour no-post decision.
   Semantic deduplication creates or updates event clusters for non-duplicate
   titles eagerly during this step.
6. `main.py` inserts the previous digest and new source data into `VIBE_PROMPT`.
7. `main.py` formats current-run event signals and includes them in the prompt
   as ranking-only context. In dry-run mode, the prompt is printed. Otherwise
   `VibeCore.ask_llm` calls Codex first and then Gemini if Codex is unavailable
   or fails. The fallback
   path is CLI-specific: Gemini reads stdin/stdout directly, while Codex writes
   its final answer to a temporary output file.
8. `VibeCore.send_tg` formats the digest, posts it to Telegram, splits it by
   topic section when it exceeds the Telegram message limit, line-splits any
   single oversized section, and returns whether every part was delivered.
9. `main.py` appends the editor input/output record to
   `memory/digest_runs.jsonl`. If the digest is the quiet-hour sentinel, it
   skips Telegram publication, commits URL marks, and leaves the previous real
   digest as the next run's context anchor.
10. If no LLM-formatted digest is returned, the bot sends an operator failure
   notice to the default chat and exits without posting raw collector lines or
   committing URL state.
11. On a successful digest send, `Collectors.commit_seen` persists the pending
   URL marks to `sent_posts` and the posted text is written to
   `last_intel_summary.txt` for the next run. On delivery failure, both are
   skipped so the next run's URL dedup gate sees the same items as un-seen.
   Successfully posted source-burst clusters are marked reported so they do not
   keep re-entering `event_signals()` on later runs.

### Deployment Flow

`make deploy` reads optional host settings from `.env.deploy`, supports direct
SSH or a bastion via `SSH_JUMP`, creates the target directory over SSH, and
copies the Python source files, tests, and `requirements.txt` with `scp`.
Runtime `.env`, workspace state, and virtualenv setup remain host-local
responsibilities.

`make install-cron` uploads `deploy/install-cron.sh` and writes managed hourly
cron entries for `main.py` and `standalone_reddit_digest.py`. `make
install-logrotate` uploads `deploy/install-logrotate.sh` and installs weekly
rotation for `main.log`, `reddit.log`, and `workspace/memory/digest_runs.jsonl`.
`make add-channels CHANNELS=...` backs up remote `.env` and merges channel
values through `deploy/merge_channels.py`; `make check-channels CHANNELS=...`
runs the Telethon readability probe from `deploy/check_channel.py`. `make
backup` snapshots remote `.env` plus `workspace/`; `make restore BACKUP=...`
extracts that archive onto the target before the next run.

## Source of Truth

- Collector behavior and source list: `collectors.py`.
- Prompt policy and orchestration order: `main.py`.
- Semantic deduplication rules and schema: `dedup.py`.
- Editor decision audit rows: `${VIBE_WORKSPACE}/memory/digest_runs.jsonl`.
- Telegram and LLM CLI integration: `core.py`.
- Required and optional runtime configuration: `env.example`.
- Setup, deployment, and operational notes: `README.md`.
- Remote scheduler/log rotation behavior: `deploy/install-cron.sh`,
  `deploy/install-logrotate.sh`, and `Makefile`.
- Channel-list operator helpers: `deploy/merge_channels.py` and
  `deploy/check_channel.py`.

## Cross-cutting Concepts

- Two-stage deduplication: normalized URL history catches exact repeats, while
  event clusters catch semantically repeated stories across sources. The event
  gate combines embeddings with anchor overlap, typed year/version conflict
  checks, running-mean centroids, cluster-size guards, and reported-cluster
  memory so already published source bursts do not keep coming back.
- Source-burst ranking: current-run event clusters with repeated observations or
  multiple sources can promote a story during selection, but their counts are
  prompt hints rather than publication facts.
- Local state: all runtime memory is file-backed under `VIBE_WORKSPACE`, mostly
  through SQLite plus previous digest text, editor audit JSONL, and fetcher
  handoff JSON files.
- Graceful degradation: missing optional API keys, missing optional scripts, and
  individual source fetch failures usually return an empty collector result
  instead of aborting the run.
- CLI-based LLM boundary: model authentication and rate-limit handling are
  delegated to installed Gemini or Codex CLIs rather than SDK credentials.
  Codex is invoked through non-interactive `codex exec`; running bare `codex`
  is not a valid cron fallback because it opens an interactive interface.
- Telegram formatting boundary: generated Markdown-like output is transformed to
  Telegram HTML at the edge, with plain-text fallback on delivery errors.
- Publication safety boundary: the public channel receives only an LLM-formatted
  digest. Empty LLM output produces an operator alert and leaves pending source
  URLs available for retry instead of dumping raw collector payloads. A separate
  quiet-hour sentinel is treated as a successful no-post decision, not a failure.

## Deployment/Operations

The expected production mode is a cron job that runs `python main.py` hourly on
a host with a Python virtualenv, `.env`, Telegram credentials, and at least one
authenticated LLM CLI. The first semantic-dedup run downloads the E5 embedding
model into the HuggingFace cache.

`make test` runs the focused deduplication test suite. `make deploy` copies
source files to a remote server but does not provision `.env`, system packages,
virtualenvs, or secrets. `make install-cron` and `make install-logrotate`
install the scheduler/log retention pieces after deploy. `make backup` and
`make restore` protect the non-Git state needed for a fresh VM.

## Known Risks/Gaps

- LLM and Telegram paths are not covered by automated tests because they depend
  on authenticated external CLIs and Telegram credentials.
- Source fetch failures are mostly printed and skipped, so a cron run can
  silently produce a smaller prompt unless logs are reviewed.
- Semantic deduplication depends on an in-process E5 model and local SQLite
  state; the first run is heavy and state loss resets duplicate memory.
- Semantic event clusters are committed while collectors run, before Telegram
  delivery. URL marks are deferred until successful send or explicit quiet-hour
  no-post, but an LLM or delivery failure can still leave semantic state ahead
  of public delivery.
- The quiet-hour sentinel depends on the LLM following the prompt exactly; if it
  is emitted incorrectly, a legitimate small digest may be skipped.
- HTML formatting uses regular expressions, which can fail on malformed or
  unexpected Markdown from the LLM.
- Deployment is file-copy based and leaves dependency installation, secrets,
  virtualenv creation, and rollback mechanics outside the repository.

## ADR Links

No ADR documents are present in this repository as of this review.

## Freshness

Reviewed on 2026-06-05 against repository evidence in `README.md`, `main.py`,
`collectors.py`, `dedup.py`, `core.py`, `standalone_reddit_digest.py`,
`standalone_telegram_digest.py`, `test_dedup.py`, `env.example`,
`env.deploy.example`, `Makefile`, `deploy/install-cron.sh`,
`deploy/install-logrotate.sh`, `deploy/merge_channels.py`, and
`deploy/check_channel.py`.

Current delta captured: source coverage includes the newer applied-AI,
SRE-depth, engineering-blog, r/singularity, and channel additions; the editor
prompt now separates applied engineering from fundamental AI/ML noise while
allowing empirical AI-agent behavior stories into the science lane; quiet hours
skip Telegram posting via an explicit sentinel; editor input/output is recorded
in `digest_runs.jsonl`; channel-list updates and readability checks have
dedicated deploy helpers; and semantic deduplication now combines anchor
overlap, year/version conflict checks, centroid freezing, cluster size caps, and
reported-cluster memory to avoid reposting the same story for days.
