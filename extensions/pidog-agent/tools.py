#!/usr/bin/env python3
"""Couche d'outils PiDog — function calling partagé CLI / voix.

Un seul endroit pour : le schéma des outils exposés au LLM, et leur exécution
réelle. Le CLI (agent_cli.py) et le stream vocal peuvent donc appeler les
mêmes fonctions avec les mêmes garde-fous.

Principes :
  * **Tout passe par le daemon** pour le matériel (`pidog.get_pidog()` est un
    proxy RPC) : aucun outil ne crée de ``Pidog()`` local.
  * **Lecture seule par défaut** pour le shell. ``PIDOG_SHELL_MODE`` vaut
    ``readonly`` (défaut), ``off`` ou ``full`` (assumé, pour Martin).
  * **Jamais d'exception** : un outil retourne ``{"ok": false, "error": ...}``
    que le LLM peut lire et corriger.

Utilisation :
    from tools import TOOLS, execute_tool
    result = execute_tool("robot_action", {"action": "posture",
                                           "params": {"name": "sit"}})
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# Bootstrap des chemins : tools.py doit fonctionner seul (python3 tools.py)
# comme importé par le CLI ou le stream.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
for _path in (_REPO, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import memory

MAX_OUTPUT = 4000          # caractères renvoyés au LLM par outil
SHELL_TIMEOUT = 15
SHELL_MODE = os.getenv("PIDOG_SHELL_MODE", "readonly").strip().lower()
CITY_DEFAULT = os.getenv("PIDOG_CITY", "Nantes")

_env_cache: Dict[str, Any] = {"at": 0.0, "value": {}}


# ------------------------------------------------------------------ schemas --
def _fn(name: str, description: str, properties: Dict[str, Any],
        required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOLS: List[Dict[str, Any]] = [
    _fn(
        "robot_action",
        "Action physique du PiDog via le daemon (posture, tête, LED, queue, "
        "patte, marche, sons, tension). Le daemon gère posture et batterie.",
        {
            "action": {"type": "string",
                       "description": "posture, head_gesture, led, wag, tail, "
                                      "lift_paw, wave_paw, handshake, high_five, "
                                      "walk, turn, trot, stretch, push_up, "
                                      "celebrate, bark, howl, sound, battery, "
                                      "stop, rest, head"},
            "params": {"type": "object", "additionalProperties": True,
                       "description": "ex. {\"name\":\"sit\"}, "
                                      "{\"color\":\"rainbow\"}, "
                                      "{\"direction\":\"forward\",\"steps\":1,"
                                      "\"speed\":80}"},
        },
        ["action"],
    ),
    _fn("robot_state",
        "État du robot : daemon prêt, tension batterie, audio/réseau/Bluetooth.",
        {}),
    _fn("robot_actions",
        "Liste les actions physiques disponibles et leurs paramètres.", {}),
    _fn("memory_search",
        "Cherche dans la mémoire persistante (profil, agenda, préférences, projets).",
        {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]),
    _fn("memory_add",
        "Enregistre un fait durable (pas la conversation courante).",
        {"key": {"type": "string"}, "value": {"type": "string"},
         "topic": {"type": "string",
                   "description": "profil, agenda, preferences, projet, divers"},
         "importance": {"type": "integer", "description": "0..3"}},
        ["key", "value"]),
    _fn("memory_forget", "Désactive une entrée mémoire obsolète ou fausse.",
        {"key": {"type": "string"}}, ["key"]),
    _fn("sys_info",
        "Raspberry Pi : uptime, charge, RAM, disque, température, throttling.", {}),
    _fn("sys_services",
        "État des services clés (pidog, stream, monitor, kodi, retropie…).", {}),
    _fn("sys_logs", "Dernières lignes du journal d'une unité systemd.",
        {"unit": {"type": "string"},
         "lines": {"type": "integer", "description": "défaut 30, max 200"}},
        ["unit"]),
    _fn("service_ctl",
        "Démarre/arrête/redémarre un service whitelist — uniquement sur demande "
        "explicite de Martin.",
        {"action": {"type": "string",
                    "enum": ["start", "stop", "restart", "status"]},
         "unit": {"type": "string"},
         "explicit": {"type": "boolean",
                      "description": "true seulement si Martin l'a demandé"}},
        ["action", "unit"]),
    _fn("shell",
        "Commande shell de LECTURE (ls, cat, grep, tail, df, systemctl status, "
        "journalctl, git log…). Pas d'écriture, pas de sudo.",
        {"cmd": {"type": "string", "description": "commande simple, sans | ni >"},
         "cwd": {"type": "string", "description": "défaut ~/pidog"}},
        ["cmd"]),
    _fn("weather", "Météo actuelle (open-meteo).",
        {"city": {"type": "string"}}),
    _fn("media", "Kodi, RetroPie, TV (HDMI-CEC) ou webradio FIP/NOVA.",
        {"action": {"type": "string",
                    "enum": ["start", "stop", "play", "pause", "on", "off"]},
         "target": {"type": "string", "enum": ["kodi", "retropie", "tv", "fip", "nova"]}},
        ["action", "target"]),
    _fn("self_info", "Détail matériel et défauts connus du PiDog.",
        {"topic": {"type": "string"}}),
]


# ------------------------------------------------------------------ helpers --
def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_OUTPUT:
        return value[:MAX_OUTPUT] + f"\n… ({len(value) - MAX_OUTPUT} car. tronqués)"
    return value


def _run(cmd: List[str], timeout: int = SHELL_TIMEOUT,
         cwd: Optional[str] = None) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"timeout {timeout}s"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}"}


def _pidog():
    import pidog
    return pidog


# --------------------------------------------------------------- exécution --
def _robot_action(args: Dict[str, Any]) -> Dict[str, Any]:
    action = args.get("action") or args.get("name") or ""
    params = dict(args.get("params") or {})
    for key, value in args.items():
        if key not in ("action", "params", "name"):
            params.setdefault(key, value)
    if not action:
        return {"ok": False, "error": "action manquante"}
    if str(action).lower() in ("reboot", "shutdown", "poweroff"):
        return {"ok": False,
                "error": "action système refusée : demande explicite requise"}
    try:
        pidog = _pidog()
        if not pidog.is_ready():
            state = pidog.service_state()
            return {"ok": False,
                    "error": f"robot indisponible ({state.get('state')}: "
                             f"{state.get('error') or 'daemon non prêt'})"}
        result = pidog.get_pidog().action(str(action), **params)
        return {"ok": True, "action": action, "params": params, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _robot_state(_args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import pidog
        state = pidog.service_state()
        out.update({k: state.get(k) for k in ("state", "ready", "error", "uptime_s")})
        if state.get("ready"):
            try:
                out["battery_v"] = pidog.get_pidog().get_battery_voltage()
            except Exception as exc:
                out["battery_error"] = str(exc)
    except Exception as exc:
        out["error"] = str(exc)
    try:
        import env_vision
        now = time.time()
        if now - _env_cache["at"] > 60:
            _env_cache["value"] = env_vision.env_summary()
            _env_cache["at"] = now
        out["env"] = _env_cache["value"]
    except Exception:
        pass
    return {"ok": True, **out}


def _robot_actions(_args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from pidog.robot_actions import ACTION_ALIASES, POSTURES, GAITS
        return {"ok": True,
                "actions": sorted(set(ACTION_ALIASES) | set(POSTURES) | set(GAITS)),
                "params": {
                    "posture": ["name"],
                    "head_gesture": ["name"],
                    "led": ["color", "style", "brightness", "off"],
                    "walk": ["direction", "steps", "speed"],
                    "turn": ["direction", "steps", "speed"],
                    "wag": ["times", "amplitude"],
                    "lift_paw": ["leg"],
                }}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _memory_search(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = memory.memory_search(str(args.get("query") or ""),
                                int(args.get("k") or 6))
    return {"ok": True, "count": len(rows),
            "results": [{"key": r["key"], "value": r["value"],
                         "topic": r.get("topic"), "updated": r.get("updated_at")}
                        for r in rows]}


def _memory_add(args: Dict[str, Any]) -> Dict[str, Any]:
    row_id = memory.memory_add(
        str(args.get("key") or ""), str(args.get("value") or ""),
        topic=str(args.get("topic") or ""),
        importance=int(args.get("importance") or 1),
    )
    if not row_id:
        return {"ok": False, "error": "clé ou valeur vide"}
    return {"ok": True, "id": row_id,
            "key": str(args.get("key")), "value": str(args.get("value"))}


def _memory_forget(args: Dict[str, Any]) -> Dict[str, Any]:
    count = memory.memory_forget(str(args.get("key") or ""))
    return {"ok": count > 0, "removed": count}


def _sys_info(_args: Dict[str, Any]) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    for key, cmd in (
        ("uptime", ["uptime"]),
        ("loadavg", ["cat", "/proc/loadavg"]),
        ("mem", ["free", "-m"]),
        ("disk", ["df", "-h", "/"]),
        ("temp", ["cat", "/sys/class/thermal/thermal_zone0/temp"]),
    ):
        res = _run(cmd, timeout=5)
        if res["returncode"] == 0:
            info[key] = res["stdout"]
    raw = info.get("temp")
    if raw and raw.strip().isdigit():
        info["temp_c"] = round(int(raw.strip()) / 1000.0, 1)
        info.pop("temp", None)
    throttled = _run(["vcgencmd", "get_throttled"], timeout=5)
    if throttled["returncode"] == 0:
        info["throttled"] = throttled["stdout"]
    return {"ok": True, **info}


ALLOWED_UNITS = {
    "pidog", "pidog.service", "pidog-agent-stream", "pidog-agent-stream.service",
    "pidog-monitor", "pidog-monitor.service", "kodi", "retropie",
    "bluetooth", "ssh", "NetworkManager", "lightdm", "x11vnc",
}

SERVICE_WHITELIST = {
    "pidog", "pidog-agent-stream", "pidog-monitor", "kodi", "retropie",
    "bluetooth",
}


def _sys_services(_args: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for unit in sorted(SERVICE_WHITELIST):
        res = _run(["systemctl", "is-active", unit], timeout=5)
        out[unit] = res["stdout"] or "unknown"
    return {"ok": True, "services": out}


def _sys_logs(args: Dict[str, Any]) -> Dict[str, Any]:
    unit = str(args.get("unit") or "").strip()
    if unit not in ALLOWED_UNITS:
        return {"ok": False, "error": f"unité non autorisée : {unit}",
                "allowed": sorted(ALLOWED_UNITS)}
    lines = max(1, min(int(args.get("lines") or 30), 200))
    res = _run(["journalctl", "-u", unit, "-n", str(lines),
                "--no-pager", "-o", "short-iso"], timeout=10)
    if res["returncode"] != 0:
        return {"ok": False, "error": res["stderr"] or "journalctl a échoué"}
    return {"ok": True, "unit": unit, "logs": res["stdout"]}


def _service_ctl(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip()
    unit = str(args.get("unit") or "").strip()
    if unit not in SERVICE_WHITELIST:
        return {"ok": False, "error": f"unité non autorisée : {unit}",
                "allowed": sorted(SERVICE_WHITELIST)}
    if action not in ("start", "stop", "restart", "status"):
        return {"ok": False, "error": f"action non autorisée : {action}"}
    if action == "status":
        res = _run(["systemctl", "status", unit, "--no-pager", "-n", "5"],
                   timeout=8)
        return {"ok": True, "status": res["stdout"] or res["stderr"]}
    if unit in ("pidog", "pidog-agent-stream") and not args.get("explicit"):
        return {"ok": False,
                "error": "changement d'état refusé : Martin doit le demander "
                         "explicitement (relancer avec explicit=true seulement "
                         "s'il l'a demandé)"}
    res = _run(["sudo", "-n", "systemctl", action, unit], timeout=20)
    if res["returncode"] != 0:
        return {"ok": False, "error": res["stderr"] or res["stdout"],
                "hint": "sudo sans mot de passe requis"}
    check = _run(["systemctl", "is-active", unit], timeout=5)
    return {"ok": True, "unit": unit, "action": action,
            "active": check["stdout"]}


# Lecture seule : premier mot + sous-commande éventuellement contrôlés.
_READ_BINARIES = {
    "ls", "cat", "head", "tail", "grep", "rg", "wc", "find", "du", "df",
    "free", "uptime", "uname", "hostname", "date", "whoami", "id", "ps",
    "pgrep", "pstree", "lsblk", "ss", "sensors", "stat", "file", "sort",
    "uniq", "cut", "tr", "diff", "tree", "which", "type", "lscpu", "lsusb",
    "pactl", "vcgencmd", "aplay", "arecord", "ip", "i2cdetect",
}
_READ_SUBCMDS = {
    "systemctl": {"status", "is-active", "is-enabled", "show", "cat",
                  "list-units", "list-timers", "list-sockets", "get-default"},
    "journalctl": {"*"},
    "git": {"status", "log", "show", "diff", "branch", "remote", "describe",
            "rev-parse", "blame", "tag"},
    "pip3": {"list", "show", "freeze"},
    "python3": {"--version", "-V"},
    "sqlite3": {"*"},
}
_FORBIDDEN = re.compile(r"[;&|><`$\n\r]")


def _shell(args: Dict[str, Any]) -> Dict[str, Any]:
    if SHELL_MODE == "off":
        return {"ok": False, "error": "shell désactivé (PIDOG_SHELL_MODE=off)"}
    raw = str(args.get("cmd") or "").strip()
    if not raw:
        return {"ok": False, "error": "commande vide"}
    if SHELL_MODE != "full":
        if _FORBIDDEN.search(raw):
            return {"ok": False,
                    "error": "caractères de composition interdits en mode "
                             "readonly (; | > < ` $)".replace("$", "$")}
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            return {"ok": False, "error": f"commande illisible : {exc}"}
        if not parts:
            return {"ok": False, "error": "commande vide"}
        binary = os.path.basename(parts[0])
        if binary == "sudo":
            return {"ok": False, "error": "sudo interdit"}
        if binary in _READ_SUBCMDS:
            sub = parts[1] if len(parts) > 1 else ""
            allowed = _READ_SUBCMDS[binary]
            if "*" not in allowed and sub not in allowed:
                return {"ok": False,
                        "error": f"sous-commande non autorisée : {binary} {sub}",
                        "allowed": sorted(allowed)}
        elif binary not in _READ_BINARIES:
            return {"ok": False, "error": f"commande non autorisée : {binary}",
                    "hint": "PIDOG_SHELL_MODE=full pour tout autoriser"}
    cwd = os.path.expanduser(str(args.get("cwd") or "~/pidog"))
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    res = _run(raw, timeout=SHELL_TIMEOUT, cwd=cwd) if SHELL_MODE == "full" \
        else _run(shlex.split(raw), timeout=SHELL_TIMEOUT, cwd=cwd)
    return {"ok": res["returncode"] == 0, "cmd": raw, **res}


_WEATHER_FR = {
    0: "ciel dégagé", 1: "peu nuageux", 2: "partiellement nuageux",
    3: "nuageux", 45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine dense",
    61: "pluie faible", 63: "pluie", 65: "pluie forte",
    71: "neige faible", 73: "neige", 75: "neige forte",
    80: "averses faibles", 81: "averses", 82: "averses fortes",
    95: "orage", 96: "orage avec grêle", 99: "orage violent",
}


def _weather(args: Dict[str, Any]) -> Dict[str, Any]:
    city = str(args.get("city") or CITY_DEFAULT).strip() or CITY_DEFAULT
    try:
        import requests
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "fr"},
            timeout=6).json()
        if not geo.get("results"):
            return {"ok": False, "error": f"ville inconnue : {city}"}
        place = geo["results"][0]
        current = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,weather_code,wind_speed_10m"},
            timeout=6).json()["current"]
        return {"ok": True, "city": place["name"],
                "temp_c": current.get("temperature_2m"),
                "wind_kmh": current.get("wind_speed_10m"),
                "conditions": _WEATHER_FR.get(current.get("weather_code"),
                                              "variable")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _media(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip()
    target = str(args.get("target") or "").strip()
    if target in ("kodi", "retropie"):
        if action not in ("start", "stop"):
            return {"ok": False, "error": f"{action} non supporté pour {target}"}
        res = _run(["sudo", "-n", "systemctl", action, target], timeout=20)
        return {"ok": res["returncode"] == 0, "target": target,
                "action": action, "detail": res["stderr"] or res["stdout"]}
    if target == "tv":
        cmd = "on 0" if action == "on" else "standby 0" if action == "off" else None
        if cmd is None:
            return {"ok": False, "error": "action TV : on ou off"}
        res = _run(["cec-client", "-s", "-d", "1", cmd], timeout=8)
        return {"ok": res["returncode"] == 0, "target": "tv", "action": action}
    if target in ("fip", "nova"):
        url = ("https://icecast.radiofrance.fr/fip-hifi.aac" if target == "fip"
               else "https://novazz.ice.infomaniak.ch/novazz-128.mp3")
        if action == "stop":
            _run(["pkill", "-f", "mpv"], timeout=5)
            return {"ok": True, "target": target, "action": "stop"}
        try:
            subprocess.Popen(["mpv", "--no-video", "--volume=60", url],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return {"ok": True, "target": target, "action": "play"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"cible inconnue : {target}"}


def _self_info(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from agent_self import self_info
        return {"ok": True, "info": self_info(str(args.get("topic") or ""))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_HANDLERS = {
    "robot_action": _robot_action,
    "robot_state": _robot_state,
    "robot_actions": _robot_actions,
    "memory_search": _memory_search,
    "memory_add": _memory_add,
    "memory_forget": _memory_forget,
    "sys_info": _sys_info,
    "sys_services": _sys_services,
    "sys_logs": _sys_logs,
    "service_ctl": _service_ctl,
    "shell": _shell,
    "weather": _weather,
    "media": _media,
    "self_info": _self_info,
}


def tool_names() -> List[str]:
    return list(_HANDLERS)


def execute_tool(name: str, arguments: Any) -> Dict[str, Any]:
    """Exécute un outil et retourne TOUJOURS un dict JSON-sérialisable."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"outil inconnu : {name}",
                "available": tool_names()}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except Exception as exc:
            return {"ok": False, "error": f"arguments JSON invalides : {exc}"}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = handler(arguments)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {k: _truncate(v) for k, v in result.items()}


if __name__ == "__main__":
    print("Outils :", ", ".join(tool_names()))
    for demo in ("robot_state", "sys_info", "memory_search"):
        payload = {"query": "coiffeur"} if demo == "memory_search" else {}
        print(f"\n=== {demo} ===")
        print(json.dumps(execute_tool(demo, payload), ensure_ascii=False,
                         indent=1)[:1200])
