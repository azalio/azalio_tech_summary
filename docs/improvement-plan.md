# azalio_tech_summary Improvement Plan

## OpenTelemetry-shaped digest run telemetry [2604.otel-genai]

- Source idea reference: `/Users/azalio/gitroot/azalio/azalio-obsidian/azalio/sources/articles/semantic-conventions-for-generative-ai-systems-opentelemetry.md`
- Benefit hypothesis: standardizing per-run collector, LLM, and Telegram delivery events would make silent source thinning, fallback-model use, and publish failures visible without reading raw cron logs.
- Confidence: medium-high.
- Reasoning: the architecture already has `digest_runs.jsonl`, source-health baselines, LLM CLI fallback, Telegram delivery splitting, and explicit retry semantics; the missing part is a stable telemetry schema that can survive future collectors and model providers.
- Why not already tried: the architecture lists durable metrics, alerting, and tracing beyond stdout as out of scope, and current audit rows are editor-centric rather than full run observability.
- Implementation layer: `main.py`, `collectors.py`, `health.py`, `core.py`, and `eval_digest.py`.
- Missing capability: machine-readable run telemetry that distinguishes source failure, LLM fallback, quiet-hour no-post, Telegram delivery failure, and successful publication.
- Architecture evidence: `docs/ARCHITECTURE.md` describes hourly cron execution, optional collectors, `source_health.json`, LLM fallback through Codex/Gemini, Telegram delivery, and `digest_runs.jsonl`; Known Risks/Gaps explicitly call out silent smaller prompts and lack of durable metrics/tracing.

### Proposed Changes

- Add an append-only `run_events.jsonl` or extend `digest_runs.jsonl` with stable event records for collector start/end, source-health alerts, LLM request/result/fallback, publish attempt/result, and URL/semantic-dedup commit decisions.
- Use OpenTelemetry GenAI-style field names where they fit (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, token/count fields when available), while keeping unsupported fields absent rather than invented.
- Add a small offline check in `eval_digest.py` that reports recent failure/no-post/fallback rates and flags sustained collector silence from the same telemetry stream.
- Cover pure formatting/schema helpers with unit tests; do not require live LLM, Telegram, or external feed access.

## OKF export for reusable digest knowledge [2606.okf]

- Source idea reference: `/Users/azalio/gitroot/azalio/azalio-obsidian/azalio/sources/articles/how-the-open-knowledge-format-can-improve-data-sharing-google-cloud-blog.md`
- Benefit hypothesis: exporting published digest items as Markdown plus YAML frontmatter would turn the Telegram-only output into a searchable, agent-consumable knowledge bundle without changing the publishing path.
- Confidence: medium.
- Reasoning: the project already normalizes source items, ranks candidates, writes editor audit rows, and keeps local state under `VIBE_WORKSPACE`; an optional file export is within the repository's output layer and does not require a new service.
- Why not already tried: the current source of truth is runtime state plus Telegram output; the architecture does not describe a durable knowledge artifact beyond previous digest text and audit JSONL.
- Implementation layer: `main.py`, `ranking.py`, `collectors.py`, and a new small exporter module.
- Missing capability: a stable, portable digest archive that other agents can consume without scraping Telegram messages or replaying raw collector data.
- Architecture evidence: `docs/ARCHITECTURE.md` says the bot produces compact Russian Telegram digests, records editor input/output rows, persists state in `${VIBE_WORKSPACE}/memory/`, and treats source-burst counts as ranking hints rather than published facts.

### Proposed Changes

- Add an opt-in `OKF_EXPORT_DIR` that writes one Markdown file per published digest or topic with YAML frontmatter for `type`, `title`, `timestamp`, source links, section, and digest run ID.
- Keep export generation after successful Telegram publication or quiet-hour no-post decisions so failed delivery does not create authoritative published knowledge.
- Include an `index.md` per month for progressive disclosure and simple `rg`/agent browsing.
- Add tests over a fixture digest to verify frontmatter, escaping, and idempotent filename generation.
