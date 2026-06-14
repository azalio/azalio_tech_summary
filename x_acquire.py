"""Free / low-cost, fault-tolerant X (Twitter) acquisition cascade.

X regularly breaks every unofficial read path, so this layer never bets on a
single scraper. For each configured source it walks an ordered cascade of
providers and takes the **first one that yields items** (first-success), trips a
per-(provider, source) **circuit breaker** on repeated failure, and degrades to a
controlled empty result instead of crashing the digest. The guiding rule:

    don't read X at any cost — build a fault-tolerant acquisition layer where X
    is one channel, not the only mast on the ship.

Cascade (default order, configurable via ``X_PROVIDER_ORDER``):

* **rss**     (L0) — official blog / GitHub-releases / Substack / YouTube /
  Mastodon RSS mirrors. The real news usually lives somewhere stabler than X.
* **bluesky** (L0.5) — AT-Protocol public API (``getAuthorFeed`` /
  ``getListFeed``), no auth, decentralised → the single most durable path for any
  account that also posts to Bluesky. Mapping is explicit per source (``bluesky``
  field); x→bsky handles are NOT 1:1.
* **rsshub**  (L1) — self-hosted RSSHub with a local ``TWITTER_AUTH_TOKEN``
  (held on the RSSHub host, never here). Strongest X-native path.
* **nitter**  (L2) — public Nitter / xcancel RSS instances. Flaky fallback.
* **twscrape**(L3) — unofficial session/cookie scraper. Opt-in, account-based.
* **browser** (L4) — logged-in Playwright with saved storage state. Last resort.
* **email**   (L5) — X email notifications parsed from an IMAP mailbox.

Everything is config-gated: a provider that isn't configured (no RSSHub URL, no
Nitter instances, no twscrape account, …) simply reports ``supports()==False``
and the cascade skips it. Nothing here ever raises out to the caller — the worst
case is an empty item list and a logged DEGRADED line.

Secrets (IMAP password, RSSHub access key, auth tokens) live only in the
environment and are scrubbed from every log line and stored error via
:func:`redact`. They are never written into an item's ``raw`` blob.

The module is import-clean (heavy / optional deps — twscrape, playwright, imap —
are imported lazily inside the provider that needs them) and the pure logic
(parsing, dedup keys, circuit breaker, ``--since``, redaction, cascade) is
unit-tested without network in ``test_x_acquire.py``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import feedparser  # type: ignore[import-untyped]
import requests

DEFAULT_UA = "Mozilla/5.0 vibe-intel-x/1.0"
DEFAULT_TIMEOUT = 15
DEFAULT_PROVIDER_ORDER = "rss,bluesky,rsshub,nitter,twscrape,browser,email"

# Circuit-breaker defaults: after this many consecutive failures for a given
# (provider, source) pair we stop trying it until the cooldown expires, with an
# exponential back-off so a persistently dead path isn't hammered every run.
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_BASE_MIN = 30
BREAKER_COOLDOWN_MAX_MIN = 360

REQUIRED_FIELDS = (
    "id", "url", "author", "text", "published", "source", "provider",
    "fetched_at", "raw",
)


# ── Secret redaction ─────────────────────────────────────────────────────────

# Env vars whose *values* must never appear in logs / stored errors / output.
SECRET_ENV_KEYS = (
    "X_RSSHUB_ACCESS_KEY",
    "X_EMAIL_PASSWORD",
    "X_EMAIL_USER",
    "RSSHUB_TWITTER_AUTH_TOKEN",
    "TWITTER_AUTH_TOKEN",
)
# Query-string / cookie params that look like credentials, masked by name.
_QS_SECRET_RE = re.compile(
    r"((?:key|access_key|auth_token|ct0|token|password|passwd|pass)=)([^&\s'\"]+)",
    re.IGNORECASE,
)


def redact(text, *, extra_secrets=()):
    """Scrub known secret values and credential-looking query params from text.

    Used on every log line and on any error string persisted to provider_state,
    so an auth token in a URL or an IMAP password in a stack trace never leaks.
    """
    if text is None:
        return ""
    s = str(text)
    secrets = [os.environ.get(k) for k in SECRET_ENV_KEYS]
    secrets.extend(extra_secrets)
    for sec in secrets:
        if sec and len(sec) >= 4:
            s = s.replace(sec, "***")
    s = _QS_SECRET_RE.sub(r"\1***", s)
    return s


# ── --since parsing ──────────────────────────────────────────────────────────

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_SINCE_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(value):
    """Parse a ``--since`` spec (``30m`` / ``6h`` / ``2d`` / ``1w`` / bare secs)
    into a :class:`datetime.timedelta`. ``None``/empty -> ``None`` (no cutoff)."""
    if not value:
        return None
    m = _SINCE_RE.match(str(value))
    if not m:
        raise ValueError(
            f"bad --since {value!r}: use forms like 30m, 6h, 2d, 1w, or seconds"
        )
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return timedelta(seconds=n * _SINCE_MULT[unit])


# ── URL canonicalisation & dedup keys ────────────────────────────────────────

_TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com", "nitter.net",
                  "xcancel.com"}
_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")


def canonical_url(url):
    """Normalise a post URL so the same tweet from RSSHub, Nitter, or x.com maps
    to one key. X/Nitter status links collapse to ``https://x.com/<user>/status/<id>``
    (handle kept when present); everything else gets plain https/host normalisation.
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return str(url or "")
    parsed = urlparse(str(url))
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path
    m = _STATUS_RE.search(path)
    is_x_like = host in _TWITTER_HOSTS or "nitter" in host
    if m and (is_x_like or "/status" in path):
        tweet_id = m.group(1)
        before = path[: m.start()].strip("/")
        user = before.split("/")[-1].lstrip("@") if before else ""
        if user and user.lower() not in ("i", "status", "statuses"):
            return f"https://x.com/{user}/status/{tweet_id}"
        return f"https://x.com/i/status/{tweet_id}"
    return urlunparse(("https", host, path.rstrip("/"), "", "", ""))


