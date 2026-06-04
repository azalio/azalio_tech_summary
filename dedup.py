"""Event-based deduplication with clustering, anchor overlap, and E5 embeddings.

Clusters headlines into "events". A new headline is matched against the
(running-mean) centroid of each live cluster. The matching gate is
embeddings-first with a cheap, language-agnostic anchor check in the gray zone:

    emb_sim >= AUTO_MATCH       -> match (trust embeddings)
    GRAY_MIN <= emb < AUTO      -> match iff anchor_overlap >= ANCHOR_MIN
    emb_sim < GRAY_MIN          -> no match

Anchors are canonical entities + Latin tokens (the RU<->EN / CN<->EN bridge) +
salient numbers (years, versions). A year/version conflict raises the anchor
bar to guard against over-merging two distinct events about the same actor.

Model: intfloat/multilingual-e5-small (384-dim, RU+EN+CN, retrieval-optimized).
"""

import json
import logging
import os
import re
import sqlite3
import struct
import time
import unicodedata
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Model ────────────────────────────────────────────────────────────────

_model = None
EMBEDDING_DIM = 384
MODEL_NAME = "intfloat/multilingual-e5-small"


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading %s ...", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Model loaded.")
    return _model


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return struct.pack(f"{EMBEDDING_DIM}f", *vec.tolist())


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.array(struct.unpack(f"{EMBEDDING_DIM}f", blob), dtype=np.float32)


# ── Headline normalization ───────────────────────────────────────────────

_SOURCE_SUFFIX = re.compile(
    r'\s*[-|–—]\s*(?:BBC|CNN|AP|Reuters|TechCrunch|Ars Technica|'
    r'The Verge|CoinDesk|ArXiv|The Guardian|NY Times|CNBC|Bloomberg|'
    r'Forbes|Wired|Al Jazeera|RBC|ТАСС|РИА|Коммерсант|Ведомости|'
    r'Хабр|Лента\.ру).*$', re.IGNORECASE)

_LEADING_TAG = re.compile(
    r'^\s*\[?(?:BREAKING|EXCLUSIVE|UPDATE|UPDATED|DEVELOPING|'
    r'OPINION|ANALYSIS|VIDEO|LIVE|СРОЧНО|ЭКСКЛЮЗИВ)\]?\s*[:\-–—]\s*',
    re.IGNORECASE)

_TRAILING_NOISE = re.compile(
    r',?\s*(?:report says?|sources? says?|according to reports?|'
    r'reports?$|per report)\s*$', re.IGNORECASE)


def normalize_headline(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-")
    text = _LEADING_TAG.sub("", text)
    text = _SOURCE_SUFFIX.sub("", text)
    text = _TRAILING_NOISE.sub("", text)
    return re.sub(r'\s+', ' ', text).strip()


# ── Token extraction & entity canonicalization ───────────────────────────

_STOPWORDS = frozenset({
    'the', 'a', 'an', 'of', 'to', 'in', 'after', 'by', 'on', 'for',
    'and', 'is', 'was', 'has', 'its', 'are', 'from', 'with', 'that',
    'says', 'new', 'report', 'news', 'update', 'how', 'why', 'what',
    'not', 'but', 'will', 'can', 'could', 'would', 'been', 'have',
    'had', 'than', 'more', 'over', 'into', 'about', 'also', 'may',
    'said', 'their', 'within',
    'и', 'в', 'на', 'с', 'по', 'после', 'из', 'для', 'что', 'как',
    'это', 'был', 'была', 'его', 'она', 'все', 'при', 'так', 'уже',
    'не', 'но', 'или', 'то', 'за', 'до', 'об', 'от', 'же', 'ещё',
})

_SHORT_TOKENS = frozenset({'ai', 'ml'})

_ENTITY_ALIASES = {
    'france': {'france', 'french', 'франция', 'французский', 'французского', 'французском'},
    'russia': {'russia', 'russian', 'россия', 'российский', 'рф', 'российского'},
    'usa': {'usa', 'united', 'states', 'america', 'american', 'сша', 'американский'},
    'china': {'china', 'chinese', 'китай', 'китайский'},
    'ukraine': {'ukraine', 'ukrainian', 'украина', 'украинский'},
    'iran': {'iran', 'iranian', 'иран', 'иранский'},
    'israel': {'israel', 'israeli', 'израиль', 'израильский'},
    'bitcoin': {'bitcoin', 'btc', 'биткоин', 'биткойн'},
    'ethereum': {'ethereum', 'eth', 'эфириум'},
    'apple': {'apple', 'эпл'},
    'google': {'google', 'alphabet', 'гугл'},
    'openai': {'openai', 'chatgpt'},
    'anthropic': {'anthropic', 'claude'},
    'trump': {'trump', 'трамп'},
    'putin': {'putin', 'путин'},
    'macron': {'macron', 'макрон'},
    'aircraft_carrier': {'aircraft', 'carrier', 'авианосец', 'авианосца'},
    'strava': {'strava', 'страва'},
    'nasa': {'nasa', 'наса'},
    'spacex': {'spacex', 'спейсикс'},
    'tesla': {'tesla', 'тесла'},
    'nvidia': {'nvidia', 'нвидиа'},
    'bezos': {'bezos', 'безос'},
    'artemis': {'artemis', 'артемида'},
    'starship': {'starship'},
}

_ALIAS_LOOKUP = {}
for _canon, _aliases in _ENTITY_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_alias] = _canon

