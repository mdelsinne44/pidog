# -*- coding: utf-8 -*-
"""Chaîne de fournisseurs LLM pour l'agent PiDog.

Ordre de référence (Martin) : **Groq d'abord**, puis **Gemini**, puis les
**modèles gratuits d'OpenRouter**. On ne bascule que sur incident réel :

    429 (quota TPM/TPD), 401/403 (clé absente, expirée ou révoquée),
    404 (modèle retiré), 400 (paramètre refusé), 5xx, erreur réseau.

Un fournisseur sans clé est simplement sauté (pas d'erreur bloquante) : le CLI
reste utilisable avec la seule clé Groq, et profite des autres dès qu'elles
sont renseignées dans ``extensions/pidog-agent/.env``.

Configuration (toutes optionnelles sauf GROQ_KEY) :

    GROQ_KEY=gsk_...                      # clé Groq (sans expiration !)
    GEMINI_API_KEY=...                    # ou GOOGLE_API_KEY
    OPENROUTER_API_KEY=sk-or-v1-...
    LLM_MODEL_CLI=qwen/qwen3.8-27b        # modèle Groq principal
    LLM_FALLBACKS=openai/gpt-oss-120b,openai/gpt-oss-20b
    LLM_GEMINI_MODELS=gemini-3.5-flash-lite,gemini-3.8-flash
    LLM_ALLAM_MODELS=allam-2-7b
    LLM_OPENROUTER_MODELS=recursive/flint-v1.0:free,cohere/north-mini-code:free
    LLM_PROVIDERS=groq,gemini,allam,openrouter   # ordre des fournisseurs

Le module n'impose rien au reste du code : ``stream()`` et ``complete()``
renvoient les mêmes événements normalisés que l'ancien ``groq_stream``.

Prompt caching : rien à activer côté API (Groq, Gemini et OpenRouter cachent
automatiquement un préfixe identique). Ce qui compte, c'est de garder un
**préfixe stable** : le prompt système statique et les schémas d'outils
doivent être envoyés à l'identique à chaque tour, et tout ce qui varie
(date, tension, mémoire) doit venir *après*. ``cache_stats()`` expose les
compteurs ``cached_tokens`` renvoyés par les trois fournisseurs pour vérifier
que le cache mord vraiment.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

import requests

# ------------------------------------------------------------------ constantes
DEFAULT_GROQ_MODELS = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODELS = "gemini-3.8-flash,gemini-4-argon,gemini-3.5-flash-lite"
# Modèles gratuits d'OpenRouter qui savent appeler des outils ; le routeur
# `openrouter/free` sert de dernier recours (il choisit lui-même un gratuit).
DEFAULT_OPENROUTER_MODELS = (
    "deepseek/deepseek-v4.1-flash-1017",
    "recursive/flint-v1.0:free",
    "cohere/north-mini-code:free",
    "openrouter/free"
)
# ALLaM est servi par Groq mais possède son propre quota (les limites Groq sont
# par modèle). C'est un 7B : filet de sécurité, pas un remplaçant de Qwen.
DEFAULT_ALLAM_MODELS = "allam-2-7b"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Délais avant de renoncer à réessayer le même modèle (seconde).
RETRY_MAX = 2
RETRY_MAX_WAIT = 20.0
# Un modèle en cooldown est sauté pendant ce temps sans retenter (s).
DEFAULT_COOLDOWN = 45.0


@dataclass(frozen=True)
class Target:
    """Un couple (fournisseur, modèle) prêt à être appelé."""

    provider: str
    model: str
    url: str
    keys: tuple
    headers: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def api_key(self) -> Optional[str]:
        for name in self.keys:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return None

    def available(self) -> bool:
        return self.api_key() is not None

    def missing_key_hint(self) -> str:
        return " / ".join(self.keys)


def _env_list(name: str, default: Any = "") -> List[str]:
    """Liste depuis ``ENV`` (séparée par virgules), sinon depuis ``default``.

    ``default`` peut être une chaîne (``"a,b"``) ou une séquence déjà découpée
    (``DEFAULT_OPENROUTER_MODELS``) : on ne suppose pas ``.split()``.
    """
    raw = os.getenv(name)
    items = raw.split(",") if raw else (
        default.split(",") if isinstance(default, str) else list(default))
    return [str(item).strip() for item in items if str(item).strip()]


def _groq_params(model: str) -> Dict[str, Any]:
    """``reasoning_effort`` uniquement sur les gpt-oss (sinon réponse vide)."""
    if str(model).startswith("openai/gpt-oss"):
        return {
            "reasoning_effort": os.getenv("PIDOG_REASONING_EFFORT", "low"),
            "include_reasoning": False,
        }
    return {}


def _openrouter_headers() -> Dict[str, str]:
    """OpenRouter identifie l'application (facultatif mais recommandé)."""
    return {
        "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/pidog"),
        "X-Title": os.getenv("OPENROUTER_TITLE", "PiDog Agent"),
    }


