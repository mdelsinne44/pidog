#!/usr/bin/env python3
"""Charge la mémoire SQLite depuis Google Drive (~/GoogleDrive/memory.db)."""
import sqlite3, os

def load_memory():
    path = os.path.expanduser("~/GoogleDrive/memory.db")
    mem = {}
    if not os.path.exists(path):
        return mem
    try:
        conn = sqlite3.connect(path)
        for row in conn.execute("SELECT key, value FROM memory"):
            mem[row[0]] = row[1]
        conn.close()
    except Exception as e:
        print(f"[MEMOIRE] Erreur lecture : {e}")
    return mem
