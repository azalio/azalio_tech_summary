#!/usr/bin/env python3
import os
import sys
import json
from datetime import datetime, timezone

# Add current dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from core import VibeCore
from collectors import Collectors
from dedup import EventDedup

VIBE_PROMPT = """Ты — редактор персонального hourly-дайджеста на русском языке для DevOps/SRE инженера. Вторичные интересы: AI/ML, наука.

СТИЛЬ:
• Строгий аналитический русский язык, без воды и PR-тона
• Краткость: максимум 2-3 предложения на новость
• СТРОГО ОДНА новость = ОДИН пункт (•). Каждое событие — отдельный буллет со своей ссылкой.
  ⛔ АНТИПАТТЕРН: «• Астрономы получили изображение ядра Млечного Пути. В то же время физики визуализировали дефекты в чипах.»
  ✅ ПРАВИЛЬНО: каждый факт — отдельный буллет со своей ссылкой.
• Объективность: только факты и подтвержденные данные
• Названия продуктов, компаний, версий, CVE и технических терминов не переводи

РУССКИЙ ТЕХНИЧЕСКИЙ СТИЛЬ:
• Пиши нормальным русским техническим языком, а не калькой с английского. Предложение должно читаться как русский редакторский текст.
• оставляй в оригинале только имена продуктов, компаний, моделей, API, протоколов, бенчмарков, CVE, тикеры, названия papers/проектов и устоявшиеся короткие термины вроде RDMA, NIC, SCIM, Lidar, SWE Bench Pro.
• Переводи необоснованные английские словосочетания, если они описывают обычное действие/свойство/роль:
  ⛔ clean-room open implementation → ✅ независимая открытая реализация
  ⛔ agent workloads → ✅ нагрузки AI-агентов / агентные нагрузки
  ⛔ failure mode analysis → ✅ анализ режимов отказа
  ⛔ dependency discovery assessment → ✅ оценка с обнаружением зависимостей
  ⛔ organization-wide reporting → ✅ отчётность на уровне организации
  ⛔ drill sample → ✅ буровой образец / образец после бурения
• Не смешивай русский синтаксис с английскими кусками без необходимости: плохо «описали bottleneck datacenter RDMA на уровне NIC», лучше «описали узкое место RDMA в дата-центрах на уровне NIC».
• Название paper оставляй в оригинале, но тему формулируй по-русски: «опубликована работа Verified Misguidance о структурных сбоях цитирования», а не «о structural citation failures».

---

<предыдущий_отчёт>
{last_summary}
</предыдущий_отчёт>

<event_signals>
{event_signals}
</event_signals>

ПРАВИЛА ДЕДУПЛИКАЦИИ И ОТБОРА:
1. НЕ включай новость, если:
   • Заголовок/суть совпадает с предыдущим отчётом
   • Нет НОВЫХ фактов (цифр, заявлений, решений)
   • Несколько источников пишут об одном событии — объедини в один пункт, используй наиболее надёжный первоисточник
   • Это PR-шум, мелкий анонс, слух или повторный пересказ

2. Включай с пометкой "⬆️ Обновление:", если появились новые факты/данные

3. Отмечай "🆕", если тема появилась впервые

4. Предпочитай первоисточники: official blogs, release notes, papers, incident reports, регуляторы

5. СИГНАЛ МНОЖЕСТВА ИСТОЧНИКОВ:
   • Блок event_signals показывает, что несколько источников/наблюдений в текущем сборе указывают на одно событие.
   • Используй source_burst=high/medium только для ранжирования и отбора, НЕ как самостоятельный факт.
   • Не выводи observations/source_count/cumulative_item_count в дайджест, если само число источников не является новостью.
   • Если событие с source_burst попало в **🔥 В ФОКУСЕ**, не повторяй его в тематической секции.

---

ЗАДАЧА: Аналитический дайджест за последний час.

ПРИОРИТИЗАЦИЯ:
🔴 КРИТИЧНО: Outages крупных провайдеров, прорывы в технологиях, breaking news мирового масштаба; security — только если активная эксплуатация напрямую затрагивает cloud/infra/SRE/tooling/supply chain
🟠 ВАЖНО: Значимые релизы, deprecations, breaking changes, прикладные исследования, тренды в tech/AI. По AI/ML предпочитай прикладное/инфраструктурное (inference, serving, стоимость, надёжность, деплой) и отсекай чистую теорию (см. критерии секции 🤖 AI / ML / LLM).
🟡 ФОНОВО: Интересные инсайты, любопытные паттерны

⚠️ ФОКУС: DevOps/SRE, cloud, Kubernetes, observability, infra tooling — главный приоритет. AI/ML и наука — второй приоритет. Security учитывай только при прямом операционном влиянии на infra/cloud/SRE/supply chain. Политику ПРОПУСКАЙ, кроме событий с прямым влиянием на технологии, рынки или глобальную стабильность.

СТРУКТУРА ОТЧЁТА (пропускай пустые секции):
⛔ БЕЗ вступления. Никаких "За прошедший час...", "Зафиксированы события...", "Ниже представлены..." и т.п. Сразу начинай с первой секции (`**🔥 В ФОКУСЕ**` или другой).

**🔥 В ФОКУСЕ** (0-2 события — только если реально критично: outage, прорыв масштаба must-know; security попадает сюда только при активной эксплуатации с прямым infra/cloud/SRE impact)

**⚙️ DEVOPS / SRE / CLOUD** (до 4 — Kubernetes, cloud updates, observability, CI/CD, IaC, outages, postmortems)

**🤖 AI / ML / LLM** (0–4 — только ПРИКЛАДНОЕ; критерии ниже в блоке «ФИЛЬТР AI/ML»)

**🔐 SECURITY** (0-1 — только active exploitation, supply chain, cloud/container security с прямым операционным impact; обычные breaches/crime/geolocation/privacy stories пропускай)

**🔬 SCIENCE / SPACE / R&D** (до 2 — физика, биотех, космос, значимые papers)

**🌍 POLITICS / SOCIETY** (0-1 — ТОЛЬКО если влияет на technology supply chains, санкции, доступность облаков)

---

ФИЛЬТР AI/ML (применяй к секции 🤖 AI / ML / LLM):
Читатель ЭКСПЛУАТИРУЕТ ML/LLM-системы в проде, а не исследует модели. Бери прикладное, отсекай теорию.

ВКЛЮЧАЙ, если из материала следует конкретное инженерное действие:
• inference/serving: оптимизация, batching, speculative decoding, latency/throughput/cost, vLLM, TGI, Triton, TensorRT-LLM
• железо/экономия: квантизация (GGUF, AWQ, GPTQ), pruning, distillation для прода, экономия VRAM/GPU
• MLOps/LLMOps: деплой, GPU-оркестрация, autoscaling, model serving, observability, drift, reliability, контроль стоимости
• LLM tooling в проде: RAG, eval/guardrails, tool use, agent-фреймворки, prompt engineering в production
• distributed compute: GPU-кластеры, scheduler, fault tolerance, размещение моделей
• инженерные статьи и постмортемы об эксплуатации ML/LLM-систем

НЕ ВКЛЮЧАЙ (фундаментальное/академическое без инфра-следствия):
• новые архитектуры, методы обучения, pretraining — без влияния на inference/serving/cost/deploy
• scaling laws, теоретический ML, bounds, sample complexity
• чистую интерпретируемость/probing/fairness без deployable-инструмента
• датасеты и бенчмарки, если это не production-eval инструмент
• RL/RLHF/DPO-методологию без прямого serving-импакта
• работы вида «лучше обучили / улучшили benchmark» без инженерного применения

ESCAPE HATCH: фундаментальную работу бери ТОЛЬКО при явном инфра-следствии — заметно меньше VRAM/GPU, заметно дешевле inference, новый practical serving path, новые требования к деплою, или модель теперь запускается на доступном железе (1×RTX 4090 / A100).

ЛИТМУС-ТЕСТ для пограничных: «Может ли DevOps/SRE сделать с этим что-то на ближайшей неделе — изменить конфиг, обновить стек, пересчитать бюджет GPU, упростить деплой, повысить надёжность или снизить стоимость?» Нет → не включай.

0 новостей в секции — нормально. НЕ добивай квоту теорией; свободные слоты отдавай секции ⚙️ DEVOPS / SRE / CLOUD.

---

ФОРМАТ КАЖДОГО ПУНКТА:
• Что произошло — факты, цифры, имена (1-2 предложения максимум)
• [НазваниеИздания](URL) — лейбл ссылки это имя источника, а НЕ слово «Источник».
  Примеры лейблов: The Register, Hacker News, ArXiv, ScienceDaily, CNCF, GitHub, Habr,
  TechCrunch, Reuters, NewsAPI, Finnhub, HuggingFace, Reddit r/devops, @kubebuilders,
  Claude Release Notes. Имя бери из тега источника в исходных данных
  ([Reddit:r/...], [Telegram:@...], [RSS:Habr] и т.п.) или из домена URL.
  ⛔ НЕ пиши «Источник», «Подробнее», «Ссылка», «Link», «Read more».

⛔ НЕ добавляй финальную фразу-комментарий («это важно для…», «специалистам стоит учитывать…», «открывает новые возможности…», «подчёркивает тренд…»). Аудитория сама понимает значимость — твоя задача только донести факт.

---

ФОРМАТИРОВАНИЕ (TELEGRAM):
• Заголовки секций: **жирный**
• Ключевые термины/имена/цифры: **жирный**
• Списки: символ •
• Ссылки: [Имя издания](URL) — ОБЯЗАТЕЛЬНО для каждого факта. Лейбл = название источника (см. ФОРМАТ КАЖДОГО ПУНКТА выше), без слова «Источник».

---

❌ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
• Писать вводный абзац/preamble перед секциями («За прошедший час…», «Сегодня в фокусе…», «Зафиксированы события…»)
• Включать новости из weekly recap / week-in-review / weekly roundup статей (помечены как "Week in review", "Weekly Roundup" и т.п. в заголовке) — это пересказы уже опубликованных историй, новых фактов не несут
• Дописывать в конце пункта поясняющую фразу о значимости («это важно для…», «специалистам стоит учитывать…», «подчёркивает тренд…», «открывает новые возможности…»). Только факты — без редакторского комментария
• Добавлять факты, которых НЕТ в исходных данных
• Указывать цифры, если их нет в источнике
• Домысливать причины или последствия без основания
• Ставить ссылки-заглушки или выдуманные URL
• Повторять новости из предыдущего отчёта без обновлений
• Объединять два разных события в один буллет-пункт
• Заполнять пустоты шумом — если нет сильных новостей, дайджест должен быть коротким

✅ ОБЯЗАТЕЛЬНО:
• Каждый факт должен иметь источник из НОВЫХ ДАННЫХ
• Если данные противоречивы — укажи это явно
• Если источник сомнителен (Reddit, слухи) — отметь скепсис
• Нет важных новостей = напиши "За последний час значимых новостей не зафиксировано."

---

<новые_данные>
{all_intelligence_data}
</новые_данные>"""