# ------------------------------------------------------------------ providers
def _targets(provider: str, models: Iterable[str]) -> List[Target]:
    provider = provider.lower()
    if provider == "groq":
        return [Target("groq", m, GROQ_URL, ("GROQ_KEY",), params=_groq_params(m))
                for m in models]
    if provider == "gemini":
        return [Target("gemini", m, GEMINI_URL,
                       ("GEMINI_API_KEY", "GOOGLE_API_KEY")) for m in models]
    if provider == "openrouter":
        return [Target("openrouter", m, OPENROUTER_URL, ("OPENROUTER_API_KEY",),
                       headers=_openrouter_headers(), params=_openrouter_params())
                for m in models]
    if provider == "allam":
        return [Target("allam", m, GROQ_URL, ("GROQ_KEY",)) for m in models]
    raise ValueError(f"fournisseur inconnu : {provider}")


def _openrouter_params() -> Dict[str, Any]:
    # OpenRouter ne renvoie `usage` en streaming que si on le demande.
    return {"usage": {"include": True}}


def chain(model: Optional[str] = None,
          providers: Optional[Iterable[str]] = None) -> List[Target]:
    """Ordre d'appel : Groq (référence), Gemini, puis OpenRouter gratuit.

    ``model`` peut forcer un fournisseur avec le préfixe ``gemini/``,
    ``openrouter/`` ou ``groq/`` (ex. ``--model gemini/gemini-3.5-flash-lite``).
    """
    order = [p.lower() for p in (providers or
                                 _env_list("LLM_PROVIDERS",
                                           "groq,gemini,allam,openrouter"))]
    primary = os.getenv("LLM_MODEL_CLI") or os.getenv("LLM_MODEL") or DEFAULT_GROQ_MODELS
    groq_models = [primary, *_env_list("LLM_FALLBACKS", "openai/gpt-oss-20b")]
    gemini_models = _env_list("LLM_GEMINI_MODELS", DEFAULT_GEMINI_MODELS)
    allam_models = _env_list("LLM_ALLAM_MODELS", DEFAULT_ALLAM_MODELS)
    openrouter_models = _env_list("LLM_OPENROUTER_MODELS", DEFAULT_OPENROUTER_MODELS)

    forced: Optional[str] = None
    if model:
        head, _, tail = model.partition("/")
        if head.lower() in ("groq", "gemini", "allam", "openrouter") and tail:
            forced = head.lower()
            model = tail
            if forced == "groq":
                groq_models = [model, *[m for m in groq_models if m != model]]
            elif forced == "gemini":
                gemini_models = [model, *[m for m in gemini_models if m != model]]
            elif forced == "allam":
                allam_models = [model, *[m for m in allam_models if m != model]]
            else:
                openrouter_models = [model, *[m for m in openrouter_models if m != model]]
        elif _env_list("LLM_ALLOW_MODEL_OVERRIDE", "1") not in ("", "0", "false"):
            # Rétro-compatibilité : un id sans préfixe reste un modèle Groq.
            groq_models = [model, *[m for m in groq_models if m != model]]

    buckets = {
        "groq": groq_models,
        "gemini": gemini_models,
        "allam": allam_models,
        "openrouter": openrouter_models,
    }
    if forced in order:
        order = [forced, *[p for p in order if p != forced]]
    out: List[Target] = []
    for provider in order:
        out.extend(t for t in _targets(provider, buckets.get(provider, []))
                   if t not in out)
    return out


# ------------------------------------------------------------------- quotas --
_cooldown: Dict[str, float] = {}
_dead_providers: Dict[str, str] = {}   # provider -> raison (clé invalide)


def _cooling(target: Target) -> float:
    """Secondes restantes de cooldown (0 si disponible)."""
    left = _cooldown.get(target.label, 0.0) - time.time()
    return max(0.0, left)


def _set_cooldown(target: Target, seconds: float) -> None:
    _cooldown[target.label] = time.time() + max(1.0, seconds)


def reset_quotas() -> None:
    _cooldown.clear()
    _dead_providers.clear()


# -------------------------------------------------------------------- stats --
STATS: Dict[str, Any] = {
    "calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "by_target": {},
    "fallbacks": 0,
}