def dedup_key(item):
    """Route-independent stable id for an item.

    Order: canonical URL -> provider-native id -> hash(author+published+text).
    Same tweet seen through different providers collapses to one key.
    """
    url = item.get("url")
    if url:
        c = canonical_url(url)
        if c:
            return "url:" + c
    raw = item.get("raw") or {}
    pid = raw.get("provider_id")
    if pid:
        return f"pid:{item.get('provider', '?')}:{pid}"
    basis = (
        f"{item.get('author', '')}|{item.get('published', '')}|"
        f"{' '.join((item.get('text') or '').split()).lower()[:200]}"
    )
    return "hash:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()


# ── Normalisation ────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text):
    """Cheap tag strip + whitespace collapse for RSS summaries (no bs4 needed)."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text))).strip()


def _now_utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_item(*, url, author, text, source, provider, title="",
                   published="", engagement=None, raw=None, fetched_at=None):
    """Build a schema-valid normalized item dict (see :data:`REQUIRED_FIELDS`).

    ``id`` is derived from the dedup key. ``fetched_at`` defaults to now (UTC).
    ``raw`` must never contain secrets — callers pass only public metadata.
    """
    item = {
        "id": "",
        "url": str(url or ""),
        "author": str(author or ""),
        "text": (text or "").strip(),
        "title": (title or "").strip(),
        "published": published or "",
        "source": source,
        "provider": provider,
        "fetched_at": fetched_at or _now_utc_iso(),
        "engagement": engagement,
        "raw": raw or {},
    }
    item["id"] = dedup_key(item)
    return item


def validate_item(item):
    """Return a list of schema problems for ``item`` (empty list == valid)."""
    problems = []
    if not isinstance(item, dict):
        return ["item is not a dict"]
    for f in REQUIRED_FIELDS:
        if f not in item:
            problems.append(f"missing field: {f}")
    if not item.get("provider"):
        problems.append("empty provider")
    if not item.get("text") and not item.get("title"):
        problems.append("empty text and title")
    if not isinstance(item.get("raw", {}), dict):
        problems.append("raw is not a dict")
    return problems


# ── Sources ──────────────────────────────────────────────────────────────────

@dataclass
class Source:
    """One configured X source and its non-X mirrors / cross-platform identities.

    ``kind``: ``x_user`` | ``x_list`` | ``rss`` | ``bluesky``.
    ``mirrors``: list of ``{"kind": ..., "url": ...}`` feeds tried first (L0).
    ``bluesky`` / ``bluesky_list``: explicit AT-proto identity (handle/DID or
    ``at://`` list URI) — x→bsky is not 1:1, so it must be stated, never guessed.
    """

    id: str
    kind: str = "x_user"
    handle: str = ""
    list_id: str = ""
    url: str = ""
    priority: int = 0
    mirrors: list = field(default_factory=list)
    bluesky: str = ""
    bluesky_list: str = ""
    max_items: int = 0  # 0 -> use the cascade default cap


def _coerce_mirrors(raw):
    out = []
    for m in raw or []:
        if isinstance(m, str):
            out.append({"kind": "rss", "url": m})
        elif isinstance(m, dict) and m.get("url"):
            out.append({"kind": m.get("kind", "rss"), "url": m["url"]})
    return out


def parse_sources(data):
    """Build ``[Source]`` from a parsed config dict/list. Tolerant of either a
    top-level ``{"sources": [...]}`` or a bare list; skips entries with no id."""
    if isinstance(data, dict):
        rows = data.get("sources", [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    sources = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        sources.append(Source(
            id=str(r["id"]),
            kind=str(r.get("kind", "x_user")),
            handle=str(r.get("handle", "")).lstrip("@"),
            list_id=str(r.get("list_id", "")),
            url=str(r.get("url", "")),
            priority=int(r.get("priority", 0) or 0),
            mirrors=_coerce_mirrors(r.get("mirrors")),
            bluesky=str(r.get("bluesky", "")).lstrip("@"),
            bluesky_list=str(r.get("bluesky_list", "")),
            max_items=int(r.get("max_items", 0) or 0),
        ))
    return sources


def load_sources(path=None, env=None):
    """Load sources from a YAML/JSON file, falling back to the ``X_HANDLES`` env
    var (comma-separated handles) when no file is configured or PyYAML is absent.

    Resolution order: explicit ``path`` -> ``X_SOURCES`` env -> ``x_sources.yaml``
    next to this module. Returns ``[]`` when nothing is configured (collector then
    silently disables itself)."""
    env = env or os.environ
    candidates = []
    if path:
        candidates.append(path)
    if env.get("X_SOURCES"):
        candidates.append(env["X_SOURCES"])
    candidates.append(_default_sources_path())
    for cand in candidates:
        if cand and os.path.exists(cand):
            data = _load_config_file(cand)
            if data is not None:
                return parse_sources(data)
    # Env fallback: a bare comma list of handles, mirror-less.
    handles = [h.strip().lstrip("@") for h in env.get("X_HANDLES", "").split(",")
               if h.strip()]
    return [Source(id=f"x_{h.lower()}", kind="x_user", handle=h, priority=5)
            for h in handles]


def _default_sources_path():
    """Default sources file: ``x_sources.yaml`` next to this module. Indirected
    through a function so it can be overridden (and tests can isolate from a real
    file sitting in the repo)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_sources.yaml")


def _load_config_file(path):
    """Parse a .yaml/.yml/.json sources file. YAML needs PyYAML; if it's missing
    we degrade (JSON still works, and ``X_HANDLES`` remains a no-dep fallback)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print(f"  [x] cannot read sources file {path}: {e}")
        return None
    if path.endswith(".json"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  [x] invalid JSON in {path}: {e}")
            return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print(f"  [x] PyYAML not installed; cannot read {path} "
              f"(use a .json sources file or set X_HANDLES)")
        return None
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as e:
        print(f"  [x] invalid YAML in {path}: {e}")
        return None


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_get(url, timeout=DEFAULT_TIMEOUT, headers=None):
    """GET with a mandatory timeout; returns the raw body bytes. Raises on
    HTTP/transport error (callers translate that into a ProviderError)."""
    h = {"User-Agent": DEFAULT_UA}
    if headers:
        h.update(headers)
    resp = requests.get(url, headers=h, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _http_get_json(url, timeout=DEFAULT_TIMEOUT, headers=None):
    return json.loads(_http_get(url, timeout=timeout, headers=headers))


def _struct_to_iso(pub):
    """feedparser publishes a UTC struct_time; turn it into an ISO-8601 string."""
    if not pub:
        return ""
    try:
        return datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc) \
            .isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return ""


def _struct_before(pub, since_dt):
    """True if a feedparser struct_time is older than the cutoff (drop it)."""
    if not since_dt or not pub:
        return False
    try:
        return datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc) < since_dt
    except (TypeError, ValueError, OverflowError):
        return False


def _author_label(source):
    """Stable per-source author label: ``@handle`` for X sources, else the id."""
    if source.handle:
        return "@" + source.handle
    return source.id


_X_HANDLE_RE = re.compile(r"^https?://x\.com/([A-Za-z0-9_]{1,15})/status/", re.IGNORECASE)


def _handle_from_x_url(url):
    """Extract ``@handle`` from a canonical x.com status URL. Used for mixed-author
    feeds (the home timeline) where every tweet has a different author. Returns ''
    for ``/i/status`` or non-X URLs."""
    m = _X_HANDLE_RE.match(url or "")
    if m and m.group(1).lower() not in ("i", "status", "statuses"):
        return "@" + m.group(1)
    return ""


def _entry_author(e):
    """Best-effort ``@handle`` from a feed entry's author field (fallback for the
    home timeline when the link didn't yield one)."""
    m = re.search(r"@([A-Za-z0-9_]{1,15})", str(e.get("author") or ""))
    return "@" + m.group(1) if m else ""


def entries_to_items(entries, source, provider, since_dt):
    """Map feedparser entries to normalized items, dropping out-of-window ones.

    For X-native providers (nitter/rsshub) the entry link points at the mirror
    host (nitter.net/…); rewrite it to the canonical x.com status URL so the
    digest links readers to X (not a flaky mirror) and the same tweet dedupes
    across instances. RSS mirror links (blogs, Bluesky web URLs) are left as-is.

    The ``x_home`` (Following timeline) source mixes many authors, so the author
    is derived per tweet from the canonical URL/entry rather than the source label.
    """
    default_author = _author_label(source)
    mixed_authors = source.kind == "x_home"
    canonicalize = provider in ("nitter", "rsshub")
    out = []
    for e in entries:
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if _struct_before(pub, since_dt):
            continue
        link = str(e.get("link") or "")
        if canonicalize and link:
            link = canonical_url(link)
        title = strip_html(e.get("title") or "")
        summary = strip_html(e.get("summary") or e.get("description") or "")
        text = summary if len(summary) > len(title) else title
        if not text:
            continue
        author = default_author
        if mixed_authors:
            author = _handle_from_x_url(link) or _entry_author(e) or default_author
        out.append(normalize_item(
            url=link, author=author, text=text, title=title,
            source=source.id, provider=provider,
            published=_struct_to_iso(pub),
            raw={"feed_provider": provider},
        ))
    return out


# ── Provider errors ──────────────────────────────────────────────────────────

class ProviderUnavailable(Exception):
    """Provider can't run (dep missing / not configured). Skip, don't trip the
    breaker — there was no failed attempt against X."""


class ProviderError(Exception):
    """Provider tried and failed (network / parse / auth). Trips the breaker."""


# ── Providers ────────────────────────────────────────────────────────────────

class RssProvider:
    """L0: official blog / GitHub-releases / Substack / YouTube / Mastodon RSS.

    Handles ``kind: rss`` sources directly and the ``mirrors`` of X sources.
    Mirror ``kind`` is documentation only — anything exposing RSS/Atom (incl.
    Mastodon ``/@user.rss``, GitHub ``/releases.atom``) is just a feed URL."""

    name = "rss"

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    def supports(self, source):
        return bool((source.kind == "rss" and source.url) or source.mirrors)

    def fetch(self, source, since_dt):
        if source.kind == "rss" and source.url:
            urls = [source.url]
        else:
            urls = [m["url"] for m in source.mirrors if m.get("url")]
        if not urls:
            raise ProviderUnavailable("no rss/mirror urls")
        items, errors, last = [], 0, None
        for u in urls:
            try:
                content = _http_get(u, self.timeout)
            except Exception as e:  # noqa: BLE001 - any fetch error -> try next
                errors += 1
                last = e
                continue
            items.extend(entries_to_items(feedparser.parse(content).entries,
                                          source, "rss", since_dt))
        if not items and errors == len(urls):
            raise ProviderError(f"all {errors} mirror feeds failed: {redact(last)}")
        return items


class BlueskyProvider:
    """L0.5: AT-Protocol public API — no auth, decentralised, the most durable
    path for any account that also posts to Bluesky. Mapping is explicit per
    source (``bluesky`` / ``bluesky_list``); x→bsky handles are not 1:1."""

    name = "bluesky"
    API = "https://public.api.bsky.app/xrpc"

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    def supports(self, source):
        return bool(source.bluesky or source.bluesky_list or source.kind == "bluesky")

    def _resolve_did(self, actor):
        if actor.startswith("did:"):
            return actor
        url = f"{self.API}/com.atproto.identity.resolveHandle?handle={actor}"
        did = _http_get_json(url, self.timeout).get("did")
        if not did:
            raise ProviderError(f"bluesky: cannot resolve handle {actor}")
        return did

    def fetch(self, source, since_dt):
        actor = source.bluesky or source.handle
        try:
            if source.bluesky_list:
                url = (f"{self.API}/app.bsky.feed.getListFeed"
                       f"?list={source.bluesky_list}&limit=50")
            else:
                if not actor:
                    raise ProviderUnavailable("no bluesky identity")
                did = self._resolve_did(actor)
                url = (f"{self.API}/app.bsky.feed.getAuthorFeed"
                       f"?actor={did}&limit=50&filter=posts_no_replies")
            data = _http_get_json(url, self.timeout)
        except ProviderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"bluesky fetch failed: {redact(e)}")
        return self._feed_to_items(data.get("feed", []), source, since_dt)

    def _feed_to_items(self, feed, source, since_dt):
        out = []
        for entry in feed:
            post = (entry or {}).get("post") or {}
            record = post.get("record") or {}
            text = strip_html(record.get("text") or "")
            if not text:
                continue
            created = record.get("createdAt") or post.get("indexedAt") or ""
            if since_dt and _iso_before(created, since_dt):
                continue
            uri = post.get("uri") or ""
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            author = post.get("author") or {}
            handle = author.get("handle") or source.bluesky or source.handle
            web_url = (f"https://bsky.app/profile/{handle}/post/{rkey}"
                       if rkey else "")
            engagement = ((post.get("likeCount") or 0)
                          + (post.get("repostCount") or 0))
            out.append(normalize_item(
                url=web_url, author="@" + handle if handle else source.id,
                text=text, title=text[:120], source=source.id, provider="bluesky",
                published=created, engagement=engagement or None,
                raw={"provider_id": uri, "at_uri": uri,
                     "reply_count": post.get("replyCount")},
            ))
        return out


