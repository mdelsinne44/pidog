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
import tempfile
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Bootstrap des chemins : tools.py doit fonctionner seul (python3 tools.py)
# comme importé par le CLI ou le stream.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
for _path in (_REPO, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import memory

MAX_OUTPUT = 4000          # caractères renvoyés au LLM par outil
SHELL_TIMEOUT = int(os.getenv("PIDOG_SHELL_TIMEOUT", "30"))
# read-only = liste blanche historique · workspace = tout sauf le dangereux,
# cwd confiné au dépôt · full = aucune vérification · off = désactivé.
SHELL_MODE = os.getenv("PIDOG_SHELL_MODE", "workspace").strip().lower()
SHELL_WORKDIR = os.path.expanduser(os.getenv("PIDOG_SHELL_CWD", "~/pidog"))
WORKSPACE_ROOTS: List[str] = [
    os.path.realpath(os.path.expanduser(p.strip()))
    for p in os.getenv("PIDOG_WORKSPACE", "~/pidog").split(",")
    if p.strip()
]
USER_AGENT = os.getenv(
    "PIDOG_USER_AGENT",
    "Mozilla/5.0 (X11; Linux armv7l) PiDogAgent/1.0 (+https://github.com/sunfounder/pidog)",
)
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
        "Action physique du PiDog via le daemon (posture et batterie gérées "
        "par le daemon).",
        {
            "action": {"type": "string",
                       "description": "posture, head, head_gesture, "
                                      "turn_head_left, turn_head_right, "
                                      "head_center, look, led, wag, tail, "
                                      "lift_paw, wave_paw, handshake, high_five, "
                                      "bow, groove, walk, turn, trot, stretch, "
                                      "push_up, celebrate, bark, howl, sound, "
                                      "battery, stop, rest"},
            "params": {"type": "object", "additionalProperties": True,
                       "description": "ex. {\"name\":\"sit\"}, "
                                      "{\"direction\":\"forward\",\"steps\":1}"},
        },
        ["action"],
    ),
    _fn("robot_state",
        "Daemon prêt, tension batterie, audio, réseau, Bluetooth.", {}),
    _fn("robot_actions",
        "Catalogue des actions physiques et de leurs paramètres.", {}),
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
        "Uptime, charge, RAM, disque, température, throttling.", {}),
    _fn("sys_services",
        "État des services clés (pidog, stream, monitor, kodi, retropie…).", {}),
    _fn("sys_logs", "Dernières lignes du journal d'une unité systemd.",
        {"unit": {"type": "string"},
         "lines": {"type": "integer", "description": "défaut 30, max 200"}},
        ["unit"]),
    _fn("service_ctl",
        "start/stop/restart/status d'un service whitelist — uniquement sur "
        "demande explicite de Martin.",
        {"action": {"type": "string",
                    "enum": ["start", "stop", "restart", "status"]},
         "unit": {"type": "string"},
         "explicit": {"type": "boolean",
                      "description": "true seulement si Martin l'a demandé"}},
        ["action", "unit"]),
    _fn("shell",
        "Commande shell dans ~/pidog (pipes et && acceptés) : git, ls, grep, "
        "systemctl status, journalctl… Destructif refusé en mode workspace.",
        {"cmd": {"type": "string",
                 "description": "ligne complète, ex. \"git status --short | head\""},
         "cwd": {"type": "string", "description": "défaut ~/pidog"}},
        ["cmd"]),
    _fn("run_python",
        "Écrit puis exécute un script Python (cwd ~/pidog, timeout 30 s). "
        "Imports locaux (pidog, memory, tools) disponibles.",
        {"code": {"type": "string", "description": "code Python complet"},
         "timeout": {"type": "integer", "description": "secondes, 1..120 (défaut 30)"}},
        ["code"]),
    _fn("write_file",
        "Crée ou remplace un fichier texte sous ~/pidog (refusé hors workspace).",
        {"path": {"type": "string",
                  "description": "relatif à ~/pidog ou absolu dessous"},
         "content": {"type": "string"},
         "mode": {"type": "string", "enum": ["overwrite", "append"]}},
        ["path", "content"]),
    _fn("web_search",
        "Recherche Internet (DuckDuckGo) : titres, URLs et extraits. "
        "Enchaîne avec fetch_url.",
        {"query": {"type": "string"},
         "k": {"type": "integer", "description": "défaut 5, max 10"}},
        ["query"]),
    _fn("fetch_url",
        "Texte d'une page http(s) (HTML converti). Météo → weather.",
        {"url": {"type": "string"},
         "max_chars": {"type": "integer", "description": "défaut 6000, max 20000"}},
        ["url"]),
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