def _record_usage(target: Target, usage: Dict[str, Any]) -> None:
    if not isinstance(usage, dict) or not usage:
        return
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    STATS["calls"] += 1
    STATS["prompt_tokens"] += prompt
    STATS["completion_tokens"] += completion
    STATS["cached_tokens"] += cached
    entry = STATS["by_target"].setdefault(target.label, {"calls": 0, "prompt": 0,
                                                         "cached": 0})
    entry["calls"] += 1
    entry["prompt"] += prompt
    entry["cached"] += cached


def cache_stats() -> Dict[str, Any]:
    """Résumé lisible des tokens et du cache (pour ``/stats``)."""
    prompt = STATS["prompt_tokens"]
    return {
        "calls": STATS["calls"],
        "prompt_tokens": prompt,
        "completion_tokens": STATS["completion_tokens"],
        "cached_tokens": STATS["cached_tokens"],
        "cache_hit_pct": round(100.0 * STATS["cached_tokens"] / prompt, 1)
        if prompt else 0.0,
        "fallbacks": STATS["fallbacks"],
        "by_target": dict(STATS["by_target"]),
    }


# ------------------------------------------------------------------ requêtes --
def _quota_wait(response) -> float:
    """Délai conseillé par le fournisseur sur un 429."""
    wait = 20.0
    if response is None:
        return wait
    for header in ("retry-after", "x-ratelimit-reset-tokens",
                   "x-ratelimit-reset-requests"):
        raw = response.headers.get(header)
        if not raw:
            continue
        try:
            wait = max(1.0, float(str(raw).rstrip("s")))
            break
        except Exception:
            pass
    try:
        message = str(response.json().get("error", {}).get("message", ""))
    except Exception:
        message = response.text[:300]
    import re
    match = re.search(r"try again in ([\d.]+)\s*(ms|s|m)", message, re.I)
    if match:
        value, unit = float(match.group(1)), match.group(2).lower()
        wait = (value / 1000 if unit == "ms"
                else value * 60 if unit == "m" else value)
    return max(1.0, min(120.0, wait))


def _post(target: Target, payload: Dict[str, Any], stream: bool):
    key = target.api_key()
    if not key:
        raise RuntimeError(f"{target.missing_key_hint()} absente")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}",
               **target.headers}
    return requests.post(target.url, headers=headers, json=payload,
                         stream=stream, timeout=(10, 120))


