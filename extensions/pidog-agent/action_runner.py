#!/usr/bin/env python3
"""Exécution partagée des blocs d'action générés par le LLM.

CLI texte (`cli/agent_cli.py`) et voix (`stream/agent_stream.py`) utilisent le
même extracteur et le même exécuteur. Objectif : un seul comportement, un seul
jeu d'actions autorisées, et toujours le robot via le daemon (jamais de GPIO
local).

Le LLM peut écrire :

    <<<CODE>>>
    dog.action("posture", name="stand")
    dog.action("led", color="rainbow", style="breath")
    <<<END_CODE>>>

Toutes les méthodes ``dog.action(...)`` passent par `pidog.robot_actions`, qui
gère les paliers de posture (assis entre couché et debout) et la tension.
"""

from __future__ import annotations

import datetime as _datetime
import os
import re
import subprocess
import time
import traceback
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "extract_action_code",
    "clean_reply",
    "execute_action_block",
    "build_action_env",
    "ACTION_RE",
]

# Tolère <<<CODE>>>, <<CODE>>, espaces, majuscules et blocs markdown ```python.
ACTION_RE = re.compile(
    r"<<<\s*CODE\s*>>>\s*(.*?)\s*<<<\s*END_CODE\s*>>>"
    r"|<<\s*CODE\s*>>\s*(.*?)\s*<<\s*END_CODE\s*>>"
    r"|```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_action_code(reply: str) -> Tuple[str, Optional[str]]:
    """Retourne (texte_nettoyé, code_ou_None).

    Le code est retiré de la réponse affichée/parlée. Les blocs markdown ne
    sont considérés comme du code d'action que s'ils contiennent un appel
    reconnaissable (dog., pidog., kodi_, retropie_, memory_...).
    """
    reply = reply or ""
    match = ACTION_RE.search(reply)
    if not match:
        return reply.strip(), None
    code = next((g for g in match.groups() if g is not None), "")
    # Un bloc ```...``` purement décoratif ne doit pas être exécuté.
    if match.group(3) is not None and not _looks_like_action(code):
        return reply.strip(), None
    cleaned = (reply[: match.start()] + reply[match.end():]).strip()
    return cleaned, code.strip()


def clean_reply(reply: str) -> str:
    """Retire les blocs d'action de la réponse (pour l'affichage / le TTS)."""
    return extract_action_code(reply)[0]


def _looks_like_action(code: str) -> bool:
    return bool(re.search(
        r"\b(dog|robot|pidog|pidog_core)\s*\.|"
        r"\b(kodi_|retropie_|memory_|reboot|shutdown|stop_service)\b",
        code or "",
    ))


def _systemctl(action: str, unit: str):
    try:
        subprocess.run(["systemctl", action, unit], capture_output=True, timeout=10)
    except Exception:
        pass


def _cec(command: str):
    """Commande HDMI-CEC (TV) si cec-client est présent."""
    try:
        subprocess.run(["cec-client", "-s", "-d", "1", command],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def build_action_env(debug_log=None) -> Dict[str, Any]:
    """Construit l'environnement d'exécution restreint du bloc CODE."""
    import pidog
    import pidog_core
    import memory

    def memory_add(key, value):
        return memory.memory_add(str(key), str(value))

    def memory_get(key=""):
        rows = memory.memory_get(str(key) if key else None)
        return [(k, v, u) for k, v, u in rows]

    def memory_check_topic(topic=""):
        return memory.memory_check_topic(str(topic))

    def memory_all():
        return memory.memory_get()

    def memory_today():
        return memory.memory_for_date(_datetime.datetime.now().strftime("%Y-%m-%d"))

    def stop_service():
        return pidog.stop()

    def retropie_start():
        _systemctl("start", "retropie")

    def retropie_stop():
        _systemctl("stop", "retropie")

    def retropie_game_cmd(game=""):
        # Remplacé plus tard par un lanceur EmulationStation si disponible.
        return str(game)

    def retropie_list_cmd():
        return []

    def kodi_start():
        _systemctl("start", "kodi")

    def kodi_stop():
        _systemctl("stop", "kodi")

    def kodi_tv_on():
        _cec("on 0")

    def kodi_tv_off():
        _cec("standby 0")

    def kodi_video_cmd():
        return None

    def kodi_pause():
        return None

    def reboot():
        pidog.stop()
        subprocess.run(["sudo", "reboot"], capture_output=True, timeout=10)

    def shutdown():
        pidog.stop()
        subprocess.run(["sudo", "shutdown", "-h", "now"],
                       capture_output=True, timeout=10)

    dog = pidog.get_pidog()
    return {
        "__builtins__": {},   # pas d'import/exec/open implicite
        "dog": dog,
        "robot": dog,         # alias lisible pour le LLM
        "get_pidog": pidog.get_pidog,
        "pidog": pidog,
        "pidog_core": pidog_core,
        "self_info": __import__("agent_self").self_info,
        "memory_add": memory_add,
        "memory_get": memory_get,
        "memory_check_topic": memory_check_topic,
        "memory_all": memory_all,
        "memory_today": memory_today,
        "stop_service": stop_service,
        "retropie_start": retropie_start,
        "retropie_stop": retropie_stop,
        "retropie_game_cmd": retropie_game_cmd,
        "retropie_list_cmd": retropie_list_cmd,
        "kodi_start": kodi_start,
        "kodi_stop": kodi_stop,
        "kodi_tv_on": kodi_tv_on,
        "kodi_tv_off": kodi_tv_off,
        "kodi_video_cmd": kodi_video_cmd,
        "kodi_pause": kodi_pause,
        "reboot": reboot,
        "shutdown": shutdown,
        "sleep": time.sleep,
        "datetime": _datetime,
        "time": time,
        "json": __import__("json"),
    }


def execute_action_block(code: str, debug_log=None) -> Tuple[bool, str]:
    """Exécute le code d'action via le daemon. Retourne (ok, détail)."""
    import pidog

    code = (code or "").strip()
    if not code:
        return False, "bloc vide"

    if not pidog.is_ready():
        state = pidog.service_state()
        return False, (
            f"robot indisponible ({state.get('state')}: "
            f"{state.get('error') or 'daemon non prêt'})"
        )

    if debug_log:
        debug_log("ACTION_CODE", code.replace("\n", " ; ")[:300])

    # Cas simple : le LLM renvoie juste un nom d'action ("stand", "wag_tail").
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", code):
        try:
            pidog.get_pidog().action(code)
            return True, code
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    env = build_action_env(debug_log=debug_log)
    try:
        exec(compile(code, "<pidog-action>", "exec"), env)
        return True, code.replace("\n", " ; ")[:200]
    except Exception as exc:
        if debug_log:
            debug_log("ACTION_EXCEPTION",
                      f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return False, f"{type(exc).__name__}: {exc}"