class RsshubProvider:
    """L1: self-hosted RSSHub with a local TWITTER_AUTH_TOKEN (held on the RSSHub
    host, never here). Routes ``/twitter/user/:handle``, ``/twitter/list/:id``, and
    ``/twitter/home_latest`` (the auth account's *Following* timeline — pulls your
    subscriptions, x_home). The auth cookie + GraphQL hashes need an occasional
    refresh on the RSSHub box; watch its release feed."""

    name = "rsshub"

    def __init__(self, base_url, access_key="", timeout=DEFAULT_TIMEOUT):
        self.base = (base_url or "").rstrip("/")
        self.access_key = access_key or ""
        self.timeout = timeout

    def supports(self, source):
        return bool(self.base) and source.kind in ("x_user", "x_list", "x_home")

    def fetch(self, source, since_dt):
        if source.kind == "x_home":
            # The authenticated account's "Following" timeline (your subscriptions).
            route = "/twitter/home_latest"
        elif source.kind == "x_list" and source.list_id:
            route = f"/twitter/list/{source.list_id}"
        elif source.handle:
            route = f"/twitter/user/{source.handle}"
        else:
            raise ProviderUnavailable("no handle/list_id for rsshub")
        url = self.base + route
        if self.access_key:
            url += ("&" if "?" in url else "?") + "key=" + self.access_key
        try:
            content = _http_get(url, self.timeout)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"rsshub fetch failed: {redact(e)}")
        return entries_to_items(feedparser.parse(content).entries,
                                source, "rsshub", since_dt)


