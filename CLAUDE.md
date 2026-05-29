# azalio_tech_summary

Hourly news digest. Cron on the production VM runs `main.py` at `:15` and
`standalone_reddit_digest.py` at `:25`. Dedup is event-clustering (`dedup.py`,
E5 embeddings) backed by SQLite.

## Production is the only source of truth

- **NEVER inspect the local `workspace/` or the local `events.db`** — the local
  copy is stale leftover state and does NOT reflect what the digest actually
  saw or posted. Any "why did the digest do X" question MUST be answered against
  the live database on the server.
- Live state lives on the deploy host from `.env.deploy`
  (currently `azalio@81.26.187.75:/home/azalio/azalio_tech_summary`).
- Dedup DB: `workspace/memory/semantic_dedup/events.db` **on the server**.
- Last posted digest: `workspace/memory/last_intel_summary.txt` **on the server**.
- The server has no system `numpy`; use the venv interpreter for any script that
  imports `dedup`/`numpy`: `.venv/bin/python`.

Example — query the real dedup DB:

```bash
ssh azalio@81.26.187.75 'cd /home/azalio/azalio_tech_summary && \
  sqlite3 workspace/memory/semantic_dedup/events.db "<query>"'
```
