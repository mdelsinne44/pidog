#!/usr/bin/env python3
"""Mémoire persistante PiDog — retrieval hybride (RAG lexical) sur SQLite.

Pourquoi ce module existe
-------------------------
L'ancienne version lisait ``~/GoogleDrive/memory.db`` (un mount rclone) et
injectait les 40 dernières lignes dans le prompt à chaque tour. Trois défauts :

  1. **SQLite sur FUSE rclone** : pas de flock fiable, ``I/O error`` au premier
     hoquet réseau, et chaque lecture coûte des dizaines de ms.
  2. **Aucune recherche** : ``LIMIT 40`` = tout le contenu, quel que soit le
     sujet. Ça ne passe pas à l'échelle et ça dilue le contexte du LLM.
  3. **Aucun cycle de vie** : les entrées de test restent éternellement.

Ici : base **locale** (``~/pidog/data/memory.db``), recherche **FTS5** (BM25)
+ bonus de récence/importance, un budget de caractères pour le prompt, et une
export JSON périodique vers Google Drive (fichier plat, pas de SQLite sur le
mount). Les embeddings vectoriels sont volontairement optionnels : sur un
Pi 4 / 2 Go, FTS5 + normalisation française donne l'essentiel du rappel pour
0 Mo de RAM. Voir ``SEMANTIC_MODEL`` plus bas pour la montée en gamme.

API stable (utilisée par le CLI, la voix et les outils) :

    memory_init()                       crée/migre la base
    memory_add(key, value, ...)         écrit un fait
    memory_search(query, k=6)           retrieval scoré
    memory_context(query, k=6)          bloc prêt à injecter dans le prompt
    memory_get(key)                     compat historique (liste de tuples)
    memory_forget(key)                  désactive une entrée
    memory_prune(days=180)              ménage
    memory_stats()                      compte / taille
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ------------------------------------------------------------------ config --
DATA_DIR = os.path.expanduser(os.getenv("PIDOG_DATA_DIR", "~/pidog/data"))
DB_PATH = os.path.expanduser(
    os.getenv("PIDOG_MEMORY_DB", os.path.join(DATA_DIR, "memory.db"))
)
LEGACY_DB = os.path.expanduser(
    os.getenv("PIDOG_MEMORY_LEGACY", "~/GoogleDrive/memory.db")
)
SNAPSHOT_PATH = os.path.expanduser(
    os.getenv("PIDOG_MEMORY_SNAPSHOT", "~/GoogleDrive/memory-export.json")
)
SNAPSHOT_MIN_INTERVAL = float(os.getenv("PIDOG_MEMORY_SNAPSHOT_SEC", "60"))
# Modèle d'embeddings optionnel (non utilisé par défaut : voir _semantic_score).
SEMANTIC_MODEL = os.getenv("PIDOG_EMBED_MODEL", "").strip()

_lock = threading.RLock()
_initialized = False
_last_snapshot = 0.0

# Bruit historique : entrées de test / doublons de mise au point.
_NOISE_RE = re.compile(r"(^|_)(test|tmp|demo|essai|debug|foo|bar)(_|$)", re.I)


# ------------------------------------------------------------------ utils --
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm(text: str) -> str:
    """Minuscule + sans accents (la recherche FTS retire déjà les diacritiques)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _slug(key: str) -> str:
    """Clé stable : minuscule, espaces/tirets → underscore."""
    key = _norm(key)
    key = re.sub(r"[^a-z0-9_]+", "_", key).strip("_")
    return key[:80] or "note"


# Un fait mémorisé avec « demain » devient faux le lendemain. On le date donc
# en absolu à l'écriture (et on rattrape les anciennes entrées au ménage).
_RELATIVE_RE = re.compile(
    r"\b(apr[eè]s[- ]demain|aujourd'?\s?hui|avant[- ]hier|demain|hier)\b", re.I)
_RELATIVE_OFFSETS = {
    "aujourdhui": 0, "demain": 1, "apresdemain": 2,
    "hier": -1, "avanthier": -2,
}


