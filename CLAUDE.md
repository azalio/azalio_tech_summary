# azalio_tech_summary

Hourly news digest for a DevOps/SRE reader (RU output). Collectors gather news →
event-clustering dedup → an LLM CLI writes the digest → posted to Telegram
`@azalio_tech_summary`. Runs from cron on a production VM.

## Server access

The production target is **kept out of git** (`.env.deploy` is gitignored — see
`env.deploy.example`). Read it for the real values; do not hardcode them here:

```bash
source .env.deploy   # exports SSH_TARGET (e.g. user@ip) and REMOTE_DIR
ssh "$SSH_TARGET" "cd $REMOTE_DIR && <cmd>"
```

- Cron: `main.py` at `:15`, `standalone_reddit_digest.py` at `:25` (append to
  `main.log` / `reddit.log` in `REMOTE_DIR`).
- Python venv with all deps lives at `$REMOTE_DIR/.venv`. The system python has
  **no numpy/sentence-transformers** — anything importing `dedup` must use
  `.venv/bin/python`. Set `HF_HUB_OFFLINE=1` (the E5 model is cached) to skip HF
  network calls.
- Passwordless `sudo` is available on the server.

## Production is the only source of truth

- **NEVER inspect the local `workspace/` or local `events.db`** — it is stale
  leftover state and does not reflect what the digest saw or posted. Answer every
  "why did the digest do X" question against the live DB on the server.
- Dedup DB: `$REMOTE_DIR/workspace/memory/semantic_dedup/events.db` (on server).
- Last posted digest: `$REMOTE_DIR/workspace/memory/last_intel_summary.txt`.
- **Historical post data**: `$REMOTE_DIR/workspace/memory/digest_runs.jsonl` —
  append-only JSONL, one record per run (`ts`, `intelligence` = the raw
  candidates incl. ArXiv/HF papers blocks, `event_signals`, `summary` = the
  posted digest). This is the audit trail for "what did the LLM editor keep vs
  drop" — e.g. whether the applied-vs-fundamental AI/ML filter is behaving.
  Rotated by logrotate (monthly, 12 generations); older history is gzipped
  siblings (`digest_runs.jsonl.N.gz`). Inspect with `jq` on the server, e.g.
  `tail -n 5 …/digest_runs.jsonl | jq -r '.ts, .summary'`, or aggregate it with
  `eval_digest.py` (see "Ranking, source-health & eval" below).

## Running & verifying

- **Tests** (real E5 model, server only): copy changed `*.py` to a temp dir on
  the server and run there, never against `$REMOTE_DIR` directly:
  ```bash
  ssh "$SSH_TARGET" 'mkdir -p /tmp/dt'; scp -q *.py "$SSH_TARGET":/tmp/dt/
  ssh "$SSH_TARGET" 'cd /tmp/dt && HF_HUB_OFFLINE=1 '"$REMOTE_DIR"'/.venv/bin/python -m pytest test_dedup.py -q'
  ```
- **Deploy**: `make deploy` (scp source to `$SSH_TARGET:$REMOTE_DIR`). Also
  `make install-cron`, `make backup`. Commit/deploy direct to `main` is the repo
  convention (cron runs from `main`).
- `main.py --dry-run` skips the LLM + Telegram post BUT collectors still run and
  `check_and_add` still commits clusters to `events.db` — so a dry-run mid-hour
  will cannibalize the next real digest's items. Prefer waiting for cron, or
  accept that side effect.

## Dedup (`dedup.py`)

Embeddings-first gate (E5 `multilingual-e5-small`) + language-agnostic anchor
overlap. `emb>=0.92` auto-matches; `0.78–0.92` needs `anchor_overlap>=0.30`;
below 0.78 = new event. Running-mean centroid (frozen after 10 items), cumulative
anchors/numbers per cluster, 72h match window. Do not lower `auto_match` below
~0.90 — e5-small gives unrelated tech news up to 0.897 similarity (false-merge).
Thresholds were measured, not guessed; see the memory note on the rationale.

A cheap **lexical fast-path** runs *before* the E5 encode: a re-syndicated
identical headline (token Jaccard ≥ 0.90 vs a cluster's representative title, ≥5
tokens, no year/version conflict) is dropped without encoding. It's a precision
shortcut, NOT a replacement — anything below that bar (perphrase, extra word,
cross-language) still goes through E5 exactly as before. Tunable/disable via
`EventDedup(lexical_jaccard_min=…)`; `stats()["lexical_skips"]` counts hits.

## Ranking, source-health & eval

- **Engagement ranking** (`ranking.py`): every collector registers a structured
  `Candidate` (with source-native engagement — HN pts / Reddit score / Habr+HF
  upvotes / GitHub stars-day / Telegram views / CVSS). `main.py` fuses them
  (log-normalized engagement + weighted RRF across sources, per-source cap 4 /
  per-author cap 3) into a **priority index** injected into the prompt as a
  `ranking_signal_only` hint — like `event_signals`. The full candidate blob is
  still passed verbatim, so the index only re-orders attention, never drops a
  story. Numbers must NOT leak into the digest (prompt rule 6).
- **Source-health** (`health.py`): **automatic, no action needed.** Tracks a
  rolling per-collector item count in
  `$REMOTE_DIR/workspace/memory/source_health.json` and posts a Telegram notice
  (default chat, title `SOURCE HEALTH`) when a collector returns 0 where it
  normally yields items — i.e. a silently dead/blocked feed. Warmup: needs
  ~`HISTORY_WINDOW=24` runs of history and `min_baseline=3` before it will fire,
  so expect no alerts for ~the first day (and never for env-gated/sparse
  collectors). Runs only on real runs, not `--dry-run`.
- **Eval harness** (`eval_digest.py`): **optional diagnostic**, run by hand when
  you suspect the applied-vs-fundamental AI/ML filter is misbehaving. Reads
  `digest_runs.jsonl` and reports ArXiv/HF paper keep-rate, no-news rate, item
  counts. On the server:
  ```bash
  ssh "$SSH_TARGET" "cd $REMOTE_DIR && .venv/bin/python eval_digest.py --last 50 --show-kept"
  # gzipped rotations: zcat …/digest_runs.jsonl.1.gz | … eval_digest.py -
  ```
  Pure parsing (no E5/network) — also runnable locally on a copied JSONL.

## LLM CLI & VPN (important)

`core.ask_llm` tries **codex** first, then **gemini** (subprocess, inherits env).
codex talks to `chatgpt.com`, which **403s the server's bare IP** — it only works
because the box egresses through a VPN (AmneziaWG `awg-quick@Germany`, full-tunnel
except SSH). If codex starts 403'ing again, the VPN routing rules have likely been
purged: check `ssh "$SSH_TARGET" 'curl -s https://api.ipify.org'` — a German exit
IP means the tunnel is up, the bare server IP means it's down. Recovery:
`sudo systemctl restart awg-quick@Germany`. (Full setup + the networkd
rule-purging fix are in the project memory note `server-vpn-full-tunnel`.)

## Gotchas

- All network fetches must pass a timeout. `_fetch_rss` now fetches via
  `requests.get(timeout=15)` then `feedparser.parse(bytes)` — never
  `feedparser.parse(url)` (no timeout → one stalled feed hangs the whole run).
- The digest only posts after a non-empty LLM response; on empty output it sends
  a failure notice and leaves URL/cluster state for the next run.