# Multi-word entity phrases matched as substrings of the raw (lowercased)
# headline — single-word token splitting can't recover "blue origin" or
# "new glenn" as one entity, and these proper nouns are exactly what fragmented
# developing stories across clusters. Word-boundary anchored to avoid partial
# hits ("nasa" inside "nasal").
# Canon keys here MUST match _ENTITY_ALIASES canon forms where they overlap
# (e.g. 'spacex'), so the same surface form isn't double-counted as two anchors
# — that would inflate overlap_coefficient for actor-only matches.
_ALIAS_PHRASES = {
    'blue_origin': ['blue origin'],
    'new_glenn': ['new glenn'],
    'new_shepard': ['new shepard'],
    'spacex': ['spacex', 'space x'],
    'falcon9': ['falcon 9'],
    'falcon_heavy': ['falcon heavy'],
    'blue_moon': ['blue moon'],
    'james_webb': ['james webb', 'jwst'],
}

_PHRASE_PATTERNS = [
    (_canon, re.compile(r'\b' + re.escape(_phrase) + r'\b'))
    for _canon, _phrases in _ALIAS_PHRASES.items()
    for _phrase in _phrases
]

_LATIN_RE = re.compile(r"[a-z][a-z0-9\-']*")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_VERSION_RE = re.compile(r"\b\d+\.\d+\b")