def _resolve_relative_dates(value: str, ref: Optional[datetime] = None) -> str:
    """Annote les dates relatives : « demain » → « demain (2026-11-04) ».

    Idempotent : si la date absolue est déjà présente, on ne touche à rien.
    """
    if not value:
        return value
    ref = ref or datetime.now()

    def repl(match: "re.Match[str]") -> str:
        word = match.group(0)
        delta = _RELATIVE_OFFSETS.get(re.sub(r"[^a-z]", "", _norm(word)))
        if delta is None:
            return word
        day = (ref + timedelta(days=delta)).strftime("%Y-%m-%d")
        if day in value:
            return word
        return f"{word} ({day})"

    return _RELATIVE_RE.sub(repl, value, count=1)



def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM memory_fts LIMIT 1")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ schéma --
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT    NOT NULL UNIQUE,
    value        TEXT    NOT NULL,
    topic        TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'fact',
    importance   INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    last_used_at TEXT,
    use_count    INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_memory_updated   ON memory(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_topic     ON memory(topic);
CREATE INDEX IF NOT EXISTS idx_memory_importance ON memory(importance DESC, updated_at DESC);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    key, value, topic,
    content='memory', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, key, value, topic)
    VALUES (new.id, new.key, new.value, new.topic);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, value, topic)
    VALUES ('delete', old.id, old.key, old.value, old.topic);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, value, topic)
    VALUES ('delete', old.id, old.key, old.value, old.topic);
    INSERT INTO memory_fts(rowid, key, value, topic)
    VALUES (new.id, new.key, new.value, new.topic);
END;
"""


def memory_init() -> None:
    """Crée la base locale, importe l'ancienne base, nettoie le bruit."""
    global _initialized
    with _lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            try:
                conn.executescript(_FTS_SCHEMA)
            except Exception as exc:  # FTS5 absent : la recherche retombera en LIKE
                print(f"[memoire] FTS5 indisponible ({exc}) — repli LIKE", flush=True)
            empty = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 0
            if empty:
                _import_legacy(conn)
            _cleanup_noise(conn)
        finally:
            conn.close()
        _initialized = True


# --------------------------------------------------------------- migration --
def _import_legacy(conn: sqlite3.Connection) -> int:
    """Importe l'ancienne table key/value (memory.db Google Drive)."""
    if not os.path.isfile(LEGACY_DB):
        return 0
    try:
        old = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True, timeout=10)
        old.row_factory = sqlite3.Row
        rows = old.execute(
            "SELECT key, value, updated_at FROM memory"
        ).fetchall()
        old.close()
    except Exception as exc:
        print(f"[memoire] import legacy impossible : {exc}", flush=True)
        return 0

    imported = 0
    for row in rows:
        key = _slug(row["key"])
        value = str(row["value"] or "").strip()
        if not key or not value:
            continue
        topic, kind, importance = _classify(key, value)
        conn.execute(
            "INSERT INTO memory (key, value, topic, kind, importance,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, value, topic, kind, importance,
             row["updated_at"] or _now(), row["updated_at"] or _now()),
        )
        imported += 1
    return imported


def _classify(key: str, value: str) -> Tuple[str, str, int]:
    """Devine topic/kind/importance à partir de la clé et du texte."""
    k, v = _norm(key), _norm(value)
    if any(w in k for w in ("createur", "femme", "fils", "fille", "nom",
                            "date_naissance", "anniversaire", "adresse")):
        return "profil", "fact", 3
    if any(w in k for w in ("rdv", "rendez", "coiffeur", "medecin", "docteur",
                            "dentiste", "reunion", "meeting", "date")):
        return "agenda", "event", 2
    if any(w in k for w in ("aime", "prefere", "deteste", "allergie",
                            "musique", "jeu", "film")):
        return "preferences", "preference", 2
    if any(w in v for w in ("demain", "aujourd", "lundi", "mardi",
                            "mercredi", "jeudi", "vendredi", "samedi", "dimanche")):
        return "agenda", "event", 2
    return "divers", "fact", 1


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text))