class NitterProvider:
    """L2: public Nitter / xcancel RSS. Instances die in waves — we try each
    health-checked instance until one returns content. Lists aren't supported via
    the per-user RSS route, so x_list sources skip this layer."""

    name = "nitter"

    def __init__(self, instances, timeout=DEFAULT_TIMEOUT):
        if isinstance(instances, str):
            instances = instances.split(",")
        self.instances = [i.strip().rstrip("/") for i in instances if i.strip()]
        self.timeout = timeout

    def supports(self, source):
        return bool(self.instances) and source.kind == "x_user" and bool(source.handle)

    def fetch(self, source, since_dt):
        any_ok, last = False, None
        for inst in self.instances:
            url = f"{inst}/{source.handle}/rss"
            try:
                content = _http_get(url, self.timeout)
            except Exception as e:  # noqa: BLE001
                last = e
                continue
            any_ok = True
            items = entries_to_items(feedparser.parse(content).entries,
                                     source, "nitter", since_dt)
            if items:
                return items
        if not any_ok:
            raise ProviderError(
                f"all {len(self.instances)} nitter instances failed: {redact(last)}")
        return []


class TwscrapeProvider:
    """L3: unofficial session scraper (``twscrape``). Opt-in, account-based.

    Gated by ``X_TWSCRAPE_ENABLED``; the library + a logged-in account pool
    (``X_TWSCRAPE_DB``) must be provisioned out of band. Read-only by design.
    Untested in CI — fails closed (ProviderError trips the breaker)."""

    name = "twscrape"

    def __init__(self, env, timeout=DEFAULT_TIMEOUT):
        self.enabled = _truthy(env.get("X_TWSCRAPE_ENABLED"))
        self.db_path = env.get("X_TWSCRAPE_DB", "")
        self.limit = int(env.get("X_TWSCRAPE_LIMIT", "20") or "20")
        self.timeout = timeout

    def supports(self, source):
        return self.enabled and source.kind in ("x_user", "x_list")

    def fetch(self, source, since_dt):
        try:
            import asyncio

            from twscrape import API  # type: ignore[import-not-found]
        except ImportError:
            raise ProviderUnavailable("twscrape not installed")
        try:
            return asyncio.run(self._fetch(API, source, since_dt))
        except ProviderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"twscrape failed: {redact(e)}")

    async def _fetch(self, API, source, since_dt):
        api = API(self.db_path) if self.db_path else API()
        out = []
        if source.kind == "x_list" and source.list_id:
            gen = api.list_timeline(int(source.list_id), limit=self.limit)
        else:
            user = await api.user_by_login(source.handle)
            if not user:
                raise ProviderError(f"twscrape: unknown user {source.handle}")
            gen = api.user_tweets(user.id, limit=self.limit)
        async for tw in gen:
            created = getattr(tw, "date", None)
            created_iso = created.isoformat() if created else ""
            if since_dt and created and created < since_dt:
                continue
            engagement = (getattr(tw, "likeCount", 0) or 0) + \
                (getattr(tw, "retweetCount", 0) or 0)
            out.append(normalize_item(
                url=getattr(tw, "url", "") or "",
                author="@" + (source.handle or getattr(
                    getattr(tw, "user", None), "username", "") or ""),
                text=getattr(tw, "rawContent", "") or "",
                source=source.id, provider="twscrape", published=created_iso,
                engagement=engagement or None,
                raw={"provider_id": str(getattr(tw, "id", "") or "")},
            ))
        return out


