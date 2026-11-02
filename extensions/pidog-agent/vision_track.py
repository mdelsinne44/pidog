#!/usr/bin/env python3
"""Vision tracking — lancé séparément, utilise le robot UNIQUEMENT via le service."""
import sys, time, threading
sys.path.insert(0, "/home/mdelsinne/pidog")

dog_proxy = None

def get_proxy():
    global dog_proxy
    if dog_proxy is None:
        try:
            from pidog.service import my_dog
            dog_proxy = my_dog()
        except Exception:
            pass
    return dog_proxy

def track_loop():
    while True:
        try:
            d = get_proxy()
            # Vérifie si le robot est initialisé dans le proxy
            if hasattr(d, 'dog') and d.dog is not None:
                # Tracking visage simple (exemple : head move vers le centre)
                # À adapter avec ton capteur (caméra + vision)
                try:
                    d.dog.head_move([[0, 0, 0]], immediately=True, speed=30)
                except Exception:
                    pass
            else:
                pass  # robot pas prêt, on attend
        except Exception:
            pass
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=track_loop, daemon=True).start()
    print("vision_track : tracking prêt (utilise le service si robot dispo)")
    while True:
        time.sleep(10)