def _similar_values(a: str, b: str) -> bool:
    """Deux formulations du même fait ? (garde-fou sur les textes courts)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 12:
        return False
    import difflib
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    inter = len(wa & wb)
    union = len(wa | wb)
    # « Demain j'ai rdv coiffeur » ⊂ « Demain j'ai rdv chez le coiffeur. »
    # → containment élevé, même si le ratio de caractères reste moyen.
    return (union and inter / union >= 0.60) or inter / min(len(wa), len(wb)) >= 0.80


def _cleanup_noise(conn: sqlite3.Connection) -> int:
    """Désactive le bruit, date les faits relatifs, fusionne les quasi-doublons."""
    removed = 0
    for row in conn.execute(
        "SELECT id, key, value, importance, updated_at FROM memory WHERE active=1"
    ).fetchall():
        if _NOISE_RE.search(row["key"]) and len(str(row["value"])) < 40:
            conn.execute("UPDATE memory SET active=0 WHERE id=?", (row["id"],))
            removed += 1
            continue
        # « Demain j'ai rdv… » écrit le 29/10 : on fige la date visée pour que
        # le fait cesse d'être ambigu (et devienne lisible comme passé).
        try:
            ref = datetime.strptime(str(row["updated_at"]), "%Y-%m-%d %H:%M:%S")
        except Exception:
            ref = datetime.now()
        dated = _resolve_relative_dates(str(row["value"]), ref=ref)
        if dated != str(row["value"]):
            conn.execute("UPDATE memory SET value=? WHERE id=?",
                         (dated, row["id"]))

    # Doublons : même valeur (ou ratio > 0,90) → garder la clé et le texte les
    # plus complets, désactiver l'autre. Attrape « coiffeur » vs
    # « rdv_coiffeur » qui racontent la même chose avec 2 mots d'écart.
    kept: List[sqlite3.Row] = []
    kept_sig: List[str] = []
    for row in conn.execute(
        "SELECT id, key, value, updated_at FROM memory WHERE active=1"
        " ORDER BY updated_at DESC"
    ).fetchall():
        sig = _norm(str(row["value"]))
        duplicate = None
        for index, other in enumerate(kept_sig):
            if _similar_values(sig, other):
                duplicate = index
                break
        if duplicate is None:
            kept.append(row)
            kept_sig.append(sig)
            continue

        keep, drop = kept[duplicate], row
        if len(str(row["value"])) > len(str(keep["value"])):
            keep, drop = row, keep
        conn.execute("UPDATE memory SET active=0 WHERE id=?", (drop["id"],))
        conn.execute(
            "UPDATE memory SET topic = CASE WHEN topic='' THEN ? ELSE topic END"
            " WHERE id=?",
            (f"alias:{drop['key']}", keep["id"]),
        )
        removed += 1
    return removed


# ------------------------------------------------------------------ write --
def memory_add(key: str, value: str, topic: str = "", importance: int = 1,
               kind: str = "fact") -> int:
    """Écrit (upsert) un fait. Retourne l'id."""
    memory_init()
    key = _slug(key)
    value = _resolve_relative_dates(str(value or "").strip())
    if not key or not value:
        return 0
    if not topic or importance <= 1:
        auto_topic, auto_kind, auto_importance = _classify(key, value)
        topic = topic or auto_topic
        importance = max(importance, auto_importance)
        kind = kind if kind != "fact" else auto_kind

    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            existing = conn.execute(
                "SELECT id, value FROM memory WHERE key=?", (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memory SET value=?, topic=?, kind=?,"
                    " importance=MAX(importance, ?), updated_at=?, active=1"
                    " WHERE id=?",
                    (value, topic, kind, int(importance), now, existing["id"]),
                )
                row_id = int(existing["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO memory (key, value, topic, kind, importance,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (key, value, topic, kind, int(importance), now, now),
                )
                row_id = int(cur.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    _maybe_snapshot()
    return row_id


def memory_forget(key: str) -> int:
    """Désactive une entrée (soft delete, historique conservé)."""
    memory_init()
    key = _slug(key)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE memory SET active=0 WHERE active=1 AND"
                " (key=? OR key LIKE ?)", (key, f"%{key}%")
            )
            return cur.rowcount or 0
        finally:
            conn.close()


# ----------------------------------------------------------------- search --
def _tokens(query: str) -> List[str]:
    stop = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "ou",
            "a", "au", "aux", "en", "je", "tu", "il", "elle", "on", "nous",
            "vous", "ils", "que", "qui", "quoi", "est", "sont", "ai", "as",
            "pour", "avec", "dans", "sur", "mon", "ma", "mes", "ton", "ta",
            "me", "moi", "te", "toi", "ce", "cette", "ces", "s", "d", "l",
            "qu", "y", "ne", "pas", "plus", "tout", "tous"}
    return [t for t in _norm(query).split() if len(t) >= 3 and t not in stop][:12]


def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def memory_search(query: str, k: int = 6) -> List[Dict[str, Any]]:
    """Retrieval hybride : BM25 FTS5 + LIKE + récence + importance."""
    memory_init()
    query = (query or "").strip()
    now = datetime.now()

    with _lock:
        conn = _connect()
        try:
            scores: Dict[int, float] = {}
            rows: Dict[int, sqlite3.Row] = {}

            use_fts = _fts_available(conn)
            tokens = _tokens(query)
            if use_fts and tokens:
                fts_query = " OR ".join(f"{t}*" for t in tokens)
                try:
                    for hit in conn.execute(
                        "SELECT m.*, bm25(memory_fts, 2.0, 1.0, 1.4) AS rank"
                        " FROM memory_fts JOIN memory m ON m.id = memory_fts.rowid"
                        " WHERE memory_fts MATCH ? AND m.active=1"
                        " ORDER BY rank LIMIT 40",
                        (fts_query,),
                    ):
                        rows[hit["id"]] = hit
                        # bm25 renvoie une valeur négative : plus petit = meilleur.
                        scores[hit["id"]] = -float(hit["rank"] or 0.0) * 1.6
                except Exception:
                    pass

            # Complément : sous-chaîne exacte sur clé/valeur (les noms propres,
            # les dates et les mots courts échappent parfois à la tokenisation).
            if query:
                like = f"%{_norm(query)}%"
                for hit in conn.execute(
                    "SELECT * FROM memory WHERE active=1 AND"
                    " (key LIKE ? OR value LIKE ? OR topic LIKE ?)"
                    " ORDER BY updated_at DESC LIMIT 20",
                    (like, like, like),
                ):
                    rows.setdefault(hit["id"], hit)
                    scores[hit["id"]] = scores.get(hit["id"], 0.0) + 2.5

            # Toujours un socle : les faits importants récents.
            if len(rows) < k:
                for hit in conn.execute(
                    "SELECT * FROM memory WHERE active=1"
                    " ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (k * 2,),
                ):
                    rows.setdefault(hit["id"], hit)
                    scores.setdefault(hit["id"], 0.0)

            def final_score(row) -> float:
                base = scores.get(row["id"], 0.0)
                try:
                    age_days = (now - datetime.strptime(
                        row["updated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
                except Exception:
                    age_days = 365
                recency = 1.0 / (1.0 + age_days / 30.0)
                return base + recency + 0.25 * int(row["importance"] or 1)

            ordered = sorted(rows.values(), key=final_score, reverse=True)[:k]
            if ordered:
                stamps = [(now.strftime("%Y-%m-%d %H:%M:%S"), r["id"])
                          for r in ordered]
                conn.executemany(
                    "UPDATE memory SET last_used_at=?, use_count=use_count+1"
                    " WHERE id=?", stamps)
            return [_row_dict(r) for r in ordered]
        finally:
            conn.close()


def memory_context(query: str = "", k: int = 6, max_chars: int = 1100) -> str:
    """Bloc compact prêt à coller dans le prompt système.

    Contient les hits pertinents + les faits structurants (importance >= 2),
    tronqué à ``max_chars`` pour garder le tour de parole rapide.
    """
    memory_init()
    picked: Dict[int, Dict[str, Any]] = {}
    for row in memory_search(query, k=k):
        picked[row["id"]] = row
    if len(picked) < k:
        with _lock:
            conn = _connect()
            try:
                for row in conn.execute(
                    "SELECT * FROM memory WHERE active=1 AND importance>=2"
                    " ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (k,),
                ):
                    picked.setdefault(row["id"], _row_dict(row))
            finally:
                conn.close()

    lines, used = [], 0
    for row in picked.values():
        line = f"- {row['key']} : {row['value']}"
        if row.get("updated_at"):
            line += f" (maj {str(row['updated_at'])[:10]})"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else "(mémoire vide)"


# ------------------------------------------------------------- compat API --
def memory_get(key: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """Compat historique : liste de (key, value, updated_at)."""
    memory_init()
    with _lock:
        conn = _connect()
        try:
            if key:
                rows = conn.execute(
                    "SELECT key, value, updated_at FROM memory"
                    " WHERE active=1 AND (key LIKE ? OR value LIKE ?)"
                    " ORDER BY updated_at DESC LIMIT 5",
                    (f"%{_slug(key)}%", f"%{key}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value, updated_at FROM memory WHERE active=1"
                    " ORDER BY importance DESC, updated_at DESC LIMIT 40"
                ).fetchall()
        finally:
            conn.close()
    return [(r["key"], r["value"], r["updated_at"]) for r in rows]


def memory_check_topic(topic: str):
    """Alias historique : recherche libre sur un sujet."""
    return memory_get(topic)


def memory_all() -> List[Tuple[str, str, str]]:
    return memory_get()


def memory_for_date(date_str: str) -> List[Tuple[str, str, str]]:
    """Entrées qui mentionnent la date (AAAA-MM-JJ) ou datent de ce jour."""
    memory_init()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT key, value, updated_at, created_at FROM memory"
                " WHERE active=1 AND (updated_at LIKE ? OR created_at LIKE ?"
                " OR value LIKE ?) ORDER BY updated_at DESC",
                (f"{date_str}%", f"{date_str}%", f"%{date_str}%"),
            ).fetchall()
        finally:
            conn.close()
    return [(r["key"], r["value"], r["updated_at"]) for r in rows]


def memory_profile(max_items: int = 4) -> str:
    """Faits structurants uniquement (importance >= 3), pour le contexte de base.

    On ne colle PAS toute la mémoire dans chaque prompt : le reste est
    récupéré à la demande via `memory_search` (outil ou retrieval ciblé).
    """
    memory_init()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT key, value FROM memory WHERE active=1 AND importance>=3"
                " ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (max_items,),
            ).fetchall()
        finally:
            conn.close()
    return ", ".join(f"{r['key']}={r['value']}" for r in rows)


def memory_stats() -> Dict[str, Any]:
    memory_init()
    with _lock:
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE active=1").fetchone()[0]
            topics = conn.execute(
                "SELECT topic, COUNT(*) c FROM memory WHERE active=1"
                " GROUP BY topic ORDER BY c DESC").fetchall()
        finally:
            conn.close()
    size = os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0
    return {
        "db": DB_PATH,
        "size_kb": round(size / 1024, 1),
        "total": total,
        "active": active,
        "topics": {r["topic"] or "(sans)": r["c"] for r in topics},
    }


# ------------------------------------------------------------------ ménage --
def memory_cleanup() -> int:
    """Fusionne les quasi-doublons et désactive le bruit (idempotent).

    L'import initial le fait déjà ; cette fonction sert à relancer le ménage
    à la demande (`python3 memory.py cleanup`).
    """
    memory_init()
    with _lock:
        conn = _connect()
        try:
            return _cleanup_noise(conn)
        finally:
            conn.close()


def memory_prune(days: int = 180) -> int:
    """Soft-delete : petites entrées non utilisées et anciennes."""
    memory_init()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE memory SET active=0 WHERE active=1 AND importance<=1"
                " AND use_count=0 AND updated_at < ?", (cutoff,))
            soft = cur.rowcount or 0
            # Purge physique au-delà d'un an d'inactivité.
            hard_cut = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "DELETE FROM memory WHERE active=0 AND updated_at < ?",
                (hard_cut,))
            return soft
        finally:
            conn.close()


def _maybe_snapshot(force: bool = False) -> None:
    """Export JSON vers Google Drive (fichier plat : pas de SQLite sur FUSE)."""
    global _last_snapshot
    if not force and time.time() - _last_snapshot < SNAPSHOT_MIN_INTERVAL:
        return
    _last_snapshot = time.time()
    try:
        if not os.path.isdir(os.path.dirname(SNAPSHOT_PATH)):
            return
        with _lock:
            conn = _connect()
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT key, value, topic, kind, importance, created_at,"
                    " updated_at FROM memory WHERE active=1 ORDER BY key")]
            finally:
                conn.close()
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "exported_at": _now(), "entries": rows},
                      fh, ensure_ascii=False, indent=1)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception as exc:
        print(f"[memoire] snapshot ignoré : {exc}", flush=True)


def memory_snapshot() -> str:
    """Force l'export JSON et retourne le chemin."""
    _maybe_snapshot(force=True)
    return SNAPSHOT_PATH


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "search":
        for row in memory_search(" ".join(sys.argv[2:])):
            print(f"{row['key']} = {row['value']}  [{row['updated_at']}]")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "prune":
        print("désactivées :", memory_prune())
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        print("fusionnées/désactivées :", memory_cleanup())
        sys.exit(0)
    memory_init()
    print("Base :", DB_PATH)
    print(json.dumps(memory_stats(), indent=2, ensure_ascii=False))
    print("--- contexte vide ---")
    print(memory_context(""))