def extract_tokens(text: str) -> set:
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9][a-zA-Zа-яА-ЯёЁ0-9\-']+", text.lower())
    normalized = {w[:-2] if w.endswith("'s") else w for w in words}
    return {
        w for w in normalized
        if w not in _STOPWORDS and (len(w) >= 3 or w in _SHORT_TOKENS)
    }


def canonicalize(tokens: set) -> set:
    return {_ALIAS_LOOKUP.get(t, t) for t in tokens}


def jaccard_similarity(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def overlap_coefficient(set_a: set, set_b: set) -> float:
    """|A ∩ B| / min(|A|, |B|). Robust when one headline is a short subset of
    the other (Jaccard would understate that; overlap doesn't)."""
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


def extract_anchors(text: str) -> set:
    """Language-agnostic anchors for the gray-zone gate.

    Combines canonical multi/single-word entities, Latin-script tokens (the
    cross-language bridge — "New Glenn"/"Uber"/"AI" survive in RU/CN headlines)
    and salient numbers (years, dotted versions).
    """
    norm = normalize_headline(text)
    low = norm.lower()
    anchors = set()

    for canon, pattern in _PHRASE_PATTERNS:
        if pattern.search(low):
            anchors.add(canon)

    for w in _LATIN_RE.findall(low):
        if w.endswith("'s"):
            w = w[:-2]
        if not w or w in _STOPWORDS:
            continue
        if len(w) >= 3 or w in _SHORT_TOKENS:
            anchors.add(_ALIAS_LOOKUP.get(w, w))

    for m in _YEAR_RE.finditer(low):
        anchors.add(m.group(0))
    for m in _VERSION_RE.finditer(low):
        anchors.add(m.group(0))

    return anchors


def extract_numbers(text: str) -> dict:
    """Typed salient numbers used only for over-merge protection.

    Plain quantities ("5 Tbps", "60 years") are intentionally ignored — they
    drift between reports of the same event. Only years and dotted versions are
    precise enough to signal a genuine event mismatch.
    """
    low = normalize_headline(text).lower()
    return {
        "year": {m.group(0) for m in _YEAR_RE.finditer(low)},
        "version": {m.group(0) for m in _VERSION_RE.finditer(low)},
    }


def number_conflict(nums_a: dict, nums_b: dict) -> bool:
    """True if both sides name a year (or version) and they don't overlap."""
    for key in ("year", "version"):
        sa = nums_a.get(key) or set()
        sb = nums_b.get(key) or set()
        if sa and sb and not (sa & sb):
            return True
    return False


# ── EventDedup ───────────────────────────────────────────────────────────

class EventDedup:
    """Event-clustering deduplicator.

    Args:
        db_dir: directory for the SQLite database.
        match_threshold: gray-zone floor; below it, never a match. Kept for
            backward compatibility — `gray_zone_min` overrides it when given.
        gray_zone_min: emb_sim floor for the anchor-gated zone (default 0.78).
        auto_match_threshold: emb_sim at/above which embeddings alone decide.
        anchor_overlap_min: required anchor overlap in the gray zone.
        anchor_overlap_conflict: required anchor overlap when a year/version
            conflict is present (raises the bar to block over-merging).
        ttl_hours: how long to keep clusters before expiry.
        matching_ttl_hours: a cluster stops accepting new matches this long
            after its FIRST sighting (bounds how long one story stays open).
        max_cluster_size: runaway safety valve — clusters at/above this size
            stop matching. Keep it well above what a hot story accrues inside
            `matching_ttl_hours`, otherwise a still-live cluster "closes" mid-
            story and the next headline spawns a duplicate cluster that the
            editor re-posts as new. `matching_ttl_hours` is the real "story
            closed" gate; the centroid is already frozen by
            `centroid_update_limit`, so a large cluster neither drifts nor costs
            more than one dot product to match.
        centroid_update_limit: stop updating the running-mean centroid after
            this many items (prevents slow topic drift on huge clusters).
        dry_run: if True, log duplicates but do not skip.
    """

    def __init__(
        self,
        db_dir: str = "/tmp/event_dedup",
        match_threshold: float = 0.78,
        gray_zone_min: Optional[float] = None,
        auto_match_threshold: float = 0.92,
        anchor_overlap_min: float = 0.30,
        anchor_overlap_conflict: float = 0.45,
        ttl_hours: int = 168,
        matching_ttl_hours: int = 72,
        max_cluster_size: int = 300,
        centroid_update_limit: int = 10,
        dry_run: bool = False,
    ):
        self.gray_zone_min = gray_zone_min if gray_zone_min is not None else match_threshold
        self.auto_match_threshold = auto_match_threshold
        self.anchor_overlap_min = anchor_overlap_min
        self.anchor_overlap_conflict = anchor_overlap_conflict
        self.ttl_hours = ttl_hours
        self.matching_ttl_hours = matching_ttl_hours
        self.max_cluster_size = max_cluster_size
        self.centroid_update_limit = centroid_update_limit
        self.dry_run = dry_run

        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "events.db")
        self._conn = sqlite3.connect(db_path)
        self._init_db()
        self._cleanup()

        # Load active clusters into memory
        self._clusters = self._load_clusters()
        self._stats = {"checked": 0, "duplicates": 0, "added": 0}
        self._run_cluster_hits = {}
        self._run_cluster_sources = {}

    # ── DB schema ────────────────────────────────────────────────────

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_clusters (
                cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
                centroid BLOB NOT NULL,
                tokens_json TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                item_count INTEGER DEFAULT 1,
                representative_title TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cls_last_seen
                ON event_clusters(last_seen);

            CREATE TABLE IF NOT EXISTS cluster_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                embedding BLOB NOT NULL,
                tokens_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (cluster_id) REFERENCES event_clusters(cluster_id)
            );
        """)
        # Migrate older DBs: cumulative anchors/numbers were added later.
        existing = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(event_clusters)")}
        if "anchors_json" not in existing:
            self._conn.execute(
                "ALTER TABLE event_clusters ADD COLUMN anchors_json TEXT")
        if "numbers_json" not in existing:
            self._conn.execute(
                "ALTER TABLE event_clusters ADD COLUMN numbers_json TEXT")
        # reported_at: set once a cluster's story has been posted to the channel,
        # so event_signals never re-surfaces an already-published story as a
        # fresh burst on a later run (the "same news for days" failure).
        if "reported_at" not in existing:
            self._conn.execute(
                "ALTER TABLE event_clusters ADD COLUMN reported_at REAL")
        self._conn.commit()

    def _cleanup(self):
        cutoff = time.time() - self.ttl_hours * 3600
        cur = self._conn.execute(
            "DELETE FROM event_clusters WHERE last_seen < ?", (cutoff,)
        )
        if cur.rowcount:
            logger.info("Expired %d old clusters", cur.rowcount)
        # Clean orphaned items and items older than TTL
        self._conn.execute(
            "DELETE FROM cluster_items WHERE cluster_id NOT IN "
            "(SELECT cluster_id FROM event_clusters)"
        )
        cur2 = self._conn.execute(
            "DELETE FROM cluster_items WHERE created_at < ?", (cutoff,)
        )
        if cur2.rowcount:
            logger.info("Expired %d old items", cur2.rowcount)
        self._conn.commit()

    # ── Cluster memory ───────────────────────────────────────────────

    @staticmethod
    def _numbers_from_json(raw: Optional[str]) -> dict:
        if not raw:
            return {"year": set(), "version": set()}
        data = json.loads(raw)
        return {
            "year": set(data.get("year", [])),
            "version": set(data.get("version", [])),
        }

    def _load_clusters(self) -> list:
        rows = self._conn.execute(
            "SELECT cluster_id, centroid, tokens_json, last_seen, "
            "item_count, representative_title, first_seen, "
            "anchors_json, numbers_json, reported_at FROM event_clusters"
        ).fetchall()
        clusters = []
        for (cid, centroid_blob, tokens_json, last_seen, count, title,
             first_seen, anchors_json, numbers_json, reported_at) in rows:
            # Legacy rows have no anchors/numbers yet — derive from the title so
            # they still match until the next sighting backfills them.
            if anchors_json:
                anchors = set(json.loads(anchors_json))
            else:
                anchors = extract_anchors(title or "")
            numbers = self._numbers_from_json(numbers_json)
            if not numbers_json:
                numbers = extract_numbers(title or "")
            clusters.append({
                "id": cid,
                "centroid": _blob_to_vec(centroid_blob),
                "tokens": set(json.loads(tokens_json)),
                "anchors": anchors,
                "numbers": numbers,
                "last_seen": last_seen,
                "first_seen": first_seen,
                "count": count,
                "title": title,
                "reported": reported_at is not None,
            })
        return clusters

    def _save_cluster(self, cluster: dict):
        self._conn.execute(
            "UPDATE event_clusters SET centroid=?, tokens_json=?, "
            "last_seen=?, item_count=?, representative_title=?, "
            "anchors_json=?, numbers_json=? "
            "WHERE cluster_id=?",
            (
                _vec_to_blob(cluster["centroid"]),
                json.dumps(sorted(cluster["tokens"])),
                cluster["last_seen"],
                cluster["count"],
                cluster["title"],
                json.dumps(sorted(cluster["anchors"])),
                json.dumps({k: sorted(v) for k, v in cluster["numbers"].items()}),
                cluster["id"],
            )
        )
        self._conn.commit()

    def _create_cluster(self, title: str, vec: np.ndarray, tokens: set,
                        anchors: set, numbers: dict, ts: float) -> dict:
        cur = self._conn.execute(
            "INSERT INTO event_clusters "
            "(centroid, tokens_json, first_seen, last_seen, item_count, "
            "representative_title, anchors_json, numbers_json) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (_vec_to_blob(vec), json.dumps(sorted(tokens)), ts, ts, title,
             json.dumps(sorted(anchors)),
             json.dumps({k: sorted(v) for k, v in numbers.items()}))
        )
        self._conn.commit()
        cluster = {
            "id": cur.lastrowid,
            "centroid": vec.copy(),
            "tokens": tokens.copy(),
            "anchors": set(anchors),
            "numbers": {k: set(v) for k, v in numbers.items()},
            "first_seen": ts,
            "last_seen": ts,
            "count": 1,
            "title": title,
            "reported": False,
        }
        self._clusters.append(cluster)
        return cluster

    def _add_to_cluster(self, cluster: dict, vec: np.ndarray,
                        anchors: set, numbers: dict, ts: float):
        # Running-mean centroid, frozen once the cluster is well-established to
        # stop slow topic drift on long-lived clusters.
        if cluster["count"] < self.centroid_update_limit:
            n = cluster["count"]
            blended = cluster["centroid"] * n + vec
            norm = float(np.linalg.norm(blended))
            if norm > 0:
                cluster["centroid"] = np.asarray(blended / norm, dtype=np.float32)
        cluster["anchors"] |= anchors
        cluster["numbers"]["year"] |= numbers["year"]
        cluster["numbers"]["version"] |= numbers["version"]
        cluster["last_seen"] = max(cluster["last_seen"], ts)
        cluster["count"] += 1
        self._save_cluster(cluster)

    def _save_item(self, cluster_id: int, title: str, source: str,
                   url: str, vec: np.ndarray, tokens: set, ts: float):
        self._conn.execute(
            "INSERT INTO cluster_items "
            "(cluster_id, title, source, url, embedding, tokens_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cluster_id, title, source, url, _vec_to_blob(vec),
             json.dumps(sorted(tokens)), ts)
        )
        self._conn.commit()

    def _mark_touched(self, cluster: dict, source: str):
        cluster_id = cluster["id"]
        self._run_cluster_hits[cluster_id] = self._run_cluster_hits.get(cluster_id, 0) + 1
        self._run_cluster_sources.setdefault(cluster_id, set()).add(source or "unknown")

    # ── Encoding ─────────────────────────────────────────────────────

    def _encode(self, text: str) -> np.ndarray:
        model = _get_model()
        vec = model.encode(f"passage: {text}", normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def _make_text(self, title: str, description: str = "") -> str:
        text = normalize_headline(title)
        if description:
            desc = re.sub(r'\s+', ' ', description.strip())[:300]
            text += ". " + desc
        return text[:600]

    # ── Matching ─────────────────────────────────────────────────────

    def _find_best_cluster(self, vec: np.ndarray, anchors: set,
                           numbers: dict, ts: float) -> Optional[tuple]:
        if not self._clusters:
            return None

        best_emb_sim = -1.0
        best_cluster = None
        best_overlap = 0.0

        matching_cutoff = ts - self.matching_ttl_hours * 3600

        for cluster in self._clusters:
            # Skip clusters too old (story closed) or too large for matching
            if cluster["first_seen"] < matching_cutoff:
                continue
            if cluster["count"] >= self.max_cluster_size:
                continue

            emb_sim = float(np.dot(vec, cluster["centroid"]))
            if emb_sim < self.gray_zone_min:
                continue

            overlap = overlap_coefficient(anchors, cluster["anchors"])
            conflict = number_conflict(numbers, cluster["numbers"])

            # A year/version conflict downgrades even high-confidence embedding
            # matches to "needs strong anchor agreement" — blocks two distinct
            # events about the same actor from collapsing together.
            if conflict:
                is_match = overlap >= self.anchor_overlap_conflict
            elif emb_sim >= self.auto_match_threshold:
                is_match = True
            else:
                is_match = overlap >= self.anchor_overlap_min

            if is_match and emb_sim > best_emb_sim:
                best_emb_sim = emb_sim
                best_cluster = cluster
                best_overlap = overlap

        if best_cluster is not None:
            return (best_cluster, best_emb_sim, best_overlap)
        return None

    # ── Public API (compatible with SemanticDedup) ───────────────────

    def check_and_add(self, title: str, source: str, url: str,
                      description: str = "") -> bool:
        """Check if a news item is new.

        Returns True if NEW (include), False if DUPLICATE (skip).
        In dry_run mode, always returns True but logs duplicates.
        """
        self._stats["checked"] += 1
        ts = time.time()

        text = self._make_text(title, description)
        vec = self._encode(text)
        tokens = extract_tokens(normalize_headline(title))
        anchors = extract_anchors(title)
        numbers = extract_numbers(title)

        match = self._find_best_cluster(vec, anchors, numbers, ts)

        if match:
            cluster, emb_sim, overlap = match
            self._stats["duplicates"] += 1
            logger.info(
                "DUPLICATE (emb=%.2f overlap=%.2f): [%s] %r  ~=  cluster #%d %r",
                emb_sim, overlap, source, title[:80],
                cluster["id"], cluster["title"][:80],
            )
            self._add_to_cluster(cluster, vec, anchors, numbers, ts)
            self._mark_touched(cluster, source)

            if self.dry_run:
                return True
            return False

        # New event
        cluster = self._create_cluster(title, vec, tokens, anchors, numbers, ts)
        self._save_item(cluster["id"], title, source, url, vec, tokens, ts)
        self._mark_touched(cluster, source)
        self._stats["added"] += 1
        return True

    def event_signals(self, min_observations: int = 2, max_events: int = 12) -> list:
        """Return source-burst signals from clusters touched in this process.

        Observations are current-run collector hits. cumulative_item_count is
        the persisted cluster count, useful context for clusters first seen in
        earlier runs and touched again now.
        """
        clusters_by_id = {cluster["id"]: cluster for cluster in self._clusters}
        signals = []
        for cluster_id, observations in self._run_cluster_hits.items():
            sources = sorted(self._run_cluster_sources.get(cluster_id, set()))
            source_count = len(sources)
            if observations < min_observations and source_count < min_observations:
                continue
            cluster = clusters_by_id.get(cluster_id)
            if not cluster:
                continue
            # Already published in an earlier run: keep absorbing its headlines
            # (dedup still works), but don't hand it to the editor again as a
            # fresh burst — that re-posted the same story on consecutive days.
            if cluster.get("reported"):
                continue
            source_burst = "high" if observations >= 3 or source_count >= 3 else "medium"
            signals.append({
                "cluster_id": cluster_id,
                "title": cluster["title"],
                "observations": observations,
                "source_count": source_count,
                "sources": sources,
                "cumulative_item_count": cluster["count"],
                "source_burst": source_burst,
                "first_seen": cluster["first_seen"],
                "last_seen": cluster["last_seen"],
            })

        signals.sort(
            key=lambda signal: (
                signal["source_burst"] == "high",
                signal["observations"],
                signal["source_count"],
                signal["cumulative_item_count"],
            ),
            reverse=True,
        )
        return signals[:max_events]

    def mark_reported(self, cluster_ids) -> None:
        """Flag clusters whose story has just been published.

        Call this only after a successful channel post, with the cluster_ids the
        editor was given as event_signals. Once flagged, event_signals() will
        not re-surface them on later runs, so a multi-day story isn't re-posted
        as fresh each day. Items still keep deduping into the cluster normally.
        """
        ids = [int(cid) for cid in cluster_ids]
        if not ids:
            return
        ts = time.time()
        self._conn.executemany(
            "UPDATE event_clusters SET reported_at=? WHERE cluster_id=? "
            "AND reported_at IS NULL",
            [(ts, cid) for cid in ids],
        )
        self._conn.commit()
        flagged = set(ids)
        for cluster in self._clusters:
            if cluster["id"] in flagged:
                cluster["reported"] = True

    def stats(self) -> dict:
        total_clusters = len(self._clusters)
        total_items = self._conn.execute(
            "SELECT COUNT(*) FROM cluster_items"
        ).fetchone()[0]
        return {**self._stats, "total_clusters": total_clusters,
                "total_items": total_items}

    def close(self):
        self._conn.close()