def _payload(target: Target, messages, max_tokens, temperature, tools,
             stream: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": target.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if target.params:
        payload.update(target.params)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _chain_with_keys(targets: List[Target]) -> Iterator[Target]:
    for target in targets:
        if target.provider in _dead_providers:
            continue
        if not target.available():
            continue
        yield target


def unavailable_report(targets: List[Target]) -> str:
    """Message d'aide quand aucune clé n'est disponible."""
    missing: Dict[str, str] = {}
    for target in targets:
        if not target.available() and target.provider not in missing:
            missing[target.provider] = target.missing_key_hint()
    if not missing:
        return "aucun fournisseur disponible"
    return " ; ".join(f"{p} → {k}" for p, k in missing.items())


# -------------------------------------------------------------------- stream --
def _safe_text(response, limit: int = 300) -> str:
    try:
        return (response.text or "")[:limit]
    except Exception:
        return ""


def _safe_close(response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _attempt(target: Target, messages, max_tokens: float, temperature: float,
             tools):
    """Essaie un modèle, avec les relances 429 « courtes ».

    Retourne ``(response, erreur, evenements)`` : ``response`` vaut ``None``
    en cas d'échec, ``evenements`` contient les messages d'information à
    remonter au shell (relance, cooldown…).
    """
    events: List[str] = []
    attempts = 0
    while True:
        payload = _payload(target, messages, max_tokens, temperature, tools,
                           stream=True)
        try:
            response = _post(target, payload, stream=True)
        except Exception as exc:      # réseau, DNS, TLS…
            return None, f"{target.label} : réseau ({type(exc).__name__}: {exc})", events

        if response.status_code == 200:
            return response, None, events

        code = response.status_code
        body = _safe_text(response)
        wait = _quota_wait(response) if code == 429 else 0.0
        _safe_close(response)
        verdict = _classify_code(code)

        if verdict == "quota":
            if wait <= RETRY_MAX_WAIT and attempts < RETRY_MAX:
                attempts += 1
                events.append(f"quota TPM sur {target.label} — nouvelle tentative "
                              f"dans {int(wait)} s ({attempts}/{RETRY_MAX})")
                time.sleep(wait)
                continue
            _set_cooldown(target, wait if wait > RETRY_MAX_WAIT else DEFAULT_COOLDOWN)
            events.append(f"{target.label} : quota atteint (cooldown {int(wait)} s)")
            return None, f"{target.label} : quota atteint", events
        if verdict == "auth":
            _dead_providers[target.provider] = body or "clé refusée"
            events.append(f"{target.label} : clé refusée par {target.provider} — "
                          f"régénère {target.missing_key_hint()}")
            return None, f"{target.label} : clé refusée", events
        if verdict == "model":
            events.append(f"{target.label} indisponible (HTTP {code})")
            return None, f"{target.label} : HTTP {code} {body}", events
        if verdict == "server":
            events.append(f"{target.label} en erreur (HTTP {code})")
            return None, f"{target.label} : HTTP {code} {body}", events
        return None, f"{target.label} : HTTP {code} {body}", events


def _classify_code(code: int) -> str:
    if code == 429:
        return "quota"
    if code in (401, 403):
        return "auth"
    if code in (400, 404, 422):
        return "model"
    if code >= 500:
        return "server"
    return "fatal"


def stream(messages, model: Optional[str] = None, tools=None,
           max_tokens: int = 800, temperature: float = 0.25) -> Iterator[tuple]:
    """Générateur d'événements normalisés, avec bascule automatique.

    Yields ``("text", str)``, ``("tool_calls", [...])``, ``("finish", str)``,
    ``("info", str)``, ``("usage", dict)`` ou ``("error", str)``.
    """
    targets = chain(model)
    usable = list(_chain_with_keys(targets))
    if not usable:
        yield ("error", "aucune clé LLM disponible — " + unavailable_report(targets))
        return

    last_error = ""
    attempted = 0
    for target in usable:
        cooling = _cooling(target)
        if cooling:
            yield ("info", f"{target.label} en cooldown ({int(cooling) + 1} s)")
            continue
        attempted += 1
        if attempted > 1:
            STATS["fallbacks"] += 1
            yield ("info", f"bascule → {target.label}")
        response, error, events = _attempt(target, messages, max_tokens,
                                           temperature, tools)
        for message in events:
            yield ("info", message)
        if response is not None:
            yield from _stream_body(response, target)
            return
        last_error = error or last_error

    yield ("error", last_error or "tous les fournisseurs ont échoué")


def _stream_body(response, target: Target) -> Iterator[tuple]:
    calls: Dict[int, Dict[str, str]] = {}
    finish_reason = None
    try:
        for raw in response.iter_lines():
            if not raw or not raw.startswith(b"data: "):
                continue
            chunk_raw = raw[6:]
            if chunk_raw == b"[DONE]":
                break
            try:
                chunk = json.loads(chunk_raw)
            except Exception:
                continue
            usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
            if usage:
                _record_usage(target, usage)
                yield ("usage", usage)
            try:
                choice = chunk["choices"][0]
            except (KeyError, IndexError):
                continue
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            if delta.get("content"):
                yield ("text", delta["content"])
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call.get("index", 0),
                                        {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["name"] += function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]
    except Exception as exc:
        yield ("error", f"flux interrompu : {type(exc).__name__}: {exc}")
        return
    finally:
        try:
            response.close()
        except Exception:
            pass

    if calls:
        yield ("tool_calls", [calls[i] for i in sorted(calls)])
    yield ("finish", finish_reason)


# ---------------------------------------------------------------- non-stream --
def complete(messages, model: Optional[str] = None, tools=None,
             max_tokens: int = 800, temperature: float = 0.25):
    """Appel non streamé. Retourne ``(data, erreur)``."""
    targets = chain(model)
    usable = list(_chain_with_keys(targets))
    if not usable:
        return None, "aucune clé LLM disponible — " + unavailable_report(targets)

    last_error = ""
    for target in usable:
        if _cooling(target):
            continue
        payload = _payload(target, messages, max_tokens, temperature, tools,
                           stream=False)
        try:
            response = _post(target, payload, stream=False)
        except Exception as exc:
            last_error = f"{target.label} : réseau ({type(exc).__name__}: {exc})"
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception as exc:
                return None, f"réponse illisible : {exc}"
            _record_usage(target, data.get("usage") or {})
            return data, None
        verdict = _classify_code(response.status_code)
        body = _safe_text(response)
        if verdict == "quota":
            wait = _quota_wait(response)
            _set_cooldown(target, wait)
            last_error = f"{target.label} : quota atteint"
        elif verdict == "auth":
            _dead_providers[target.provider] = body
            last_error = (f"{target.label} : clé invalide ou expirée — "
                          f"régénère {target.missing_key_hint()}")
        else:
            last_error = f"{target.label} : HTTP {response.status_code} {body}"
    return None, last_error or "tous les fournisseurs ont échoué"