# ------------------------------------------------------------------ fichiers --
def _within_workspace(path: str) -> Optional[str]:
    """Chemin absolu réel s'il est dans le workspace, sinon None."""
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except Exception:
        return None
    for root in WORKSPACE_ROOTS:
        if real == root or real.startswith(root + os.sep):
            return real
    return None


def _workspace_cwd(raw: Optional[str]) -> str:
    candidate = os.path.expanduser(raw or SHELL_WORKDIR)
    if not os.path.isabs(candidate):
        candidate = os.path.join(SHELL_WORKDIR, candidate)
    real = _within_workspace(candidate) or os.path.realpath(SHELL_WORKDIR)
    return real if os.path.isdir(real) else os.path.realpath(SHELL_WORKDIR)


# ------------------------------------------------------------------- shell ---
# Commandes refusées en mode « workspace » : destructrices ou système.
_DANGEROUS = re.compile(
    r"""
      \bsudo\b
    | \b(mkfs(\.\w+)?|fdisk|parted|wipefs|mkswap)\b
    | \bdd\s+[^\n]*\bof=/dev/
    | \b(shutdown|reboot|poweroff|halt)\b
    | \bsystemctl\s+(stop|disable|mask|kill|isolate)\b
    | \b(rm)\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\s+(/|~|\$HOME|/home)/?\s*$
    | \bch(mod|own)\s+-R\s+\S+\s+/(\s|$)
    | \bgit\s+push\b[^\n]*--force
    | :\s*\(\s*\)\s*\{
    | \b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b
    | >\s*/dev/(sd|mmcblk|nvme|hd)
    """,
    re.VERBOSE | re.IGNORECASE,
)


# Mode readonly : premier mot + sous-commande éventuellement contrôlés.
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
    if len(raw) > 2000:
        return {"ok": False, "error": "commande trop longue (max 2000 car.)"}

    cwd = _workspace_cwd(args.get("cwd"))

    if SHELL_MODE == "readonly":
        # Mode historique : liste blanche, pas de composition de commandes.
        if _FORBIDDEN.search(raw):
            return {"ok": False,
                    "error": "mode readonly : ; | > < ` $ interdits "
                             "(passe PIDOG_SHELL_MODE=workspace pour les "
                             "pipes et l'écriture dans ~/pidog)"}
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
                    "hint": "PIDOG_SHELL_MODE=workspace pour exécuter du code "
                            "et des commandes dans ~/pidog"}
        cmd: Any = parts
    else:
        if SHELL_MODE != "full" and _DANGEROUS.search(raw):
            return {"ok": False,
                    "error": "commande refusée : opération destructrice ou "
                             "système (sudo, reboot, mkfs, rm -rf /, "
                             "systemctl stop…). PIDOG_SHELL_MODE=full pour "
                             "passer outre."}
        # bash -lc : pipes, &&, redirections et variables fonctionnent.
        cmd = ["/bin/bash", "-lc", raw]

    res = _run(cmd, timeout=SHELL_TIMEOUT, cwd=cwd)
    return {"ok": res["returncode"] == 0, "cmd": raw, "cwd": cwd, **res}


