#!/usr/bin/env python3
"""Mémoire persistante SQLite — base existante /home/mdelsinne/GoogleDrive/memory.db (table memory : id, key, value, updated_at)"""
import sqlite3
import threading
import os
import time
from datetime import datetime, timedelta

DB_PATH = "/home/mdelsinne/GoogleDrive/memory.db"
# Si le chemin calculé n'existe pas, retomber sur le dossier GoogleDrive absolu connu
if not os.path.isfile(DB_PATH):
    DB_PATH = "/home/mdelsinne/GoogleDrive/memory.db"

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def memory_init():
    # La table existe déjà dans la DB gdrive, on ne recrée rien
    with _lock:
        conn = _connect()
        conn.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, value TEXT, updated_at TEXT)")
        conn.commit()
        conn.close()


def memory_add(key: str, value: str):
    memory_init()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO memory (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_str),
        )
        conn.commit()
        conn.close()


def memory_get(key: str = None):
    memory_init()
    with _lock:
        conn = _connect()
        if key:
            row = conn.execute("SELECT key, value, updated_at FROM memory WHERE LOWER(key) LIKE ? ORDER BY updated_at DESC LIMIT 5", (f"%{key.lower()}%",)).fetchall()
        else:
            row = conn.execute("SELECT key, value, updated_at FROM memory ORDER BY updated_at DESC LIMIT 40").fetchall()
        conn.close()
    result = []
    for r in row:
        result.append((r["key"], r["value"], r["updated_at"]))
    return result


def memory_for_date(date_str: str):
    # date_str au format YYYY-MM-DD
    memory_init()
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT key, value, updated_at FROM memory ORDER BY updated_at DESC").fetchall()
        conn.close()
    # Filtre simple par mention de la date dans la clé ou la valeur (approche heuristique)
    result = []
    for r in rows:
        # On conserve tout — le LLM filtrera sémantiquement
        result.append((r["key"], r["value"], r["updated_at"]))
    return result


def memory_check_topic(topic: str):
    rows = memory_get(topic)
    return rows


if __name__ == "__main__":
    memory_init()
    print("DB utilisée :", DB_PATH)
    print("Contenu actuel :")
    for k, v, u in memory_get():
        print(f"  {k} = {v} (maj {u})")
