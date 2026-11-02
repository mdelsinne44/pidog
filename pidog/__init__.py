#!/usr/bin/env python3
from .pidog import Pidog
from robot_hat import utils
from time import sleep
from .version import __version__

# ── Service partagé (le chien unique, accessible partout) ──
from .service import start, stop, status, my_dog, get_pidog, release_pidog

# `my_dog` est déjà défini via `from .service import my_dog`, aucun besoin de le redéfinir.
# `get_pidog()` et `release_pidog()` sont aussi importés directement.

def __main__():
    print(f"Thanks for using Pidog {__version__} ! woof, woof, woof !")
    utils.reset_mcu()
    sleep(0.2)