def _run_python(args: Dict[str, Any]) -> Dict[str, Any]:
    if SHELL_MODE == "off":
        return {"ok": False, "error": "exécution désactivée (PIDOG_SHELL_MODE=off)"}
    code = str(args.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "code vide"}
    if len(code) > 20000:
        return {"ok": False, "error": "code trop long (max 20 000 car.)"}
    timeout = args.get("timeout") or SHELL_TIMEOUT
    try:
        timeout = max(1, min(120, int(timeout)))
    except Exception:
        timeout = SHELL_TIMEOUT
    cwd = os.path.realpath(SHELL_WORKDIR)
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", prefix="pidog_run_",
                dir=os.path.expanduser("~"), delete=False,
                encoding="utf-8") as handle:
            handle.write(code)
            script_path = handle.name
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [_REPO, os.path.join(_REPO, "pidog"), _HERE]
            + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
                "timeout_s": timeout}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout {timeout}s",
                "hint": "découpe le code ou augmente timeout"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:
                pass


def _write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    if SHELL_MODE in ("off", "readonly"):
        return {"ok": False,
                "error": "écriture refusée en mode readonly/off "
                         "(PIDOG_SHELL_MODE=workspace)"}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return {"ok": False, "error": "chemin manquant"}
    if not os.path.isabs(raw_path):
        raw_path = os.path.join(SHELL_WORKDIR, raw_path)
    path = _within_workspace(raw_path)
    if path is None:
        return {"ok": False, "error": f"chemin hors workspace : {raw_path}",
                "allowed_roots": WORKSPACE_ROOTS}
    relative = path
    for root in WORKSPACE_ROOTS:
        if path == root or path.startswith(root + os.sep):
            relative = os.path.relpath(path, root)
            break
    parts = set(relative.split(os.sep))
    if ".git" in parts or "models" in parts or "data" in parts \
            or os.path.basename(path) == ".env":
        return {"ok": False,
                "error": "écriture refusée : .env, .git, data/ et models/ sont "
                         "protégés"}
    content = args.get("content")
    if content is None:
        return {"ok": False, "error": "content manquant"}
    content = str(content)
    if len(content) > 200000:
        return {"ok": False, "error": "contenu trop long (max 200 000 car.)"}
    mode = "a" if str(args.get("mode") or "").lower() == "append" else "w"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode, encoding="utf-8") as handle:
            handle.write(content)
        return {"ok": True, "path": path, "mode": "append" if mode == "a" else "overwrite",
                "bytes": len(content.encode("utf-8"))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------- internet ----
class _HTMLText(HTMLParser):
    """Convertit du HTML en texte lisible (sans script/style)."""

    _SKIP = {"script", "style", "noscript", "svg", "template", "nav",
             "aside", "form", "button", "select", "textarea",
             "iframe", "footer"}
    _BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article", "header", "footer", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.main_parts: List[str] = []
        self._skip = 0
        self._main = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
        if tag in ("main", "article"):
            self._main += 1
            if self._main == 1:
                self.main_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")
        if tag in ("main", "article") and self._main:
            self._main -= 1

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._main:
            self.main_parts.append(data)

    def text(self) -> str:
        def clean(chunks: List[str]) -> str:
            raw = "".join(chunks)
            raw = re.sub(r"[ \t\f\v]+", " ", raw)
            raw = re.sub(r"\n\s*\n+", "\n", raw)
            return raw.strip()

        main = clean(self.main_parts)
        # <main>/<article> quand il existe (Wikipedia, blogs, docs) : évite les
        # menus et barres latérales qui polluaient le début du texte.
        return main if len(main) > 200 else clean(self.parts)


def _html_to_text(html_text: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html_text)
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html_text)


def _clean_ddg_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        values = parse_qs(urlparse(href).query).get("uddg")
        if values:
            return values[0]
    return href


def _strip_tags(fragment: str) -> str:
    return _html_to_text(fragment or "")