WORKSPACE = os.path.expanduser(os.environ.get("VIBE_WORKSPACE", "./workspace"))
DIGEST_CHAT = os.environ.get("TELEGRAM_DIGEST_CHAT", "@azalio_tech_summary")
LAST_SUMMARY_PATH = os.path.join(WORKSPACE, "memory", "last_intel_summary.txt")
DEDUP_DB_DIR = os.path.join(WORKSPACE, "memory", "semantic_dedup")
# Append-only audit log of editor decisions: what candidates went in vs what the
# LLM kept. Lets us review periodically whether the AI/ML applied-vs-fundamental
# filter is behaving (council-recommended input/output logging). One JSON object
# per line; never block the digest if it fails to write.
DIGEST_LOG_PATH = os.path.join(WORKSPACE, "memory", "digest_runs.jsonl")
# Sentinel the editor emits for a genuinely quiet hour (see VIBE_PROMPT). When
# the digest is just this line, we suppress the Telegram post entirely instead
# of spamming the channel with a "nothing happened" notice.
NO_NEWS_MARKER = "значимых новостей не зафиксировано"

def load_last_summary():
    try:
        with open(LAST_SUMMARY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        # Missing file is the common case; permission / I/O errors degrade
        # gracefully — the digest just runs without "previous report" context.
        return ""

def save_summary(text):
    os.makedirs(os.path.dirname(LAST_SUMMARY_PATH), exist_ok=True)
    try:
        with open(LAST_SUMMARY_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        # Telegram post already went out — don't crash on a memory write failure.
        print(f"save_summary: failed to write {LAST_SUMMARY_PATH}: {e}")

def is_empty_digest(summary):
    """True if the editor signalled a genuinely quiet hour (the no-news sentinel)
    rather than a real digest. Tolerant of minor LLM wording/punctuation drift,
    but length-gated so a real digest that merely mentions the phrase isn't
    suppressed."""
    norm = " ".join(summary.split()).lower()
    if NO_NEWS_MARKER not in norm:
        return False
    # A real digest always carries bullets and links; the sentinel line has
    # neither. This guards against suppressing a real digest that happens to
    # mention the phrase, with the length cap as a final backstop.
    has_content = "•" in summary or "](" in summary
    return not has_content and len(norm) < 200

def log_digest_run(intelligence, event_signals, summary):
    """Append one JSON record of this run's editor input/output for later review.

    Stores the raw candidate intelligence (incl. the ArXiv / HuggingFace papers
    blocks) alongside the final digest, so we can audit what the applied-vs-
    fundamental AI/ML filter actually dropped vs kept. Best-effort: a logging
    failure must never affect the already-posted digest."""
    try:
        os.makedirs(os.path.dirname(DIGEST_LOG_PATH), exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "intelligence": intelligence,
            "event_signals": event_signals,
            "summary": summary,
        }
        with open(DIGEST_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"log_digest_run: failed to write {DIGEST_LOG_PATH}: {e}")

def format_event_signals(signals):
    if not signals:
        return "Нет сильных source-burst сигналов."

    lines = []
    for signal in signals:
        title = " ".join(str(signal.get("title", "")).split())
        sources = ", ".join(signal.get("sources", [])[:5])
        lines.append(
            f"- event_id={signal.get('cluster_id')}; "
            f"source_burst={signal.get('source_burst')}; "
            f"observations={signal.get('observations')}; "
            f"source_count={signal.get('source_count')}; "
            f"cumulative_item_count={signal.get('cumulative_item_count')}; "
            f"title={title}; sources={sources}; "
            "note=ranking_signal_only"
        )
    return "\n".join(lines)

def main():
    dry_run = "--dry-run" in sys.argv
    workspace = WORKSPACE
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    core = None if dry_run else VibeCore()

    # Initialize event-clustering dedup. Embeddings-first gate with a
    # language-agnostic anchor check in the gray zone (see dedup.py). The auto
    # threshold sits above the measured false-pair ceiling for same-domain tech
    # news (~0.90 on e5-small), so embeddings alone only merge near-duplicates;
    # everything in 0.78–0.92 must also share entities/numbers (anchor overlap).
    dedup = EventDedup(
        db_dir=DEDUP_DB_DIR,
        gray_zone_min=0.78,
        auto_match_threshold=0.92,
        anchor_overlap_min=0.30,
        ttl_hours=168,
        matching_ttl_hours=72,
        max_cluster_size=50,
        dry_run=False,
    )
    collectors = Collectors(workspace, dedup=dedup)

    # 1. Collect all news sources
    all_intelligence_data = ""

    # Existing sources
    reddit = collectors.collect_reddit()
    if reddit: all_intelligence_data += "\n" + reddit
    telegram = collectors.collect_telegram()
    if telegram: all_intelligence_data += "\n" + telegram
    market = collectors.collect_market_news()
    if market: all_intelligence_data += "\n" + market
    ru_news = collectors.collect_ru_news()
    if ru_news: all_intelligence_data += "\n" + ru_news

    # New sources
    hn = collectors.collect_hackernews()
    if hn: all_intelligence_data += "\n" + hn
    global_news = collectors.collect_global_news()
    if global_news: all_intelligence_data += "\n" + global_news
    china_news = collectors.collect_china_news()
    if china_news: all_intelligence_data += "\n" + china_news
    china_tech = collectors.collect_china_tech()
    if china_tech: all_intelligence_data += "\n" + china_tech
    arxiv = collectors.collect_arxiv()
    if arxiv: all_intelligence_data += "\n" + arxiv
    tech = collectors.collect_tech_news()
    if tech: all_intelligence_data += "\n" + tech
    gnews = collectors.collect_google_news()
    if gnews: all_intelligence_data += "\n" + gnews
    science = collectors.collect_science()
    if science: all_intelligence_data += "\n" + science
    infra = collectors.collect_infra_news()
    if infra: all_intelligence_data += "\n" + infra
    security = collectors.collect_security_news()
    if security: all_intelligence_data += "\n" + security
    nvd = collectors.collect_nvd_cves()
    if nvd: all_intelligence_data += "\n" + nvd
    ai_labs = collectors.collect_ai_labs()
    if ai_labs: all_intelligence_data += "\n" + ai_labs
    eng_curated = collectors.collect_eng_curated()
    if eng_curated: all_intelligence_data += "\n" + eng_curated
    newsapi = collectors.collect_newsapi()
    if newsapi: all_intelligence_data += "\n" + newsapi
    finnhub = collectors.collect_finnhub()
    if finnhub: all_intelligence_data += "\n" + finnhub
    hf_papers = collectors.collect_hf_papers()
    if hf_papers: all_intelligence_data += "\n" + hf_papers
    habr = collectors.collect_habr()
    if habr: all_intelligence_data += "\n" + habr
    claude_rel = collectors.collect_claude_releases()
    if claude_rel: all_intelligence_data += "\n" + claude_rel
    gh_trending = collectors.collect_github_trending()
    if gh_trending: all_intelligence_data += "\n" + gh_trending

    # Log dedup stats
    stats = dedup.stats()
    print(f"[DEDUP] Checked: {stats['checked']} | Duplicates: {stats['duplicates']} | Clusters: {stats['total_clusters']} | Items: {stats['total_items']}")
    event_signals = format_event_signals(dedup.event_signals())
    dedup.close()

    # 2. Summary
    if all_intelligence_data.strip():
        last_summary = load_last_summary()
        if not last_summary:
            last_summary = "Это первый отчёт — предыдущего нет."

        prompt = VIBE_PROMPT.format(
            last_summary=last_summary,
            event_signals=event_signals,
            all_intelligence_data=all_intelligence_data,
        )
        if dry_run:
            print("=" * 60)
            print("DRY RUN — full prompt that would go to LLM:")
            print("=" * 60)
            print(prompt)
            print("=" * 60)
            print(f"[DRY RUN] {len(prompt)} chars in prompt. Skipping LLM call and Telegram post.")
            return
        assert core is not None
        summary = core.ask_llm(prompt)
        if not summary:
            # LLM CLI failed or returned empty. The old code fell back to
            # posting `all_intelligence_data` verbatim, which dumped raw
            # collector lines ("[Source] Title - Link: URL", "FINNHUB MARKET
            # NEWS:") to the public channel and polluted last_summary so the
            # next run's "previous report" context was garbage too. Refuse to
            # publish without an LLM-formatted digest; notify the operator on
            # the default chat and leave URL/event dedup state uncommitted so
            # the next run retries with the same items.
            print("ask_llm returned no output — skipping digest post (no raw dump)")
            core.send_tg(
                "LLM CLI вернул пустой ответ — дайджест пропущен.\n"
                "Источники собраны, но не закоммичены: следующий запуск повторит попытку.",
                title="DIGEST FAILURE",
            )
            return

        # Audit the editor's decision (input candidates vs kept digest) before
        # delivery — captured regardless of whether we post. Quiet hours are
        # logged too, so the audit trail shows whether "no news" was a genuinely
        # thin input or the filter over-pruning.
        log_digest_run(all_intelligence_data, event_signals, summary)

        if is_empty_digest(summary):
            # Genuinely quiet hour: the editor found nothing worth posting. Don't
            # spam the channel with a "nothing happened" notice. Advance URL
            # dedup (we *did* adjudicate these candidates, so the same low-value
            # items aren't re-judged every quiet hour), but DON'T overwrite
            # last_summary — keep the last real digest as the dedup anchor for
            # the next run's "previous report" context.
            print("editor returned the no-news sentinel — skipping post (quiet hour)")
            collectors.commit_seen()
            return

        # commit_seen + save_summary only after a successful Telegram delivery.
        # If send fails, pending URL marks stay un-persisted so the next run's
        # URL gate sees the same items as un-seen. Note: EventDedup commits
        # are still eager (issue #2 only defers URL marks), so a rerun may
        # still drop items as semantic duplicates — but they won't be
        # silently lost to a stale sent_posts row.
        if core.send_tg(summary, title="WORLD INTEL BRIEF", chat_id=DIGEST_CHAT):
            collectors.commit_seen()
            save_summary(summary)
        else:
            print("send_tg failed — leaving URL marks uncommitted for retry")

if __name__ == "__main__":
    main()
