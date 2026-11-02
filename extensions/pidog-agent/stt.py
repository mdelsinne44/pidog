#!/usr/bin/env python3
"""Transcription optionnelle via Groq Whisper.

La veille (« PiDog ») reste assurée par Vosk, léger et local. Pour la phrase
de commande, on peut ré-transcrire l'audio avec Whisper pour une meilleure
précision, puis retomber sur Vosk si le réseau/la clé ne répond pas.

Configuration (.env) :
    STT_BACKEND=auto|vosk|groq     # auto = groq si GROQ_KEY, sinon vosk
    WHISPER_MODEL=whisper-large-v3-turbo
"""

from __future__ import annotations

import io
import os
import wave
from typing import Optional

__all__ = ["backend", "transcribe_groq", "transcribe"]


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


def transcribe_groq(pcm: bytes, rate: int = 8000,
                    language: str = "fr") -> Optional[str]:
    """Transcrit un buffer PCM s16le mono. Retourne None en cas d'échec."""
    key = os.getenv("GROQ_KEY", "")
    if not key or not pcm:
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
        if response.status_code != 200:
            print(f"[STT] Whisper HTTP {response.status_code}: "
                  f"{response.text[:150]}", flush=True)
            return None
        text = (response.json().get("text") or "").strip()
        return text or None
    except Exception as exc:
        print(f"[STT] Whisper: {type(exc).__name__}: {exc}", flush=True)
        return None


def transcribe(pcm: bytes, rate: int, fallback: str = "") -> str:
    """Transcrit avec Whisper si configuré, sinon renvoie le texte Vosk."""
    if backend() == "groq":
        text = transcribe_groq(pcm, rate=rate)
        if text:
            print(f"  [STT Whisper] {text}", flush=True)
            return text
        if fallback:
            print(f"  [STT Vosk fallback] {fallback}", flush=True)
    return fallback
