#!/usr/bin/env python3
"""Transcription optionnelle via Groq Whisper.

La veille (« PiDog ») reste assurée par Vosk, léger et local. Pour la phrase
de commande, on peut ré-transcrire l'audio avec Whisper pour une meilleure
précision, puis retomber sur Vosk si le réseau/la clé ne répond pas.

Quotas : l'API audio de Groq ne partage pas le quota des modèles de chat. Elle
compte des **secondes d'audio** (par heure et par jour) plus un RPM propre.
Quand ça sature, inutile de retenter à chaque phrase : on marque Whisper en
cooldown et Vosk prend le relais immédiatement (même précision moindre, zéro
réseau, zéro quota).

Configuration (.env) :
    STT_BACKEND=auto|vosk|groq     # auto = groq si GROQ_KEY, sinon vosk
    WHISPER_MODEL=whisper-large-v3-turbo
    WHISPER_COOLDOWN=90            # durée du repli Vosk après saturation (s)
"""

from __future__ import annotations

import io
import os
import time
import wave
from typing import Optional

__all__ = ["backend", "transcribe_groq", "transcribe", "describe",
           "whisper_ready"]

_whisper_until = 0.0
_whisper_reason = ""
DEFAULT_COOLDOWN = float(os.getenv("WHISPER_COOLDOWN", "90"))


def _set_cooldown(seconds: float, reason: str) -> None:
    global _whisper_until, _whisper_reason
    _whisper_until = time.time() + max(5.0, seconds)
    _whisper_reason = reason


def whisper_ready() -> bool:
    """Whisper est-il utilisable maintenant (clé + pas en cooldown) ?"""
    return bool(os.getenv("GROQ_KEY")) and time.time() >= _whisper_until


def describe() -> str:
    """Résumé lisible pour la bannière de démarrage."""
    if backend() == "vosk":
        return "Vosk local (Whisper désactivé)"
    if whisper_ready():
        return "Whisper " + os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
    left = int(_whisper_until - time.time()) + 1
    return f"Vosk — Whisper en cooldown {left}s ({_whisper_reason})"


def backend() -> str:
    choice = os.getenv("STT_BACKEND", "auto").strip().lower()
    if choice in ("groq", "whisper"):
        return "groq"
    if choice == "vosk":
        return "vosk"
    return "groq" if os.getenv("GROQ_KEY") else "vosk"


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)       # s16le
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


def _retry_after(response) -> float:
    raw = response.headers.get("retry-after") or ""
    try:
        return float(str(raw).rstrip("s"))
    except Exception:
        return DEFAULT_COOLDOWN


def transcribe_groq(pcm: bytes, rate: int = 8000,
                    language: str = "fr") -> Optional[str]:
    """Transcrit un buffer PCM s16le mono. Retourne None en cas d'échec.

    Un échec arme un cooldown : Whisper ne sera pas retenté à la phrase
    suivante, Vosk répondra directement.
    """
    key = os.getenv("GROQ_KEY", "")
    if not key or not pcm:
        return None
    if not whisper_ready():
        return None
    try:
        import requests
    except Exception:
        return None

    model = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("pidog.wav", _wav_bytes(pcm, rate), "audio/wav")},
            data={
                "model": model,
                "language": language,
                "response_format": "json",
                "temperature": "0",
            },
            timeout=25,
        )
    except Exception as exc:
        print(f"[STT] Whisper: {type(exc).__name__}: {exc}", flush=True)
        _set_cooldown(min(DEFAULT_COOLDOWN, 30.0), "réseau")
        return None

    if response.status_code == 200:
        try:
            text = (response.json().get("text") or "").strip()
        except Exception:
            return None
        return text or None

    body = response.text[:150]
    print(f"[STT] Whisper HTTP {response.status_code}: {body}", flush=True)
    if response.status_code == 429:
        _set_cooldown(_retry_after(response), "quota audio")
    elif response.status_code in (401, 403):
        _set_cooldown(3600.0, "clé refusée")
    elif response.status_code >= 500:
        _set_cooldown(min(DEFAULT_COOLDOWN, 30.0), f"HTTP {response.status_code}")
    return None


def transcribe(pcm: bytes, rate: int, fallback: str = "") -> str:
    """Transcrit avec Whisper si possible, sinon renvoie le texte Vosk."""
    if backend() == "groq" and whisper_ready():
        text = transcribe_groq(pcm, rate=rate)
        if text:
            print(f"  [STT Whisper] {text}", flush=True)
            return text
        if fallback:
            print(f"  [STT Vosk fallback] {fallback}", flush=True)
    elif backend() == "groq" and fallback:
        print(f"  [STT Vosk] Whisper en cooldown ({_whisper_reason}) — {fallback}",
              flush=True)
    return fallback