class BrowserProvider:
    """L4: logged-in Playwright with a saved ``storage_state``. Last resort —
    slow, fragile, breaks on X frontend deploys. Gated by ``X_BROWSER_STATE``
    pointing at a storage-state JSON. Untested in CI — fails closed."""

    name = "browser"

    def __init__(self, env, timeout=DEFAULT_TIMEOUT):
        self.state_path = env.get("X_BROWSER_STATE", "")
        self.max_items = int(env.get("X_BROWSER_MAX_ITEMS", "15") or "15")
        self.timeout = timeout

    def supports(self, source):
        return bool(self.state_path) and os.path.exists(self.state_path) \
            and source.kind == "x_user" and bool(source.handle)

    def fetch(self, source, _since_dt):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            raise ProviderUnavailable("playwright not installed")
        try:
            return self._scrape(sync_playwright, source)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"browser scrape failed: {redact(e)}")

    def _scrape(self, sync_playwright, source):
        out = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=self.state_path)
            page = ctx.new_page()
            page.goto(f"https://x.com/{source.handle}",
                      wait_until="domcontentloaded", timeout=self.timeout * 1000)
            page.wait_for_selector("article", timeout=self.timeout * 1000)
            for art in page.query_selector_all("article")[: self.max_items]:
                text = (art.inner_text() or "").strip()
                link = ""
                a = art.query_selector("a[href*='/status/']")
                if a:
                    href = a.get_attribute("href") or ""
                    link = "https://x.com" + href if href.startswith("/") else href
                if not text:
                    continue
                out.append(normalize_item(
                    url=link, author="@" + source.handle, text=text[:600],
                    source=source.id, provider="browser",
                    raw={"provider_id": canonical_url(link) if link else ""},
                ))
            browser.close()
        return out


