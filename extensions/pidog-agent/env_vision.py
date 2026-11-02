#!/usr/bin/env python3
"""Vision environnement — CLI + Voice assistant."""
import os, subprocess, requests


def bt_flip4():
    try:
        cards = subprocess.run(["pactl", "list", "short", "cards"],
                               capture_output=True, text=True).stdout
        sinks = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True).stdout
        connected = "bluez" in cards or "04_FE_A1_8F_3C_A2" in cards
        active = any("bluez" in l or "handsfree" in l for l in sinks.splitlines())
        if connected and active:
            return "Flip 4 (JBL) connectée en Bluetooth HFP — audio actif."
        if connected:
            return "Flip 4 (JBL) détectée mais pas active en sink."
        return "Flip 4 (JBL) non détectée — Bluetooth peut-être coupé."
    except Exception as e:
        return f"Bluetooth indisponible : {e}"


def audio_default():
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True).stdout
        for line in sinks.splitlines():
            if "*" in line:
                return f"Sortie audio par défaut : {line.split()[1]}"
        return "Aucune sortie audio détectée."
    except Exception as e:
        return f"Audio non lisible : {e}"


def net_ip():
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=3)
        return f"IP publique : {r.json()['ip']}" if r.status_code == 200 else "IP inconnue"
    except Exception:
        return "IP non disponible (hors réseau ?)"


def dog_status():
    """État réel du daemon partagé (ne crée aucun Pidog())."""
    try:
        import pidog
        state = pidog.service_state()
        if state.get("ready"):
            return "PiDog robot présent et connecté (daemon prêt)."
        if state.get("state") == "starting":
            return "PiDog daemon actif, robot en cours d'initialisation."
        if state.get("state") == "error":
            return f"PiDog robot indisponible ({state.get('error')})."
        return "PiDog robot non détecté (service arrêté)."
    except Exception as exc:
        return f"PiDog service non interrogeable ({exc})."


def env_summary():
    return {
        "bluetooth": bt_flip4(),
        "audio": audio_default(),
        "reseau": net_ip(),
        "robot": dog_status(),
    }
