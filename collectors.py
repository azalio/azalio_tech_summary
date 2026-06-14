import hashlib
import os
import json
import re
import subprocess
import sqlite3
import sys
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from dotenv import load_dotenv

from ranking import Candidate

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

class Collectors:
    def __init__(self, workspace, dedup=None):
        self.workspace = workspace
        self.dedup = dedup
        # Default to the interpreter that's running us — when main.py is launched
        # via .venv/bin/python, subprocesses inherit the same venv (so telethon /
        # praw / etc. are importable). PYTHON_BIN env var still overrides.
        self.python_bin = os.environ.get("PYTHON_BIN", sys.executable)
        self.venv_python = os.environ.get("VENV_PYTHON", self.python_bin)
        self.db_path = os.path.join(workspace, "memory/reddit_sent.db")

        # Pending URL-mark queue: collectors call _mark_seen during a run, but
        # the writes don't hit sent_posts until commit_seen() runs — and that
        # only happens after a successful Telegram delivery. Prevents the
        # "marked but never published" data-loss class (see GitHub issue #2).
        self._pending_marks = []
        self._pending_marks_set = set()

        # Structured candidates registered alongside the free-text blob each
        # collector returns. main.py fuses these into a ranked priority index
        # (ranking.py) and derives per-collector item counts for source-health
        # checks (health.py). Populated via _add_candidate at every emit point.
        self.candidates: list[Candidate] = []
        self.source_counts: dict = {}

        # Paths to JSONs
        self.reddit_raw_json = os.path.join(workspace, "memory/reddit_ai_raw.json")
        self.market_news_json = os.path.join(workspace, "memory/market_news_raw.json")
        self.ru_news_json = os.path.join(workspace, "memory/ru_news_latest.json")
        self.telegram_raw_json = os.path.join(workspace, "memory/telegram_raw.json")
        self.x_raw_json = os.path.join(workspace, "memory/x_raw.json")

        # Paths to fetcher sub-scripts. Reddit + Telegram digests ship with
        # this repo; the others are optional external scripts (collector
        # silently skips if path is unset or file missing).
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        self.reddit_script = os.environ.get(
            "REDDIT_SCRIPT", os.path.join(repo_dir, "standalone_reddit_digest.py")
        )
        self.telegram_script = os.environ.get(
            "TELEGRAM_DIGEST_SCRIPT",
            os.path.join(repo_dir, "standalone_telegram_digest.py"),
        )
        self.x_script = os.environ.get(
            "X_DIGEST_SCRIPT", os.path.join(repo_dir, "standalone_x_digest.py")
        )
        self.ru_news_script = os.environ.get("RU_NEWS_SCRIPT", "")
        self.market_news_script = os.environ.get("MARKET_NEWS_SCRIPT", "")

        # API keys (optional, from env)
        self.finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
        self.newsapi_key = os.environ.get("NEWSAPI_KEY", "")
        self.nvd_key = os.environ.get("NVD_API_KEY", "")

        self._init_db()
        self._cleanup_seen()

    def _init_db(self):
        """Ensure sent_posts table exists (URL dedup layer)."""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sent_posts (
                    url TEXT PRIMARY KEY,
                    subreddit TEXT,
                    sent_at TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  _init_db error: {e}")

    _RECAP_RE = re.compile(
        r'\bweek in review\b|\bweekly (?:roundup|recap|digest|summary|news)\b|\bthis week in\b|\bin review:',
        re.IGNORECASE,
    )

    def _is_semantic_dup(self, title, source, url="", description=""):
        """Check semantic dedup. Returns True if duplicate (or recap), False if new."""
        # Recap/roundup articles re-cover already-reported stories with vague
        # multi-topic titles that defeat embedding+jaccard dedup. Drop them upfront.
        if self._RECAP_RE.search(title or ""):
            return True
        if not self.dedup:
            return False
        return not self.dedup.check_and_add(title, source, url, description)

    @staticmethod
    def _normalize_url(url):
        """Normalize URL: https, lowercase host, strip trailing /, remove tracking params."""
        if not url or not url.startswith(("http://", "https://")):
            return url
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        skip = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                "utm_content", "ref", "source", "fbclid", "gclid"}
        qs = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in qs.items() if k.lower() not in skip}
        query = urlencode(sorted(filtered.items()), doseq=True) if filtered else ""
        return urlunparse(("https", netloc, path, "", query, ""))

    def _add_candidate(self, collector, source, title, url, line="",
                       engagement=None, author=None, freshness=0.5, cvss=None):
        """Register a structured candidate for ranking + health tracking.

        Called once per emitted item, in parallel with the text line that goes
        into the LLM blob. `collector` is the section/collector name (the key
        into ranking weights and health baselines); `source` is the concrete
        per-item feed/subreddit used for the diversity cap. Never raises — a
        bookkeeping error must not break collection."""
        try:
            self.candidates.append(Candidate(
                collector=collector, source=source, title=title or "", url=url or "",
                line=line, engagement=engagement, author=author,
                freshness=freshness, cvss=cvss,
            ))
            self.source_counts[collector] = self.source_counts.get(collector, 0) + 1
        except Exception as e:
            print(f"  _add_candidate error: {e}")

    @staticmethod
    def _freshness_from_struct_time(pub, max_age_days):
        """Map an RSS published_parsed time tuple to a 0..1 recency score.

        1.0 = just published, decaying linearly to 0.0 at max_age_days. Returns
        0.5 (neutral) when the entry carries no usable timestamp."""
        if not pub:
            return 0.5
        import time as _time
        try:
            age = _time.time() - _time.mktime(pub)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return 0.5
        span = max_age_days * 86400
        if span <= 0:
            return 0.5
        return max(0.0, min(1.0, 1.0 - age / span))

    def _is_seen(self, url):
        if not url: return False
        url = self._normalize_url(url)
        if url in self._pending_marks_set:
            return True
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM sent_posts WHERE url = ?', (url,))
            res = cursor.fetchone() is not None
            conn.close()
            return res
        except Exception as e:
            print(f"  _is_seen error: {e}")
            return False

    def _mark_seen(self, url, source="generic"):
        """Queue a URL to be persisted to sent_posts after digest delivery.

        Within the current run, _is_seen() returns True for queued URLs (via
        _pending_marks_set), so callers see the same "seen" semantics as before.
        commit_seen() writes to SQLite only after Telegram delivery succeeds.
        """
        if not url: return
        url = self._normalize_url(url)
        if url in self._pending_marks_set:
            return
        self._pending_marks.append((url, source))
        self._pending_marks_set.add(url)

    def commit_seen(self):
        """Persist pending URL marks to sent_posts. On success the in-memory
        queue is cleared so a second call is a no-op; on failure the queue is
        preserved so a caller can retry (INSERT OR IGNORE keeps it safe to
        re-attempt the same rows)."""
        if not self._pending_marks:
            return
        now_str = datetime.now().isoformat()
        rows = [(url, source, now_str) for url, source in self._pending_marks]
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executemany(
                'INSERT OR IGNORE INTO sent_posts (url, subreddit, sent_at) VALUES (?, ?, ?)',
                rows,
            )
            conn.commit()
            # total_changes reflects rows actually inserted; INSERT OR IGNORE
            # skips existing PKs so this can be less than len(rows).
            written = conn.total_changes
        except Exception as e:
            print(f"  commit_seen error: {e}")
            return
        finally:
            if conn is not None:
                conn.close()
        print(f"  commit_seen: wrote {written}/{len(rows)} URL marks "
              f"({len(rows) - written} already present)")
        self._pending_marks.clear()
        self._pending_marks_set.clear()

    def _cleanup_seen(self, ttl_days=30):
        """Remove sent_posts entries older than ttl_days."""
        try:
            conn = sqlite3.connect(self.db_path)
            cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
            cur = conn.execute('DELETE FROM sent_posts WHERE sent_at < ?', (cutoff,))
            if cur.rowcount:
                print(f"  Cleaned up {cur.rowcount} old sent_posts entries")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  _cleanup_seen error: {e}")

    def _read_and_clear(self, path):
        if not os.path.exists(path): return []
        # Explicit utf-8 — fetcher scripts write with ensure_ascii=False, so on
        # locales whose default encoding isn't utf-8 a bare open() would raise
        # UnicodeDecodeError on Cyrillic/emoji posts and silently drop them.
        # OSError covers permission / I/O / broken symlink so the hourly cron
        # keeps running on other sources instead of crashing the whole digest.
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"  _read_and_clear: failed to read {path}: {e}")
            data = []
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
        except OSError as e:
            print(f"  _read_and_clear: failed to truncate {path}: {e}")
        return data

    def _fetch_rss(self, feeds, source_label, max_per_feed=5, max_total=20, max_age_days=7):
        """Generic RSS fetcher with URL + semantic dedup. Skips entries older than max_age_days."""
        import time as _time
        content = f"{source_label}:\n"
        count = 0
        cutoff_ts = _time.time() - max_age_days * 86400
        for name, url in feeds.items():
            try:
                # Fetch with an explicit timeout instead of letting feedparser
                # fetch via urllib — feedparser.parse(url) has NO timeout and a
                # single stalled feed hangs the whole pipeline indefinitely.
                resp = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 vibe-intel/1.0"},
                    timeout=15,
                )
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                per_feed = 0
                for entry in feed.entries:
                    if count >= max_total:
                        break
                    # Drop archive entries: feeds like kubernetes.io/feed.xml ship 50 historical
                    # posts, and once recent ones are in sent_posts the collector starts surfacing
                    # year-old releases as "new".
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        try:
                            if _time.mktime(pub) < cutoff_ts:  # type: ignore[arg-type]
                                continue
                        except (TypeError, ValueError, OverflowError):
                            pass
                    link = str(entry.get("link") or "")
                    if self._is_seen(link):
                        continue
                    self._mark_seen(link, f"{source_label}:{name}")
                    title = str(entry.get("title") or "").strip()
                    if len(title) < 15:
                        continue
                    summary = str(entry.get("summary") or "")[:300].strip()
                    if self._is_semantic_dup(title, f"{source_label}:{name}", link, summary):
                        continue
                    line = f"[{name}] {title} - Link: {link}\n"
                    content += line
                    if summary:
                        content += f"  {summary}\n"
                    # RSS feeds carry no engagement metric; freshness from the
                    # entry's publish date is the ranking signal here.
                    self._add_candidate(
                        source_label, name, title, link, line=line.strip(),
                        freshness=self._freshness_from_struct_time(pub, max_age_days),
                    )
                    count += 1
                    per_feed += 1
                    if per_feed >= max_per_feed:
                        break
            except Exception as e:
                print(f"  RSS error {name}: {e}")
        return content if count > 0 else ""

    # ── Existing collectors ──────────────────────────────────────

    def collect_reddit(self):
        print("Fetching Reddit...")
        try: subprocess.run([self.python_bin, self.reddit_script], check=True, timeout=3600)
        except: pass
        data = self._read_and_clear(self.reddit_raw_json)
        if not data: return ""

        content = "REDDIT FEED:\n"
        for p in data:
            title = p.get('title', '')
            url = p.get('url', 'No link')
            if self._is_seen(url):
                continue
            sub = p.get('subreddit', '?')
            source = f"Reddit:r/{sub}"
            self._mark_seen(url, source)
            desc = p.get('text', '')[:300]
            if self._is_semantic_dup(title, source, url, desc):
                continue
            # Surface the real engagement the fetcher already collected (score,
            # comment count, upvote ratio) so the editor and ranking weigh what
            # the community actually engaged with, not just front-page presence.
            score = p.get('score') or 0
            num_comments = p.get('num_comments') or 0
            ratio = p.get('upvote_ratio')
            metrics = []
            if score:
                metrics.append(f"{score} upvotes")
            if num_comments:
                metrics.append(f"{num_comments} comments")
            if ratio:
                metrics.append(f"{int(ratio * 100)}% upvoted")
            metric_str = f" ({', '.join(metrics)})" if metrics else ""
            content += f"\n[r/{sub}] {title}{metric_str}\n"
            content += f"Link: {url}\n"
            if p.get('text'): content += f"Context: {p['text'][:400]}\n"
            # Top comment as a community-signal snippet (the fetcher sorts by top).
            top = p.get('top_comments') or []
            if top:
                c0 = top[0]
                body = " ".join((c0.get('body') or "").split())[:200]
                if body:
                    content += f"Top comment ({c0.get('score', 0)} pts): {body}\n"
            self._add_candidate(
                "Reddit", f"r/{sub}", title, url,
                line=f"[r/{sub}] {title}{metric_str} - Link: {url}",
                engagement=float(score) if score else None, freshness=0.8,
            )
        return content

    def collect_telegram(self):
        """Telegram channels via Telethon (MTProto user account).

        Channels list comes from TELEGRAM_CHANNELS env var. Standalone script
        pulls up to 10 latest text posts per channel; this method dedupes and
        formats them for the digest prompt. Silently disabled if MTProto creds
        or channel list are missing.
        """
        if not (os.environ.get("TELEGRAM_API_ID")
                and os.environ.get("TELEGRAM_API_HASH")
                and os.environ.get("TELEGRAM_CHANNELS")):
            return ""
        print("Fetching Telegram channels...")
        try:
            subprocess.run([self.python_bin, self.telegram_script], check=True, timeout=600)
        except Exception as e:
            # Bail without reading the JSON — otherwise stale data from a prior
            # successful run would leak into this digest.
            print(f"  Telegram fetcher error: {e}")
            return ""
        data = self._read_and_clear(self.telegram_raw_json)
        if not data: return ""

        # Markers in Russian so the LLM (which writes the Russian digest) reads them
        # natively. "[фото]/[видео]" flag posts whose visual content the LLM can't see.
        media_ru = {"photo": "фото", "video": "видео", "gif": "gif",
                    "voice": "голос", "audio": "аудио", "file": "файл"}

        content = "TELEGRAM CHANNELS:\n"
        count = 0
        for p in data:
            url = p.get("url", "")
            if not url or self._is_seen(url):
                continue
            channel = p.get("channel", "?")
            source = f"Telegram:@{channel}"
            self._mark_seen(url, source)
            title = (p.get("title") or "").strip() or "(no title)"
            text = p.get("text", "") or ""
            desc = text[:300]
            if self._is_semantic_dup(title, source, url, desc):
                continue
            metrics = []
            if p.get("views"): metrics.append(f"{p['views']} views")
            if p.get("reactions"): metrics.append(f"{p['reactions']} reactions")
            if p.get("forwards"): metrics.append(f"{p['forwards']} forwards")
            media_tags = "".join(f"[{media_ru.get(m, m)}]" for m in p.get("media") or [])
            prefix = f"{media_tags} " if media_tags else ""
            content += f"\n[@{channel}] {prefix}{title}\n"
            if metrics:
                content += f"  ({', '.join(metrics)})\n"
            content += f"Link: {url}\n"
            if text and text != title:
                content += f"Context: {text[:400]}\n"
            self._add_candidate(
                "Telegram", f"@{channel}", title, url,
                line=f"[@{channel}] {title} - Link: {url}",
                engagement=float(p.get("views") or 0) or None, freshness=0.8,
            )
            count += 1
        return content if count > 0 else ""

    def _x_enabled(self):
        """True if any X source is configured (sources file or X_HANDLES env).

        Mirrors x_acquire.load_sources resolution without importing it, so an
        unconfigured deployment skips the subprocess entirely (no health noise,
        no wasted run) — same gating pattern as the Telegram collector."""
        if os.environ.get("X_HANDLES", "").strip():
            return True
        env_sources = os.environ.get("X_SOURCES", "")
        if env_sources and os.path.exists(env_sources):
            return True
        default = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "x_sources.yaml")
        return os.path.exists(default)

    def collect_x(self):
        """X/Twitter via the free/low-cost acquisition cascade (standalone_x_digest.py).

        The cascade reads each source through the first working provider —
        RSS/blog mirrors, Bluesky public API, self-hosted RSSHub, Nitter, … (see
        docs/x_acquisition.md). The subprocess writes x_raw.json; we apply the
        digest's own URL + semantic dedup here, exactly like the Telegram/Reddit
        collectors. Silently disabled when no X sources are configured, and any
        fetcher failure degrades to an empty section instead of breaking the run.
        """
        if not self._x_enabled():
            return ""
        print("Fetching X/Twitter (cascade)...")
        try:
            subprocess.run([self.python_bin, self.x_script], check=True, timeout=600)
        except Exception as e:
            # Don't read x_raw.json on failure — the script truncates it up front,
            # so stale data can't leak, and a partial file is best skipped.
            print(f"  X fetcher error: {e}")
            return ""
        data = self._read_and_clear(self.x_raw_json)
        if not data:
            return ""

        content = "X / TWITTER:\n"
        count = 0
        for p in data:
            url = p.get("url") or ""
            # Items with no canonical URL (e.g. email notifications) can't be
            # URL-deduped; skip them rather than risk re-posting every hour.
            if not url or self._is_seen(url):
                continue
            author = p.get("author") or "?"
            source = f"X:{author}"
            self._mark_seen(url, source)
            text = (p.get("text") or "").strip()
            title = ((p.get("title") or text or "").strip())[:200] or "(no title)"
            if self._is_semantic_dup(title, source, url, text[:300]):
                continue
            provider = p.get("provider") or "?"
            content += f"\n[X {author}] {title} (via {provider})\n"
            content += f"Link: {url}\n"
            if text and text != title:
                content += f"Context: {text[:400]}\n"
            eng = p.get("engagement")
            self._add_candidate(
                "X", author, title, url,
                line=f"[X {author}] {title} - Link: {url}",
                engagement=float(eng) if eng else None, freshness=0.8,
            )
            count += 1
        return content if count > 0 else ""

    def collect_market_news(self):
        if not self.market_news_script:
            return ""
        print("Fetching Market News...")
        try: subprocess.run([self.venv_python, self.market_news_script], check=True, timeout=180)
        except: pass

        raw_data = self._read_and_clear(self.market_news_json)
        if not raw_data: return ""

        content = "MARKET NEWS (Yahoo/Finance):\n"
        count = 0
        for n in raw_data:
            link = n.get('link')
            if self._is_seen(link): continue
            self._mark_seen(link, "market")
            title = n.get('title', '')
            source_name = n.get('source', 'unknown')
            if self._is_semantic_dup(title, f"market:{source_name}", link or ""):
                continue
            content += f"- {title} ({source_name}) - Link: {link}\n"
            self._add_candidate("MARKET NEWS", source_name, title, link or "",
                                line=f"- {title} ({source_name}) - Link: {link}")
            count += 1
            if count >= 20: break
        return content if count > 0 else ""

    def collect_ru_news(self):
        if not self.ru_news_script:
            return ""
        print("Fetching RU News...")
        try: subprocess.run([self.venv_python, self.ru_news_script], check=True, timeout=180)
        except: pass

        raw_data = self._read_and_clear(self.ru_news_json)
        if not raw_data: return ""

        content = "RUSSIAN NEWS FEED:\n"
        count = 0
        for provider in raw_data:
            source = provider.get('source', 'Unknown')
            for story in provider.get('stories', []):
                link = story.get('link')
                if self._is_seen(link): continue
                self._mark_seen(link, f"ru_news:{source}")
                title = story.get('title', '')
                desc = story.get('summary', '')[:300]
                if self._is_semantic_dup(title, f"ru_news:{source}", link or "", desc):
                    continue
                content += f"[{source}] {title} - Link: {link}\n"
                if story.get('summary'): content += f"  {story['summary'][:200]}\n"
                self._add_candidate("RUSSIAN NEWS FEED", source, title, link or "",
                                    line=f"[{source}] {title} - Link: {link}")
                count += 1
                if count >= 5: break
        return content if count > 0 else ""

    # ── New collectors ───────────────────────────────────────────

    def collect_hf_papers(self):
        """HuggingFace Daily Papers with upvotes >= 100."""
        print("Fetching HuggingFace Daily Papers...")
        try:
            resp = requests.get("https://huggingface.co/api/daily_papers", timeout=15)
            resp.raise_for_status()
            papers = resp.json()
        except Exception as e:
            print(f"  HF Papers error: {e}")
            return ""

        content = "HUGGINGFACE DAILY PAPERS (upvotes>=100):\n"
        count = 0
        for entry in papers:
            p = entry.get("paper", {})
            upvotes = p.get("upvotes", 0)
            if upvotes < 100:
                continue
            arxiv_id = p.get("id", "")
            paper_url = f"https://huggingface.co/papers/{arxiv_id}"
            if self._is_seen(paper_url):
                continue
            self._mark_seen(paper_url, "HFPapers")
            title = p.get("title", "").strip()
            desc = p.get("ai_summary") or p.get("summary", "")
            desc = desc[:300].strip()
            if self._is_semantic_dup(title, "HFPapers", paper_url, desc):
                continue
            stars = p.get("githubStars", 0)
            extra = f", {stars} GH stars" if stars else ""
            content += f"- {title} ({upvotes} upvotes{extra}) - Link: {paper_url}\n"
            self._add_candidate(
                "HFPapers", "HuggingFace", title, paper_url,
                line=f"- {title} ({upvotes} upvotes{extra}) - Link: {paper_url}",
                engagement=float(upvotes), freshness=0.8,
            )
            count += 1
            if count >= 15:
                break
        return content if count > 0 else ""

    def collect_hackernews(self):
        """Hacker News front page via Algolia API."""
        print("Fetching Hacker News...")
        try:
            url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as e:
            print(f"  HN error: {e}")
            return ""

        content = "HACKER NEWS (front page):\n"
        count = 0
        for h in hits:
            hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
            story_url = h.get("url") or hn_url
            if self._is_seen(hn_url) or self._is_seen(story_url):
                continue
            self._mark_seen(hn_url, "HackerNews")
            if story_url != hn_url:
                self._mark_seen(story_url, "HackerNews")
            title = h.get("title", "").strip()
            points = h.get("points", 0)
            if self._is_semantic_dup(title, "HackerNews", story_url):
                continue
            content += f"- {title} ({points} pts) - Link: {story_url}\n"
            self._add_candidate(
                "HackerNews", "Hacker News", title, story_url,
                line=f"- {title} ({points} pts) - Link: {story_url}",
                engagement=float(points) if points else None, freshness=0.85,
            )
            count += 1
            if count >= 20:
                break
        return content if count > 0 else ""

    def collect_global_news(self):
        """BBC Tech, MIT Tech Review, IEEE Spectrum, The Register RSS."""
        print("Fetching Global News RSS...")
        feeds = {
            "BBC Tech": "http://feeds.bbci.co.uk/news/technology/rss.xml",
            "MIT Tech Review": "https://www.technologyreview.com/feed/",
            "IEEE Spectrum": "https://spectrum.ieee.org/feeds/feed.rss",
            "The Register": "https://www.theregister.com/headlines.atom",
        }
        return self._fetch_rss(feeds, "GLOBAL NEWS", max_per_feed=3, max_total=10)

    def collect_arxiv(self):
        """ArXiv AI/ML/agents/code papers. cs.MA covers multi-agent systems and
        cs.CL/cs.SE catch LLM-agent and code-agent papers that don't land in cs.AI.
        cs.DC (distributed/parallel/cluster) surfaces the applied infra-relevant
        work — distributed inference, GPU scheduling, serving, fault tolerance —
        that the DevOps/SRE reader cares about more than fundamental ML theory."""
        print("Fetching ArXiv AI/ML...")
        feeds = {
            "cs.AI": "http://export.arxiv.org/rss/cs.AI",
            "cs.LG": "http://export.arxiv.org/rss/cs.LG",
            "cs.MA": "http://export.arxiv.org/rss/cs.MA",
            "cs.CL": "http://export.arxiv.org/rss/cs.CL",
            "cs.SE": "http://export.arxiv.org/rss/cs.SE",
            "cs.DC": "http://export.arxiv.org/rss/cs.DC",
        }
        return self._fetch_rss(feeds, "ARXIV AI/ML PAPERS", max_per_feed=5, max_total=30)

    def collect_tech_news(self):
        """TechCrunch, Ars Technica, Wired, The Verge RSS."""
        print("Fetching Tech News RSS...")
        feeds = {
            "TechCrunch": "https://techcrunch.com/feed/",
            "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
            "The Verge": "https://www.theverge.com/rss/index.xml",
            "Wired": "https://www.wired.com/feed/rss",
        }
        return self._fetch_rss(feeds, "TECH NEWS", max_per_feed=5, max_total=20)

    def collect_china_news(self):
        """Chinese English-language outlets: CGTN, China Daily, SCMP, Global Times."""
        print("Fetching China News RSS...")
        feeds = {
            "CGTN World": "https://www.cgtn.com/subscribe/rss/section/world.xml",
            "CGTN China": "https://www.cgtn.com/subscribe/rss/section/china.xml",
            "CGTN Business": "https://www.cgtn.com/subscribe/rss/section/business.xml",
            "China Daily World": "https://www.chinadaily.com.cn/rss/world_rss.xml",
            "China Daily China": "https://www.chinadaily.com.cn/rss/china_rss.xml",
            "SCMP": "https://www.scmp.com/rss/91/feed/",
            "Global Times": "https://www.globaltimes.cn/rss/outbrain.xml",
        }
        return self._fetch_rss(feeds, "CHINA NEWS", max_per_feed=3, max_total=12)

    def collect_china_tech(self):
        """Chinese tech media — English (CGTN Tech, SCMP Tech, Pandaily, TechNode) and
        zh-language (36Kr, IT之家, 少数派, 雷锋网). Non-English titles get passed to
        the LLM which handles translation in the digest step."""
        print("Fetching China Tech RSS...")
        feeds = {
            "CGTN Tech": "https://www.cgtn.com/subscribe/rss/section/tech-sci.xml",
            "SCMP Tech": "https://www.scmp.com/rss/36/feed/",
            "Pandaily": "https://pandaily.com/feed/",
            "TechNode": "https://technode.com/feed/",
            "36Kr": "https://36kr.com/feed",
            "ITHome": "https://www.ithome.com/rss/",
            "Sspai": "https://sspai.com/feed",
            "LeiPhone": "https://www.leiphone.com/feed/",
        }
        return self._fetch_rss(feeds, "CHINA TECH", max_per_feed=3, max_total=12)

    def collect_google_news(self):
        """Google News RSS for targeted topics."""
        print("Fetching Google News RSS...")
        feeds = {
            "AI": "https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
            "SpaceX/NASA": "https://news.google.com/rss/search?q=SpaceX+OR+NASA+OR+ESA&hl=en-US&gl=US&ceid=US:en",
        }
        return self._fetch_rss(feeds, "GOOGLE NEWS", max_per_feed=5, max_total=15)

    def collect_science(self):
        """NASA, Nature, Space News, Astronomy/Astrophysics RSS."""
        print("Fetching Science/Space/Astro RSS...")
        feeds = {
            "NASA": "https://www.nasa.gov/news-release/feed/",
            "SpaceNews": "https://spacenews.com/feed/",
            "Nature": "https://www.nature.com/nature.rss",
            "ScienceDaily": "https://www.sciencedaily.com/rss/all.xml",
            "Phys.org Astro": "https://phys.org/rss-feed/space-news/astronomy/",
            "Astronomy.com": "https://www.astronomy.com/feed/",
            "ESO": "https://feeds.feedburner.com/EsoTopNews",
            "ESA Science": "https://www.esa.int/rssfeed/Our_Activities/Space_Science",
            "Chandra X-ray": "https://chandra.si.edu/blog/rss.xml",
        }
        astro_papers = {
            "astro-ph": "http://export.arxiv.org/rss/astro-ph",
        }
        content = self._fetch_rss(feeds, "SCIENCE & SPACE", max_per_feed=5, max_total=20)
        papers = self._fetch_rss(astro_papers, "ARXIV ASTROPHYSICS", max_per_feed=8, max_total=8)
        if papers and content:
            return content + "\n" + papers
        return content or papers or ""

    def collect_habr(self):
        """Habr top daily articles with score >= 100."""
        print("Fetching Habr top daily...")
        try:
            resp = requests.get(
                "https://habr.com/kek/v2/articles/",
                params={"sort": "rating", "period": "daily", "score": "100",
                        "fl": "ru", "hl": "ru"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("publicationIds", [])
            articles_data = data.get("publicationRefs", {})
        except Exception as e:
            print(f"  Habr error: {e}")
            return ""

        content = "HABR (top daily, score>=100):\n"
        count = 0
        for aid in articles:
            art = articles_data.get(str(aid), {})
            if not art:
                continue
            habr_url = f"https://habr.com/ru/articles/{aid}/"
            if self._is_seen(habr_url):
                continue
            self._mark_seen(habr_url, "Habr")
            title = art.get("titleHtml", "").strip()
            stats = art.get("statistics", {})
            score = stats.get("score", 0)
            if score < 100:
                continue
            lead = art.get("leadData", {}).get("textHtml", "")
            desc = re.sub(r'<[^>]+>', '', lead)[:300].strip()
            if self._is_semantic_dup(title, "Habr", habr_url, desc):
                continue
            content += f"- {title} ({score} pts) - Link: {habr_url}\n"
            self._add_candidate(
                "Habr", "Habr", title, habr_url,
                line=f"- {title} ({score} pts) - Link: {habr_url}",
                engagement=float(score) if score else None, freshness=0.8,
            )
            count += 1
            if count >= 10:
                break
        return content if count > 0 else ""

    def collect_github_trending(self, top_n=5, min_stars_today=200, readme_chars=400):
        """GitHub Trending daily, top by stars-today. Adds README excerpt for context."""
        print("Fetching GitHub Trending...")
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print("  GitHub Trending skipped: bs4 not installed")
            return ""
        try:
            resp = requests.get(
                "https://github.com/trending",
                headers={"User-Agent": "Mozilla/5.0 vibe-intel/1.0"},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"  GitHub Trending error: {e}")
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.select("article.Box-row")

        content = f"GITHUB TRENDING (today, stars>={min_stars_today}):\n"
        count = 0
        for art in articles:
            if count >= top_n:
                break
            link = art.select_one("h2 a")
            if not link:
                continue
            href = link.get("href")
            path = href.strip() if isinstance(href, str) else ""
            if not path or not path.startswith("/"):
                continue
            owner_repo = re.sub(r"\s+", "", link.get_text())
            repo_url = f"https://github.com{path}"

            stars_today_el = art.select_one(".float-sm-right")
            stars_today = 0
            if stars_today_el:
                m = re.search(r"([\d,]+)\s+stars\s+today", stars_today_el.get_text())
                if m:
                    stars_today = int(m.group(1).replace(",", ""))
            if stars_today < min_stars_today:
                continue

            desc_el = art.select_one("p")
            desc = desc_el.get_text(strip=True) if desc_el else ""

            lang_el = art.select_one('[itemprop="programmingLanguage"]')
            lang = lang_el.get_text(strip=True) if lang_el else ""

            if self._is_seen(repo_url):
                continue
            self._mark_seen(repo_url, "GitHubTrending")

            title = f"{owner_repo}: {desc}" if desc else owner_repo
            if self._is_semantic_dup(title, "GitHubTrending", repo_url, desc):
                continue

            readme = self._fetch_github_readme(path.strip("/"), max_chars=readme_chars)

            line = f"- [{owner_repo}] {desc}"
            if lang:
                line += f" | {lang}"
            line += f" | {stars_today} stars today - Link: {repo_url}\n"
            if readme:
                line += f"  README: {readme}\n"
            content += line
            self._add_candidate(
                "GitHubTrending", owner_repo, title, repo_url,
                line=f"[{owner_repo}] {desc} - Link: {repo_url}",
                engagement=float(stars_today) if stars_today else None, freshness=0.7,
            )
            count += 1

        return content if count > 0 else ""

    def _fetch_github_readme(self, owner_repo, max_chars=400):
        """Fetch README via raw.githubusercontent.com (no API limit), strip markdown, truncate."""
        text = ""
        for name in ("README.md", "Readme.md", "readme.md", "README.rst", "README.txt", "README"):
            url = f"https://raw.githubusercontent.com/{owner_repo}/HEAD/{name}"
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 vibe-intel/1.0"},
                    timeout=10,
                )
            except Exception:
                continue
            if resp.status_code == 200 and resp.text:
                text = resp.text
                break
        if not text:
            return ""

        # Strip markdown noise
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)  # code blocks
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)  # HTML comments
        text = re.sub(r"<[^>]+>", " ", text)  # HTML tags
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)  # images
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links → text
        text = re.sub(r"^[#>*\-]+\s*", "", text, flags=re.MULTILINE)  # leading md tokens
        text = re.sub(r"[*_`]", "", text)  # inline emphasis
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "…"
        return text

    _CLAUDE_XML_TAG_RE = re.compile(r'<\s*([A-Za-z][A-Za-z0-9_-]*)\s*/?\s*>')
    _CLAUDE_MODEL_RE = re.compile(
        r'\bClaude\s+(?:Opus|Sonnet|Haiku)(?:\s+\d+(?:\.\d+)*)?\b',
        re.IGNORECASE,
    )

    def _claude_release_group_key(self, text):
        """Return a stable key/display pair for product-specific release items."""
        m = self._CLAUDE_XML_TAG_RE.search(text or "")
        if m:
            tag = m.group(1)
            return (f"tag:{tag.lower()}", f"<{tag} />")

        m = self._CLAUDE_MODEL_RE.search(text or "")
        if m:
            label = re.sub(r'\s+', ' ', m.group(0)).strip()
            return (f"model:{label.lower()}", label)

        return None

    def collect_claude_releases(self):
        """Anthropic Claude Platform release notes (last 48h)."""
        print("Fetching Claude Platform release notes...")
        md_url = "https://platform.claude.com/docs/en/release-notes/overview.md"
        try:
            resp = requests.get(md_url, timeout=15)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            print(f"  Claude releases error: {e}")
            return ""

        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

        # Split by "### Month Day, Year" headers
        blocks = re.split(r'(?=^### )', text, flags=re.MULTILINE)

        content = "CLAUDE PLATFORM RELEASES:\n"
        count = 0
        date_pattern = re.compile(
            r'(January|February|March|April|May|June|July|August|'
            r'September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})'
        )
        for block in blocks:
            if not block.startswith('### '):
                continue
            m = date_pattern.search(block)
            if not m:
                continue
            try:
                date_str = f"{m.group(1)} {m.group(2)} {m.group(3)}"
                entry_date = datetime.strptime(date_str, "%B %d %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if entry_date < cutoff:
                break  # entries are reverse-chronological

            # Extract top-level bullet items (lines starting with "- ")
            items = re.findall(r'^- (.+)', block, re.MULTILINE)
            groups = []
            group_by_key = {}
            for item in items:
                # Strip markdown links to plain text for dedup
                plain = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', item).strip()
                plain = re.sub(r'[*`]', '', plain).strip()
                if not plain:
                    continue
                item_key = hashlib.md5(f"{date_str}:{plain}".encode()).hexdigest()
                item_url = f"claude-release:{item_key}"
                if self._is_seen(item_url):
                    continue

                group_key = self._claude_release_group_key(plain)
                if group_key:
                    key, label = group_key
                    stable_key = f"{date_str}:{key}"
                else:
                    key, label = item_url, ""
                    stable_key = item_url

                group = group_by_key.get(stable_key)
                if group is None:
                    group = {"key": key, "label": label, "items": []}
                    group_by_key[stable_key] = group
                    groups.append(group)
                group["items"].append((plain, item_url))

            for group in groups:
                entries = group["items"]
                if group["label"] and len(entries) > 1:
                    joined = "; ".join(plain.rstrip(".") for plain, _ in entries)
                    plain = f"{group['label']} updates: {joined}."
                    dedup_key = hashlib.md5(
                        f"{date_str}:{group['key']}:{plain}".encode()
                    ).hexdigest()
                    item_url = f"claude-release-group:{dedup_key}"
                else:
                    plain, item_url = entries[0]

                for _, original_url in entries:
                    self._mark_seen(original_url, "ClaudePlatform")
                if self._is_semantic_dup(plain, "ClaudePlatform", item_url):
                    continue
                content += f"- {plain}\n"
                self._add_candidate(
                    "ClaudePlatform", "Claude Release Notes", plain,
                    "https://platform.claude.com/docs/en/release-notes/overview",
                    line=f"- {plain}", freshness=0.9,
                )
                count += 1

        return content if count > 0 else ""

    def collect_newsapi(self):
        """NewsAPI: AI, DevOps, Cybersecurity news."""
        if not self.newsapi_key:
            return ""
        print("Fetching NewsAPI...")
        queries = {
            "AI": '"artificial intelligence" OR "large language model" OR "generative AI"',
            "DevOps": 'kubernetes OR terraform OR "cloud native"',
            "Security": 'cybersecurity OR "data breach" OR "zero-day"',
        }
        skip_domains = {"pypi.org", "cgpersia.com", "substack.com"}
        content = "NEWSAPI:\n"
        count = 0
        for label, q in queries.items():
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": q,
                        "pageSize": 10,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "apiKey": self.newsapi_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
            except Exception as e:
                print(f"  NewsAPI {label} error: {e}")
                continue
            for art in articles:
                link = art.get("url", "")
                if any(d in link for d in skip_domains):
                    continue
                if self._is_seen(link):
                    continue
                self._mark_seen(link, f"NewsAPI:{label}")
                title = art.get("title", "").strip()
                if not title or title == "[Removed]":
                    continue
                source_name = art.get("source", {}).get("name", "")
                desc = art.get("description") or ""
                desc = desc[:300].strip()
                if self._is_semantic_dup(title, f"NewsAPI:{label}", link, desc):
                    continue
                content += f"[{label}:{source_name}] {title} - Link: {link}\n"
                self._add_candidate("NewsAPI", source_name or label, title, link,
                                    line=f"[{label}:{source_name}] {title} - Link: {link}")
                count += 1
                if count >= 20:
                    return content
        return content if count > 0 else ""

    def collect_infra_news(self):
        """Kubernetes, CNCF, AWS, GCP, Azure, Cloudflare, HashiCorp, Datadog,
        Grafana, Last Week in AWS, CISA — DevOps/SRE/cloud. The New Stack +
        Elastic + AWS DevOps cover the AI-agents-in-infra angle. GitHub Blog
        catches platform changes (Copilot/Actions/GHAS) and Netflix Tech Blog
        feeds in distributed-systems / observability deep-dives."""
        print("Fetching Infra/DevOps RSS...")
        feeds = {
            "Kubernetes": "https://kubernetes.io/feed.xml",
            "CNCF": "https://www.cncf.io/feed/",
            "AWS News": "https://aws.amazon.com/blogs/aws/feed/",
            "AWS DevOps": "https://aws.amazon.com/blogs/devops/feed/",
            "Google Cloud": "https://cloudblog.withgoogle.com/rss/",
            "Azure": "https://azure.microsoft.com/blog/feed/",
            "Cloudflare": "https://blog.cloudflare.com/rss/",
            "HashiCorp": "https://www.hashicorp.com/blog/feed.xml",
            "Datadog": "https://www.datadoghq.com/blog/index.xml",
            "Grafana Labs": "https://grafana.com/blog/index.xml",
            "Last Week in AWS": "https://www.lastweekinaws.com/feed/",
            "The New Stack": "https://thenewstack.io/feed/",
            "Elastic": "https://www.elastic.co/blog/feed",
            "GitHub Blog": "https://github.blog/feed/",
            "Netflix Tech": "https://netflixtechblog.com/feed",
            "CISA Alerts": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
            "SRE Weekly": "https://sreweekly.com/feed/",
            "Brendan Gregg": "https://www.brendangregg.com/blog/rss.xml",
            "Julia Evans": "https://jvns.ca/atom.xml",
        }
        return self._fetch_rss(feeds, "INFRA / DEVOPS / SRE", max_per_feed=2, max_total=32)

    def collect_security_news(self):
        """Krebs, The Hacker News, BleepingComputer, Project Zero, Help Net Security
        — CVE/breaches/exploits."""
        print("Fetching Security RSS...")
        feeds = {
            "KrebsOnSecurity": "https://krebsonsecurity.com/feed/",
            "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
            "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
            "Project Zero": "https://googleprojectzero.blogspot.com/feeds/posts/default",
            "Help Net Security": "https://www.helpnetsecurity.com/feed/",
        }
        return self._fetch_rss(feeds, "SECURITY", max_per_feed=3, max_total=18)

    def collect_nvd_cves(self, hours_back=24, min_score=7.0, max_results=15):
        """NVD CVE feed via JSON API 2.0. Returns CVEs published in the last
        `hours_back` hours with CVSS baseScore >= min_score (HIGH/CRITICAL).
        NVD_API_KEY env raises the rate limit from 5 to 50 req/30s."""
        print("Fetching NVD CVEs...")
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours_back)
        # NVD wants ISO 8601 without timezone suffix; millisecond precision.
        fmt = "%Y-%m-%dT%H:%M:%S.000"
        params = {
            "pubStartDate": start.strftime(fmt),
            "pubEndDate": now.strftime(fmt),
            "resultsPerPage": 2000,
        }
        headers = {"User-Agent": "azalio-tech-summary/1.0"}
        if self.nvd_key:
            headers["apiKey"] = self.nvd_key
        try:
            resp = requests.get(
                "https://services.nvd.nist.gov/rest/json/cves/2.0",
                params=params,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  NVD error: {e}")
            return ""

        items = []
        for v in data.get("vulnerabilities", []):
            cve = v.get("cve", {})
            cve_id = cve.get("id", "")
            if not cve_id:
                continue
            # Description: first English entry.
            desc = ""
            for d in cve.get("descriptions", []):
                if d.get("lang") == "en":
                    desc = d.get("value", "").strip()
                    break
            # CVSS: prefer v4.0 → v3.1 → v3.0 → v2 (most recent metric standard wins).
            score = 0.0
            severity = ""
            metrics = cve.get("metrics", {})
            for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
                entries = metrics.get(key) or []
                if not entries:
                    continue
                cvss = entries[0].get("cvssData", {})
                score = cvss.get("baseScore", 0.0)
                # v2 puts severity at the entry level; v3+ inside cvssData.
                severity = cvss.get("baseSeverity") or entries[0].get("baseSeverity", "")
                break
            if score < min_score:
                continue
            url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
            items.append({
                "id": cve_id,
                "score": score,
                "severity": severity,
                "desc": desc,
                "url": url,
            })

        # Sort by severity descending so the LLM sees the worst ones first.
        items.sort(key=lambda x: x["score"], reverse=True)

        content = f"NVD CVEs (last {hours_back}h, CVSS>={min_score}):\n"
        count = 0
        for it in items:
            if count >= max_results:
                break
            if self._is_seen(it["url"]):
                continue
            self._mark_seen(it["url"], "NVD")
            title = f"{it['id']} (CVSS {it['score']} {it['severity']})"
            if self._is_semantic_dup(title, "NVD", it["url"], it["desc"]):
                continue
            short_desc = it["desc"][:400].strip()
            content += f"- [{it['id']}] CVSS {it['score']} {it['severity']} — {short_desc} - Link: {it['url']}\n"
            self._add_candidate(
                "NVD", it["id"], f"{it['id']} {short_desc[:80]}", it["url"],
                line=f"- [{it['id']}] CVSS {it['score']} {it['severity']} — {short_desc} - Link: {it['url']}",
                cvss=float(it["score"]), freshness=0.9,
            )
            count += 1
        return content if count > 0 else ""

    def collect_ai_labs(self):
        """Official lab blogs (OpenAI, DeepMind, Meta, Google Research) and
        individual high-signal AI writers (Willison, Raschka, Karpathy). HF Blog +
        NVIDIA Technical Blog cover the applied serving/quantization/GPU/inference
        angle; Latent Space is practical AI-engineering (serving, agents, eval)."""
        print("Fetching AI labs RSS...")
        feeds = {
            "OpenAI": "https://openai.com/news/rss.xml",
            "DeepMind": "https://deepmind.google/blog/rss.xml",
            "Meta Engineering": "https://engineering.fb.com/feed/",
            "Simon Willison": "https://simonwillison.net/atom/everything/",
            "Sebastian Raschka": "https://sebastianraschka.com/rss_feed.xml",
            "Karpathy": "https://karpathy.github.io/feed.xml",
            "HuggingFace Blog": "https://huggingface.co/blog/feed.xml",
            "NVIDIA Technical Blog": "https://developer.nvidia.com/blog/feed/",
            "Latent Space": "https://www.latent.space/feed",
            "Google Research": "https://research.google/blog/rss/",
        }
        return self._fetch_rss(feeds, "AI LABS", max_per_feed=3, max_total=22)

    def collect_eng_curated(self):
        """Community-curated and individual-curator engineering signal.
        Lobsters is HN-adjacent but smaller and more infra/security-skewed.
        The Pragmatic Engineer (Gergely Orosz) is weekly engineering-org +
        industry analysis."""
        print("Fetching Engineering curated RSS...")
        feeds = {
            "Lobsters": "https://lobste.rs/rss",
            "Pragmatic Engineer": "https://newsletter.pragmaticengineer.com/feed",
            "InfoQ": "https://feed.infoq.com/",
            "Stripe Engineering": "https://stripe.com/blog/feed.rss",
            "Discord Engineering": "https://discord.com/blog/rss.xml",
        }
        return self._fetch_rss(feeds, "ENGINEERING CURATED", max_per_feed=4, max_total=16)

    def collect_finnhub(self):
        """Finnhub market news (needs FINNHUB_API_KEY env var)."""
        if not self.finnhub_key:
            return ""
        print("Fetching Finnhub...")
        content = "FINNHUB MARKET NEWS:\n"
        count = 0
        for category in ["general"]:
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/news",
                    params={"category": category, "token": self.finnhub_key},
                    timeout=15,
                )
                resp.raise_for_status()
                for item in resp.json():
                    link = item.get("url", "")
                    if self._is_seen(link):
                        continue
                    self._mark_seen(link, f"finnhub:{category}")
                    title = item.get("headline", "").strip()
                    source = item.get("source", "")
                    desc = item.get("summary", "")[:300]
                    if self._is_semantic_dup(title, f"finnhub:{source}", link, desc):
                        continue
                    content += f"[{source}] {title} - Link: {link}\n"
                    self._add_candidate("FINNHUB MARKET NEWS", source or "Finnhub", title, link,
                                        line=f"[{source}] {title} - Link: {link}")
                    count += 1
                    if count >= 15:
                        break
            except Exception as e:
                print(f"  Finnhub {category} error: {e}")
        return content if count > 0 else ""