class EmailProvider:
    """L5: X email notifications parsed from an IMAP mailbox. Sometimes the most
    survivable channel (X wants notifications to work) but coverage is partial and
    text is truncated. Gated by ``X_EMAIL_HOST`` + creds. Untested in CI."""

    name = "email"

    def __init__(self, env, timeout=DEFAULT_TIMEOUT):
        self.host = env.get("X_EMAIL_HOST", "")
        self.user = env.get("X_EMAIL_USER", "")
        self.password = env.get("X_EMAIL_PASSWORD", "")
        self.folder = env.get("X_EMAIL_FOLDER", "INBOX")
        self.timeout = timeout

    def supports(self, source):
        return bool(self.host and self.user and self.password) \
            and source.kind == "x_user" and bool(source.handle)

    def fetch(self, source, _since_dt):
        import email
        import imaplib
        from email.header import decode_header

        try:
            box = imaplib.IMAP4_SSL(self.host, timeout=self.timeout)
            box.login(self.user, self.password)
            box.select(self.folder)
            # X notification mails come from x.com / twitter.com senders; filter
            # to the handle so we only surface this source's posts.
            data = box.search(None, 'FROM', 'x.com', 'TEXT', source.handle)[1]
            ids = data[0].split() if data and data[0] else []
            out = []
            for mid in ids[-20:]:
                raw = box.fetch(mid, "(RFC822)")[1]
                part = raw[0] if raw else None
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                msg = email.message_from_bytes(part[1])
                subj_parts = decode_header(msg.get("Subject", ""))
                subject = "".join(
                    (p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p)
                    for p, enc in subj_parts
                )
                subject = strip_html(subject)
                if not subject:
                    continue
                out.append(normalize_item(
                    url="", author="@" + source.handle, text=subject,
                    source=source.id, provider="email",
                    published=msg.get("Date", ""),
                    raw={"provider_id": msg.get("Message-ID", "")},
                ))
            box.logout()
            return out
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"email fetch failed: {redact(e)}")


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _iso_before(iso_str, since_dt):
    """True if an ISO-8601 timestamp is older than the cutoff."""
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < since_dt


