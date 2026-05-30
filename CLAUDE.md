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
  `tail -n 5 …/digest_runs.jsonl | jq -r '.ts, .summary'`.

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
