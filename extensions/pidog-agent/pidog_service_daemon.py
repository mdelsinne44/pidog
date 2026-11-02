#!/usr/bin/env python3
"""Daemon du service PiDog — reste en vie après le lancement du CLI."""
import sys, os, time
sys.path.insert(0, "/home/mdelsinne/pidog")
from pidog.service import _proxy, _clear_lock, _write_lock_pid, stop

if __name__ == "__main__":
    # Lancer le service (crée le socket + lance le robot au démarrage)
    _proxy.start(auto_start_robot=True)
    # Écrire le lockfile du daemon
    _write_lock_pid()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception:
        pass
    finally:
        _clear_lock()
        try:
            _proxy.stop()
        except Exception:
            pass