_PROVIDER_REGISTRY = {
    "rss": lambda _env, t: RssProvider(t),
    "bluesky": lambda _env, t: BlueskyProvider(t),
    "rsshub": lambda env, t: RsshubProvider(
        env.get("X_RSSHUB_URL", ""), env.get("X_RSSHUB_ACCESS_KEY", ""), t),
    "nitter": lambda env, t: NitterProvider(env.get("X_NITTER_INSTANCES", ""), t),
    "twscrape": lambda env, t: TwscrapeProvider(env, t),
    "browser": lambda env, t: BrowserProvider(env, t),
    "email": lambda env, t: EmailProvider(env, t),
}


def build_providers(env=None, *, timeout=DEFAULT_TIMEOUT):
    """Instantiate providers in cascade order from ``X_PROVIDER_ORDER``."""
    env = env or os.environ
    order = env.get("X_PROVIDER_ORDER") or DEFAULT_PROVIDER_ORDER
    providers = []
    for name in (n.strip() for n in order.split(",")):
        factory = _PROVIDER_REGISTRY.get(name)
        if factory:
            providers.append(factory(env, timeout))
    return providers


# ── SQLite state: seen items, circuit breaker, run log ───────────────────────

class XState:
    """Persistent acquisition state: seen-item dedup, per-(provider, source)
    circuit breaker, and a fetch-run audit log. All times stored ISO-8601 UTC."""

    def __init__(self, db_path, *, breaker_threshold=BREAKER_THRESHOLD,
                 cooldown_base_min=BREAKER_COOLDOWN_BASE_MIN,
                 cooldown_max_min=BREAKER_COOLDOWN_MAX_MIN):
        self.db_path = db_path
        self.breaker_threshold = breaker_threshold
        self.cooldown_base_min = cooldown_base_min
        self.cooldown_max_min = cooldown_max_min
        if db_path != ":memory:":
            d = os.path.dirname(db_path)
            if d:
                os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                dedup_key     TEXT PRIMARY KEY,
                first_seen_at TEXT,
                source        TEXT,
                url           TEXT
            );
            CREATE TABLE IF NOT EXISTS provider_state (
                provider      TEXT,
                source_id     TEXT,
                failure_count INTEGER DEFAULT 0,
                last_error    TEXT,
                cooldown_until TEXT,
                PRIMARY KEY (provider, source_id)
            );
            CREATE TABLE IF NOT EXISTS fetch_runs (
                started_at   TEXT,
                finished_at  TEXT,
                items_count  INTEGER,
                errors_count INTEGER
            );
            """
        )
        self.conn.commit()

    # -- circuit breaker --
    def breaker_open(self, provider, source_id, now=None):
        now = now or datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT cooldown_until FROM provider_state WHERE provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        if not row or not row[0]:
            return False
        return now < _parse_iso(row[0])

    def record_failure(self, provider, source_id, error, now=None):
        now = now or datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT failure_count FROM provider_state WHERE provider=? AND source_id=?",
            (provider, source_id),
        ).fetchone()
        failures = (row[0] if row else 0) + 1
        cooldown_until = None
        if failures >= self.breaker_threshold:
            extra = failures - self.breaker_threshold
            minutes = min(self.cooldown_base_min * (2 ** extra), self.cooldown_max_min)
            cooldown_until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO provider_state
                   (provider, source_id, failure_count, last_error, cooldown_until)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider, source_id) DO UPDATE SET
                   failure_count=excluded.failure_count,
                   last_error=excluded.last_error,
                   cooldown_until=excluded.cooldown_until""",
            (provider, source_id, failures, redact(str(error))[:500], cooldown_until),
        )
        self.conn.commit()
        return failures, cooldown_until

    def record_success(self, provider, source_id):
        self.conn.execute(
            """INSERT INTO provider_state
                   (provider, source_id, failure_count, last_error, cooldown_until)
               VALUES (?, ?, 0, NULL, NULL)
               ON CONFLICT(provider, source_id) DO UPDATE SET
                   failure_count=0, last_error=NULL, cooldown_until=NULL""",
            (provider, source_id),
        )
        self.conn.commit()

    # -- seen-item dedup (used by the standalone JSONL pipeline, NOT digest mode) --
    def filter_new(self, items):
        out = []
        for it in items:
            key = it.get("id") or dedup_key(it)
            row = self.conn.execute(
                "SELECT 1 FROM seen_items WHERE dedup_key=?", (key,)
            ).fetchone()
            if not row:
                out.append(it)
        return out

    def mark_seen(self, items, now=None):
        now_str = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        rows = [(it.get("id") or dedup_key(it), now_str, it.get("source", ""),
                 it.get("url", "")) for it in items]
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items (dedup_key, first_seen_at, source, url)"
            " VALUES (?, ?, ?, ?)", rows,
        )
        self.conn.commit()

    def record_run(self, started_at, items_count, errors_count, finished_at=None):
        self.conn.execute(
            "INSERT INTO fetch_runs (started_at, finished_at, items_count, errors_count)"
            " VALUES (?, ?, ?, ?)",
            (started_at, finished_at or _now_utc_iso(), items_count, errors_count),
        )
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Cascade ──────────────────────────────────────────────────────────────────

