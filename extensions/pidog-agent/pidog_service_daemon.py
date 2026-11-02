#!/usr/bin/env python3
"""Daemon du service PiDog — possède l'unique Pidog() et le socket.

Ce script est lancé par `pidog start` (détaché) ou par systemd
(pidog.service). Toute la logique (lock, socket, init, arrêt propre) est
dans `pidog.service.DogService`.
"""
import os
import sys

REPO_ROOT = "/home/mdelsinne/pidog"
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pidog.service import run_daemon

if __name__ == "__main__":
    # Un daemon doit être indépendant du terminal qui l'a lancé.
    try:
        os.setsid()
    except Exception:
        pass
    sys.exit(run_daemon(auto_start_robot=True))
