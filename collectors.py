import hashlib
import os
import json
import re
import subprocess
import sqlite3
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

class Collectors:
    def __init__(self, workspace, dedup=None):
        self.workspace = workspace
        self.dedup = dedup
        self.python_bin = os.environ.get("PYTHON_BIN", "python3")
        self.venv_python = os.environ.get("VENV_PYTHON", self.python_bin)
        self.db_path = os.path.join(workspace, "memory/reddit_sent.db")

        # Paths to JSONs
        self.reddit_raw_json = os.path.join(workspace, "memory/reddit_ai_raw.json")
        self.market_news_json = os.path.join(workspace, "memory/market_news_raw.json")
        self.ru_news_json = os.path.join(workspace, "memory/ru_news_latest.json")
        self.telegram_raw_json = os.path.join(workspace, "memory/telegram_raw.json")

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
        self.ru_news_script = os.environ.get("RU_NEWS_SCRIPT", "")
        self.market_news_script = os.environ.get("MARKET_NEWS_SCRIPT", "")

        # API keys (optional, from env)
        self.finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
        self.newsapi_key = os.environ.get("NEWSAPI_KEY", "")

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

    def _is_seen(self, url):
        if not url: return False
        url = self._normalize_url(url)
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
        if not url: return
        url = self._normalize_url(url)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            now_str = datetime.now().isoformat()
            cursor.execute('INSERT OR IGNORE INTO sent_posts (url, subreddit, sent_at) VALUES (?, ?, ?)',
                           (url, source, now_str))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  _mark_seen error: {e}")

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
        with open(path, "r") as f:
            try: data = json.load(f)
            except: data = []
        with open(path, "w") as f:
            json.dump([], f)
        return data

    def _fetch_rss(self, feeds, source_label, max_per_feed=5, max_total=20, max_age_days=7):
        """Generic RSS fetcher with URL + semantic dedup. Skips entries older than max_age_days."""
        import time as _time
        content = f"{source_label}:\n"
        count = 0
        cutoff_ts = _time.time() - max_age_days * 86400
        for name, url in feeds.items():
            try:
                feed = feedparser.parse(url)
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
                    content += f"[{name}] {title} - Link: {link}\n"
                    if summary:
                        content += f"  {summary}\n"
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
            source = f"Reddit:r/{p.get('subreddit', '?')}"
            self._mark_seen(url, source)
            desc = p.get('text', '')[:300]
            if self._is_semantic_dup(title, source, url, desc):
                continue
            content += f"\n[r/{p['subreddit']}] {title}\n"
            content += f"Link: {url}\n"
            if p.get('text'): content += f"Context: {p['text'][:400]}\n"
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
            print(f"  Telegram fetcher error: {e}")
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
        """ArXiv AI/ML papers RSS."""
        print("Fetching ArXiv AI/ML...")
        feeds = {
            "cs.AI": "http://export.arxiv.org/rss/cs.AI",
            "cs.LG": "http://export.arxiv.org/rss/cs.LG",
        }
        return self._fetch_rss(feeds, "ARXIV AI/ML PAPERS", max_per_feed=8, max_total=15)

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
                self._mark_seen(item_url, "ClaudePlatform")
                if self._is_semantic_dup(plain, "ClaudePlatform", item_url):
                    continue
                content += f"- {plain}\n"
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
                count += 1
                if count >= 20:
                    return content
        return content if count > 0 else ""

    def collect_infra_news(self):
        """Kubernetes, CNCF, AWS, Cloudflare, CISA RSS for DevOps/SRE."""
        print("Fetching Infra/DevOps RSS...")
        feeds = {
            "Kubernetes": "https://kubernetes.io/feed.xml",
            "CNCF": "https://www.cncf.io/feed/",
            "AWS News": "https://aws.amazon.com/blogs/aws/feed/",
            "Cloudflare": "https://blog.cloudflare.com/rss/",
            "CISA Alerts": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        }
        return self._fetch_rss(feeds, "INFRA / DEVOPS / SRE", max_per_feed=3, max_total=12)

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
                    count += 1
                    if count >= 15:
                        break
            except Exception as e:
                print(f"  Finnhub {category} error: {e}")
        return content if count > 0 else ""
