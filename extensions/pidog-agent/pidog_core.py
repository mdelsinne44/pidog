#!/usr/bin/env python3
"""Compatibilité : ancien module `pidog_core` (avant le daemon partagé).

Le singleton matériel n'existe PLUS ici. Tout passe par le daemon lancé par
`pidog start`, via `pidog.service`. Ce module ne sert qu'à ne pas casser les
imports historiques (`from pidog_core import get_pidog`).

NE JAMAIS remettre un `Pidog()` ici : cela recréerait un second propriétaire
du GPIO/I2C.
"""
import subprocess

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

__all__ = [
    "get_pidog", "my_dog", "is_ready", "park", "release_pidog",
    "start", "status", "stop", "service_state", "stop_service",
    "reboot", "shutdown", "SOCKET_PATH",
]


def stop_service():
    """Arrêt officiel du service partagé (daemon + robot)."""
    return stop()


def reboot():
    """Arrêt du robot puis redémarrage système (action explicite uniquement)."""
    stop()
    subprocess.run(["sudo", "reboot"], capture_output=True, timeout=10)


def shutdown():
    """Arrêt du robot puis extinction du système (action explicite uniquement)."""
    stop()
    subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True, timeout=10)
