#!/usr/bin/env python3
"""Routage local des commandes robot simples — sans appel LLM.

Les demandes évidentes (« assis », « led rouge », « lève la patte gauche »,
« tourne la tête à droite ») ne doivent PAS dépendre de Groq ni de son quota.
Elles sont traduites ici en appels `dog.action(...)` puis exécutées par le
daemon. Cela évite aussi les cas « le LLM a répondu du texte mais n'a rien
fait » quand le quota est atteint.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

__all__ = ["infer_actions", "actions_to_code", "describe_actions"]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


COLOR_WORDS = {
    "rouge": "red", "red": "red",
    "vert": "green", "verte": "green", "green": "green",
    "bleu": "blue", "bleue": "blue", "blue": "blue",
    "jaune": "yellow", "yellow": "yellow",
    "blanc": "white", "blanche": "white", "white": "white",
    "cyan": "cyan", "magenta": "magenta",
    "rose": "pink", "pink": "pink",
    "orange": "orange",
    "violet": "purple", "violette": "purple", "purple": "purple",
    "noir": "black", "noire": "black", "black": "black",
}

STYLE_WORDS = {
    "respire": "breath", "breath": "breath",
    "boom": "boom", "explose": "boom",
    "aboie": "bark", "bark": "bark",
    "parle": "speak", "speak": "speak",
    "ecoute": "listen", "listen": "listen",
}


def _first_color(text: str) -> Optional[str]:
    for word, color in COLOR_WORDS.items():
        if _has(text, word):
            return color
    return None


def _first_style(text: str) -> str:
    for word, style in STYLE_WORDS.items():
        if _has(text, word):
            return style
    return "breath"


def _led(text: str) -> Optional[List[Dict[str, Any]]]:
    if not _has(text, "led", "leds", "guirlande", "guirlandes", "lumiere",
                "lumieres", "light", "neon", "eclaire", "illumine"):
        return None
    if _has(text, "etein", "eteins", "eteindre", "coupe", "off", "noir", "black"):
        return [{"action": "led", "params": {"off": True}}]
    if _has(text, "multicolore", "multicolor", "arc en ciel", "rainbow",
            "toutes les couleurs", "fete", "noel"):
        color = "rainbow"
    else:
        color = _first_color(text) or "rainbow"
    return [{"action": "led", "params": {
        "color": color, "style": _first_style(text), "brightness": 0.7,
    }}]


def _posture(text: str) -> Optional[List[Dict[str, Any]]]:
    if _has(text, "assis", "assise", "assieds", "assois", "assoir", "sit"):
        return [{"action": "posture", "params": {"name": "sit"}}]
    if _has(text, "debout", "leve toi", "leve-toi", "stand"):
        return [{"action": "posture", "params": {"name": "stand"}}]
    if _has(text, "couche", "couche toi", "couche-toi", "lie", "dors", "dormir"):
        return [{"action": "posture", "params": {"name": "lie"}}]
    return None


def _head(text: str) -> Optional[List[Dict[str, Any]]]:
    if not _has(text, "tete", "head", "regarde", "hoche", "secoue", "penche"):
        return None
    if _has(text, "hoche", "nod"):
        return [{"action": "head_gesture", "params": {"name": "nod"}}]
    if _has(text, "secoue", "shake"):
        return [{"action": "head_gesture", "params": {"name": "shake"}}]
    if _has(text, "penche", "tilt"):
        side = "tilt_left" if _has(text, "gauche", "left") else "tilt_right"
        return [{"action": "head_gesture", "params": {"name": side}}]
    if _has(text, "centre", "center", "axe", "milieu", "droit devant"):
        return [{"action": "head_gesture", "params": {"name": "center"}}]
    if _has(text, "baisse", "bas", "down", "sol", "par terre"):
        return [{"action": "head_gesture", "params": {"name": "look_down"}}]
    if _has(text, "ciel", "haut", "up", "leve"):
        return [{"action": "head_gesture", "params": {"name": "look_up"}}]
    if _has(text, "gauche", "left"):
        return [{"action": "head_gesture", "params": {"name": "look_left"}}]
    if _has(text, "droite", "right"):
        return [{"action": "head_gesture", "params": {"name": "look_right"}}]
    return [{"action": "head_gesture", "params": {"name": "center"}}]


def _paw(text: str) -> Optional[List[Dict[str, Any]]]:
    if not _has(text, "patte", "papatte", "paw", "main"):
        return None
    if _has(text, "serre", "handshake", "poignee"):
        return [{"action": "handshake", "params": {}}]
    if _has(text, "high five", "high5", "tape"):
        return [{"action": "high_five", "params": {}}]
    leg = "front_left" if _has(text, "gauche", "left") else "front_right"
    if _has(text, "salue", "wave", "coucou", "agite"):
        return [{"action": "wave_paw", "params": {"leg": leg}}]
    return [{"action": "lift_paw", "params": {"leg": leg}}]


def _tail(text: str) -> Optional[List[Dict[str, Any]]]:
    if _has(text, "queue", "tail", "wag", "remue"):
        return [{"action": "wag", "params": {"times": 4, "amplitude": 30}}]
    return None


def _sound(text: str) -> Optional[List[Dict[str, Any]]]:
    if _has(text, "aboie", "aboyer", "bark"):
        return [{"action": "bark", "params": {}}]
    if _has(text, "hurle", "howl"):
        return [{"action": "howl", "params": {}}]
    if _has(text, "grogne", "growl"):
        return [{"action": "sound", "params": {"name": "growl"}}]
    return None


def _locomotion(text: str) -> Optional[List[Dict[str, Any]]]:
    if _has(text, "avance", "marche", "forward", "walk"):
        return [{"action": "walk", "params": {"direction": "forward", "steps": 1}}]
    if _has(text, "recule", "backward", "arriere"):
        return [{"action": "walk", "params": {"direction": "backward", "steps": 1}}]
    if _has(text, "tourne", "turn"):
        direction = "left" if _has(text, "gauche", "left") else "right"
        return [{"action": "turn", "params": {"direction": direction, "steps": 1}}]
    if _has(text, "etire", "stretch"):
        return [{"action": "stretch", "params": {}}]
    if _has(text, "pompe", "pushup"):
        return [{"action": "push_up", "params": {}}]
    if _has(text, "celebre", "bravo"):
        return [{"action": "celebrate", "params": {}}]
    if _has(text, "batterie", "tension", "battery"):
        return [{"action": "battery", "params": {}}]
    if _has(text, "stop", "arrete", "immobile"):
        return [{"action": "stop", "params": {}}]
    return None


def _context_switch(text: str, context: Optional[Dict[str, Any]]):
    """« et la droite ? » après « lève la patte gauche » → patte droite."""
    if not context or not _has(text, "droite", "right", "gauche", "left"):
        return None
    if len(text.split()) > 4 and not _has(text, "et", "autre", "maintenant"):
        return None
    action = context.get("action")
    params = dict(context.get("params") or {})
    right = _has(text, "droite", "right")
    if action in ("lift_paw", "wave_paw"):
        params["leg"] = "front_right" if right else "front_left"
        return [{"action": action, "params": params}]
    if action == "head_gesture":
        params["name"] = "look_right" if right else "look_left"
        return [{"action": "head_gesture", "params": params}]
    return None


def infer_actions(text: str, context: Optional[Dict[str, Any]] = None):
    """Retourne une liste d'actions locales, ou None si le LLM doit décider."""
    raw = _norm(text)
    if not raw:
        return None
    switched = _context_switch(raw, context)
    if switched:
        return switched
    for handler in (_posture, _led, _paw, _tail, _sound, _head, _locomotion):
        actions = handler(raw)
        if actions:
            return actions
    return None


def actions_to_code(actions) -> str:
    import json

    def literal(value):
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        return repr(value)

    lines = []
    for item in actions or []:
        name = json.dumps(item["action"])
        params = item.get("params") or {}
        args = ", ".join(f"{k}={literal(v)}" for k, v in params.items())
        lines.append(f"dog.action({name}{', ' + args if args else ''})")
    return "\n".join(lines)


def describe_actions(actions) -> str:
    labels = []
    for item in actions or []:
        action = item.get("action")
        params = item.get("params") or {}
        if action == "posture":
            labels.append({"sit": "assis", "stand": "debout", "lie": "couché"}
                          .get(params.get("name"), params.get("name", "posture")))
        elif action == "led":
            labels.append("LED éteintes" if params.get("off") else
                          f"LED {params.get('color', 'allumées')}")
        elif action == "head_gesture":
            labels.append("tête " + str(params.get("name", "")).replace("look_", ""))
        elif action in ("lift_paw", "wave_paw"):
            side = "gauche" if params.get("leg") == "front_left" else "droite"
            labels.append(f"{'salut' if action == 'wave_paw' else 'patte'} {side}")
        else:
            labels.append(str(action))
    return ", ".join(labels) if labels else "action"


