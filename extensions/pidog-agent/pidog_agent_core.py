#!/usr/bin/env python3
"""Ancien module `pidog_agent_core` — conservé comme simple compatibilité.

ATTENTION : ce module ne crée plus aucun Pidog(). Le seul propriétaire du
matériel est le daemon lancé par `pidog start`. Les fonctions ci-dessous sont
donc des clients du service partagé.

Si vous cherchez l'ancien singleton local : il a été supprimé volontairement.
Il provoquait les erreurs « GPIO busy » en créant un deuxième Pidog().
"""
import subprocess
from time import sleep

from pidog import (
    get_pidog,
    my_dog,
    is_ready,
    park,
    release_pidog,
    start,
    status,
    stop,
    service_state,
)
from pidog.service import SOCKET_PATH


def stop_service():
    """Arrêt complet du service partagé (daemon + robot)."""
    return stop()


def reboot():
    stop()
    subprocess.run(["sudo", "reboot"], capture_output=True, timeout=10)


def shutdown():
    stop()
    subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True, timeout=10)


def head_toward_sound():
    """Tourne la tête vers un son détecté, via le daemon (RPC)."""
    d = get_pidog()
    if not is_ready():
        return False
    detected = False
    direction = 0
    for _ in range(30):
        if d.ears.isdetected():
            direction = d.ears.read()
            print(f"DEBUG sound_direction: direction={direction}")
            detected = True
            break
        sleep(0.1)
    if detected and direction >= 0:
        # Convention du module : 0 = devant, 90 = droite, 270 = gauche.
        # yaw positif = gauche (cf. pidog/robot_actions.py).
        if 0 < direction < 180:
            yaw = -direction          # son à droite → yaw négatif
        elif 180 < direction < 360:
            yaw = 360 - direction     # son à gauche → yaw positif
        else:
            yaw = 0
        yaw = max(-80, min(80, yaw))
        d.head_move([[yaw, 0, 0]], immediately=True, speed=50)
        d.wait_all_done()
        d.stable()
        d.rgb_strip.set_mode(style="listen", color="yellow", brightness=1)
        print("DEBUG head_toward_sound: head moved toward sound")
        sleep(1.5)
    else:
        d.head_move([[10, 0, 0]], immediately=True, speed=50)
        d.wait_all_done()
        d.stable()
        d.rgb_strip.set_mode(style="listen", color="black")
        sleep(1.5)
    return True
