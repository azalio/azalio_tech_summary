#!/usr/bin/env python3
import os
import sys

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

---

<предыдущий_отчёт>
{last_summary}
</предыдущий_отчёт>

ПРАВИЛА ДЕДУПЛИКАЦИИ И ОТБОРА:
1. НЕ включай новость, если:
   • Заголовок/суть совпадает с предыдущим отчётом
   • Нет НОВЫХ фактов (цифр, заявлений, решений)
   • Несколько источников пишут об одном событии — объедини в один пункт, используй наиболее надёжный первоисточник
   • Это PR-шум, мелкий анонс, слух или повторный пересказ

2. Включай с пометкой "⬆️ Обновление:", если появились новые факты/данные

3. Отмечай "🆕", если тема появилась впервые

4. Предпочитай первоисточники: official blogs, release notes, papers, incident reports, регуляторы

---

ЗАДАЧА: Аналитический дайджест за последний час.

ПРИОРИТИЗАЦИЯ:
🔴 КРИТИЧНО: Критические CVE/уязвимости, outages крупных провайдеров, прорывы в технологиях, breaking news мирового масштаба
🟠 ВАЖНО: Значимые релизы, deprecations, breaking changes, исследования, тренды в tech/AI
🟡 ФОНОВО: Интересные инсайты, любопытные паттерны

⚠️ ФОКУС: DevOps/SRE, cloud, Kubernetes, observability, security, infra tooling — главный приоритет. AI/ML и наука — второй приоритет. Политику ПРОПУСКАЙ, кроме событий с прямым влиянием на технологии, рынки или глобальную стабильность.

СТРУКТУРА ОТЧЁТА (пропускай пустые секции):
⛔ БЕЗ вступления. Никаких "За прошедший час...", "Зафиксированы события...", "Ниже представлены..." и т.п. Сразу начинай с первой секции (`**🔥 В ФОКУСЕ**` или другой).

**🔥 В ФОКУСЕ** (0-2 события — только если реально критично: outage, critical CVE, прорыв масштаба must-know)

**⚙️ DEVOPS / SRE / CLOUD** (до 4 — Kubernetes, cloud updates, observability, CI/CD, IaC, outages, postmortems)

**🤖 AI / ML / LLM** (до 4 — модели, research, inference/deployment, LLM tooling, GPU/cloud)

**🔐 SECURITY** (до 3 — CVE, active exploitation, supply chain, breaches, cloud/container security)

**🔬 SCIENCE / SPACE / R&D** (до 2 — физика, биотех, космос, значимые papers)

**🌍 POLITICS / SOCIETY** (0-1 — ТОЛЬКО если влияет на technology supply chains, санкции, доступность облаков)

ФОРМАТ КАЖДОГО ПУНКТА:
• Что произошло — факты, цифры, имена (1-2 предложения максимум)
• [Источник](URL)

⛔ НЕ добавляй финальную фразу-комментарий («это важно для…», «специалистам стоит учитывать…», «открывает новые возможности…», «подчёркивает тренд…»). Аудитория сама понимает значимость — твоя задача только донести факт.

---

ФОРМАТИРОВАНИЕ (TELEGRAM):
• Заголовки секций: **жирный**
• Ключевые термины/имена/цифры: **жирный**
• Списки: символ •
• Ссылки: [краткое описание](URL) — ОБЯЗАТЕЛЬНО для каждого факта

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

def main():
    dry_run = "--dry-run" in sys.argv
    workspace = WORKSPACE
    os.makedirs(os.path.join(workspace, "memory"), exist_ok=True)
    core = None if dry_run else VibeCore()

    # Initialize event-clustering dedup
    dedup = EventDedup(
        db_dir=DEDUP_DB_DIR,
        match_threshold=0.80,
        ttl_hours=168,
        matching_ttl_hours=48,
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
    dedup.close()

    # 2. Summary
    if all_intelligence_data.strip():
        last_summary = load_last_summary()
        if not last_summary:
            last_summary = "Это первый отчёт — предыдущего нет."

        prompt = VIBE_PROMPT.format(
            last_summary=last_summary,
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
        result = summary if summary else all_intelligence_data
        # commit_seen + save_summary only after a successful Telegram delivery.
        # If send fails, pending URL marks stay un-persisted so the next run
        # can retry the same items instead of silently dropping them (issue #2).
        if core.send_tg(result, title="WORLD INTEL BRIEF", chat_id=DIGEST_CHAT):
            collectors.commit_seen()
            save_summary(result)
        else:
            print("send_tg failed — leaving URL marks uncommitted for retry")

if __name__ == "__main__":
    main()