def cascade_fetch(sources, providers, state=None, *, since_dt=None,
                  per_source_cap=10, now=None, logger=print):
    """Walk the provider cascade per source, first-success wins.

    For each source (priority desc), try each supporting provider in order:
    skip if its breaker is open; ``ProviderUnavailable`` -> skip (no breaker);
    ``ProviderError``/unexpected -> record failure + continue; the first provider
    returning non-empty items wins and the cascade moves to the next source. A
    source where no provider yields is logged DEGRADED (never fatal).

    Returns ``(items, errors)``.
    """
    now = now or datetime.now(timezone.utc)
    items, errors = [], 0
    for source in sorted(sources, key=lambda s: (-s.priority, s.id)):
        cap = source.max_items or per_source_cap
        used = None
        for provider in providers:
            if not provider.supports(source):
                continue
            if state is not None and state.breaker_open(provider.name, source.id, now):
                logger(f"  [x] skip {provider.name}/{source.id}: breaker open")
                continue
            try:
                got = provider.fetch(source, since_dt)
            except ProviderUnavailable as e:
                logger(f"  [x] {provider.name}/{source.id}: unavailable ({redact(e)})")
                continue
            except ProviderError as e:
                errors += 1
                if state is not None:
                    state.record_failure(provider.name, source.id, str(e), now)
                logger(f"  [x] {provider.name}/{source.id}: error ({redact(e)})")
                continue
            except Exception as e:  # noqa: BLE001 - one bad provider must not kill the run
                errors += 1
                if state is not None:
                    state.record_failure(provider.name, source.id, str(e), now)
                logger(f"  [x] {provider.name}/{source.id}: unexpected ({redact(e)})")
                continue
            if state is not None:
                state.record_success(provider.name, source.id)
            if got:
                kept = got[:cap]
                items.extend(kept)
                used = provider.name
                logger(f"  [x] {source.id}: {len(kept)} item(s) via {provider.name}")
                break
        if not used:
            logger(f"  [x] {source.id}: DEGRADED — no provider yielded items")
    return items, errors


def dedupe_in_batch(items):
    """Collapse items sharing a dedup id within this batch (same post via two
    providers/mirrors), keeping first occurrence (cascade order = trust order)."""
    seen, out = set(), []
    for it in items:
        key = it.get("id") or dedup_key(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def acquire(sources, *, env=None, since_dt=None, per_source_cap=10,
            state=None, timeout=DEFAULT_TIMEOUT, now=None, logger=print):
    """Top-level convenience: build providers, run the cascade, dedupe the batch.

    Returns ``(items, errors)``. Does NOT touch ``seen_items`` (the digest path
    lets collectors.py own dedup); the standalone JSONL pipeline applies
    ``state.filter_new`` / ``mark_seen`` separately."""
    env = env or os.environ
    providers = build_providers(env, timeout=timeout)
    items, errors = cascade_fetch(
        sources, providers, state, since_dt=since_dt,
        per_source_cap=per_source_cap, now=now, logger=logger,
    )
    return dedupe_in_batch(items), errors