def _web_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "requête vide"}
    try:
        k = max(1, min(10, int(args.get("k") or 5)))
    except Exception:
        k = 5
    try:
        import requests
    except Exception as exc:
        return {"ok": False, "error": f"requests indisponible : {exc}"}

    endpoints = (
        ("html", "https://html.duckduckgo.com/html/",
         {"q": query, "kl": "fr-fr"}),
        ("lite", "https://lite.duckduckgo.com/lite/", {"q": query}),
    )
    last_error = ""
    for kind, url, data in endpoints:
        try:
            response = requests.post(
                url, data=data,
                headers={"User-Agent": USER_AGENT,
                         "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6"},
                timeout=10,
            )
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code} ({kind})"
                continue
            page = response.text
            results: List[Dict[str, str]] = []
            if kind == "html":
                anchors = re.findall(
                    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    page, re.S)
                snippets = re.findall(
                    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                    page, re.S)
            else:
                anchors = re.findall(
                    r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    page, re.S)
                snippets = re.findall(
                    r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>', page, re.S)
            for index, (href, title) in enumerate(anchors[:k]):
                snippet = _strip_tags(snippets[index]) if index < len(snippets) else ""
                results.append({
                    "title": _strip_tags(title),
                    "url": _clean_ddg_url(href),
                    "snippet": snippet[:200],
                })
            if results:
                return {"ok": True, "query": query, "engine": kind,
                        "results": results}
            last_error = f"aucun résultat parsé ({kind})"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "error": last_error or "recherche impossible",
            "hint": "vérifie la connexion Internet ; sinon utilise fetch_url "
                    "sur une URL connue (fr.wikipedia.org, etc.)"}


def _wikipedia_summary(url: str, max_chars: int) -> Optional[Dict[str, Any]]:
    """Raccourci Wikipédia : l'API REST évite les menus de la page HTML."""
    match = re.match(
        r"^https?://([a-z]{2,3}(?:-[a-z]+)?)\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)",
        url, re.I)
    if not match:
        return None
    lang, title = match.group(1), match.group(2)
    api = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
           f"{title}")
    try:
        import requests
        response = requests.get(api, headers={"User-Agent": USER_AGENT},
                                timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:
        return None
    text = data.get("extract") or ""
    if not text:
        return None
    description = data.get("description")
    if description and description not in text:
        text = f"{description}.\n{text}"
    return {"ok": True, "url": data.get("content_urls", {})
            .get("desktop", {}).get("page", url),
            "source": "wikipedia-api",
            "title": data.get("title", ""),
            "chars": len(text), "truncated": len(text) > max_chars,
            "text": text[:max_chars]}


def _fetch_url(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "URL http(s) uniquement"}
    try:
        max_chars = max(500, min(20000, int(args.get("max_chars") or 6000)))
    except Exception:
        max_chars = 6000
    summary = _wikipedia_summary(url, max_chars)
    if summary is not None:
        return summary
    try:
        import requests
    except Exception as exc:
        return {"ok": False, "error": f"requests indisponible : {exc}"}
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT,
                     "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6"},
            timeout=(8, 25), stream=True, allow_redirects=True,
        )
        if response.status_code != 200:
            return {"ok": False, "error": f"HTTP {response.status_code}",
                    "url": response.url}
        content_type = response.headers.get("content-type", "").lower()
        raw = response.raw.read(2 * 1024 * 1024 + 1, decode_content=True)
        truncated_bytes = len(raw) > 2 * 1024 * 1024
        raw = raw[:2 * 1024 * 1024]
        encoding = response.encoding or "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        if "html" in content_type or "xml" in content_type:
            text = _html_to_text(text)
        elif "json" in content_type:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except Exception:
                pass
        elif "text" not in content_type and not text.lstrip().startswith(("{", "[", "<")):
            return {"ok": False,
                    "error": f"contenu non textuel ({content_type or 'inconnu'})",
                    "url": response.url}
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return {"ok": True, "url": response.url, "content_type": content_type,
                "chars": len(text), "truncated": truncated_bytes
                or len(text) > max_chars,
                "text": text[:max_chars]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}


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
    "run_python": _run_python,
    "write_file": _write_file,
    "web_search": _web_search,
    "fetch_url": _fetch_url,
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
