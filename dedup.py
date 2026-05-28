"""Event-based deduplication with clustering, token overlap, and E5 embeddings.

Replaces pairwise title-only cosine similarity with event clusters that
accumulate embeddings and entity tokens from multiple sources.

Model: intfloat/multilingual-e5-small (384-dim, RU+EN, retrieval-optimized).
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
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
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
}

_ALIAS_LOOKUP = {}
for _canon, _aliases in _ENTITY_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_alias] = _canon


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


# ── EventDedup ───────────────────────────────────────────────────────────

class EventDedup:
    """Event-clustering deduplicator.

    Args:
        db_dir: directory for the SQLite database.
        match_threshold: combined score above which item is a duplicate.
        ttl_hours: how long to keep clusters before expiry.
        dry_run: if True, log duplicates but do not skip.
    """

    def __init__(
        self,
        db_dir: str = "/tmp/event_dedup",
        match_threshold: float = 0.80,
        ttl_hours: int = 168,
        matching_ttl_hours: int = 48,
        max_cluster_size: int = 50,
        dry_run: bool = False,
    ):
        self.threshold = match_threshold
        self.ttl_hours = ttl_hours
        self.matching_ttl_hours = matching_ttl_hours
        self.max_cluster_size = max_cluster_size
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

    def _load_clusters(self) -> list:
        rows = self._conn.execute(
            "SELECT cluster_id, centroid, tokens_json, last_seen, "
            "item_count, representative_title, first_seen FROM event_clusters"
        ).fetchall()
        clusters = []
        for cid, centroid_blob, tokens_json, last_seen, count, title, first_seen in rows:
            clusters.append({
                "id": cid,
                "centroid": _blob_to_vec(centroid_blob),
                "tokens": set(json.loads(tokens_json)),
                "last_seen": last_seen,
                "first_seen": first_seen,
                "count": count,
                "title": title,
            })
        return clusters

    def _save_cluster(self, cluster: dict):
        self._conn.execute(
            "UPDATE event_clusters SET centroid=?, tokens_json=?, "
            "last_seen=?, item_count=?, representative_title=? "
            "WHERE cluster_id=?",
            (
                _vec_to_blob(cluster["centroid"]),
                json.dumps(sorted(cluster["tokens"])),
                cluster["last_seen"],
                cluster["count"],
                cluster["title"],
                cluster["id"],
            )
        )
        self._conn.commit()

    def _create_cluster(self, title: str, vec: np.ndarray,
                        tokens: set, ts: float) -> dict:
        cur = self._conn.execute(
            "INSERT INTO event_clusters "
            "(centroid, tokens_json, first_seen, last_seen, item_count, representative_title) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (_vec_to_blob(vec), json.dumps(sorted(tokens)), ts, ts, title)
        )
        self._conn.commit()
        cluster = {
            "id": cur.lastrowid,
            "centroid": vec.copy(),
            "tokens": tokens.copy(),
            "first_seen": ts,
            "last_seen": ts,
            "count": 1,
            "title": title,
        }
        self._clusters.append(cluster)
        return cluster

    def _add_to_cluster(self, cluster: dict, title: str,
                        vec: np.ndarray, tokens: set, ts: float):
        # Centroid is FROZEN (first item's embedding) — no averaging
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
        return vec.astype(np.float32)

    def _make_text(self, title: str, description: str = "") -> str:
        text = normalize_headline(title)
        if description:
            desc = re.sub(r'\s+', ' ', description.strip())[:300]
            text += ". " + desc
        return text[:600]

    # ── Matching ─────────────────────────────────────────────────────

    def _find_best_cluster(self, vec: np.ndarray, title_tokens: set,
                           ts: float) -> Optional[tuple]:
        if not self._clusters:
            return None

        canon_new = canonicalize(title_tokens)
        best_emb_sim = -1.0
        best_cluster = None
        best_jaccard = 0.0

        matching_cutoff = ts - self.matching_ttl_hours * 3600

        for cluster in self._clusters:
            # Skip clusters too old or too large for matching
            if cluster["first_seen"] < matching_cutoff:
                continue
            if cluster["count"] >= self.max_cluster_size:
                continue

            # Compare against frozen centroid (= first item's embedding)
            emb_sim = float(np.dot(vec, cluster["centroid"]))

            # Tiered gate:
            #   High confidence (emb >= 0.90): trust embeddings alone
            #   Medium confidence (emb >= threshold): require Jaccard >= 0.15
            if emb_sim >= 0.90:
                is_match = True
                j = 0.0
            elif emb_sim >= self.threshold:
                cls_tokens = extract_tokens(cluster["title"])
                canon_cls = canonicalize(cls_tokens)
                j = jaccard_similarity(canon_new, canon_cls)
                is_match = j >= 0.15
            else:
                is_match = False
                j = 0.0

            if is_match and emb_sim > best_emb_sim:
                best_emb_sim = emb_sim
                best_cluster = cluster
                best_jaccard = j

        if best_cluster is not None:
            return (best_cluster, best_emb_sim, best_jaccard)
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
        title_tokens = extract_tokens(normalize_headline(title))

        match = self._find_best_cluster(vec, title_tokens, ts)

        if match:
            cluster, emb_sim, jac = match
            self._stats["duplicates"] += 1
            logger.info(
                "DUPLICATE (emb=%.2f jac=%.2f): [%s] %r  ~=  cluster #%d %r",
                emb_sim, jac, source, title[:80],
                cluster["id"], cluster["title"][:80],
            )
            self._add_to_cluster(cluster, title, vec, title_tokens, ts)
            self._mark_touched(cluster, source)

            if self.dry_run:
                return True
            return False

        # New event
        cluster = self._create_cluster(title, vec, title_tokens, ts)
        self._save_item(cluster["id"], title, source, url, vec, title_tokens, ts)
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

    def stats(self) -> dict:
        total_clusters = len(self._clusters)
        total_items = self._conn.execute(
            "SELECT COUNT(*) FROM cluster_items"
        ).fetchone()[0]
        return {**self._stats, "total_clusters": total_clusters,
                "total_items": total_items}

    def close(self):
        self._conn.close()
