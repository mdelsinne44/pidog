#!/usr/bin/env python3
"""Vision tracking — lancé séparément, utilise le robot UNIQUEMENT via le service."""
import sys, time, threading
sys.path.insert(0, "/home/mdelsinne/pidog")

dog_proxy = None


def get_proxy():
    global dog_proxy
    if dog_proxy is None:
        try:
            import pidog
            dog_proxy = pidog.get_pidog()
        except Exception:
            pass
    return dog_proxy


def track_loop():
    while True:
        try:
            import pidog
            if pidog.is_ready():
                d = get_proxy()
                # Tracking visage simple (exemple : head move vers le centre)
                # À adapter avec ton capteur (caméra + vision)
                d.head_move([[0, 0, 0]], immediately=True, speed=30)
        except Exception:
            pass
        time.sleep(2)


if __name__ == "__main__":
    threading.Thread(target=track_loop, daemon=True).start()
    print("vision_track : tracking prêt (utilise le service si robot dispo)")
    while True:
        time.sleep(10)
