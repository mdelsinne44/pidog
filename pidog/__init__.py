#!/usr/bin/env python3
from .pidog import Pidog
from robot_hat import utils
from time import sleep
from .version import __version__

# ── Service partagé (le chien unique, accessible partout) ──
# Le singleton matériel vit dans le processus daemon lancé par `pidog start`.
# Toutes les fonctions ci-dessous sont des clients : elles ne créent jamais
# de second Pidog() et ne touchent pas au GPIO.
from .service import (
    start,
    stop,
    status,
    service_state,
    is_ready,
    my_dog,
    get_pidog,
    release_pidog,
    park,
)
from .robot_actions import (
    ACTION_CATALOG,
    RobotActions,
)

# `my_dog`/`get_pidog()` renvoient le proxy RPC du daemon.
# `release_pidog()`/`park()` couchent le robot sans arrêter le daemon.

def __main__():
    print(f"Thanks for using Pidog {__version__} ! woof, woof, woof !")
    utils.reset_mcu()
    sleep(0.2)
