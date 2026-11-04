# -*- coding: utf-8 -*-
"""Chaîne de fournisseurs LLM pour l'agent PiDog.

La chaîne n'est pas unique : elle dépend de la **tâche** (``task=``), parce que
les contraintes s'opposent.

    agent   outils fiables, contexte moyen   Groq → Gemini → OpenRouter → ALLaM
    voice   latence < 2 s, réponse courte    Groq 20b → ALLaM → Gemini → OpenRouter
    cheap   mémoire, titres, résumés         ALLaM → Gemini → Groq → OpenRouter
    code    gros contexte                    Gemini → Groq → OpenRouter → ALLaM

On ne bascule que sur incident réel :

    429 (quota TPM ou TPD), 401 (clé refusée), 403 (politique, transitoire),
    404 (modèle retiré), 400 (paramètre refusé), 5xx, erreur réseau.

Chaque incident a son traitement : une clé refusée écarte le fournisseur, un
modèle retiré l'écarte 6 h, un paramètre refusé se retente une fois sans les
paramètres exotiques, un quota journalier pose un cooldown long (inutile de
re-sonder toutes les 2 min), un quota minute se retente.

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
    LLM_PROVIDERS=groq,gemini,allam,openrouter   # force l'ordre (sinon : tâche)
    LLM_NO_TOOLS=allam                    # modèles à ne pas utiliser avec tools
    PIDOG_LLM_TASK=agent                  # tâche par défaut

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
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

# ------------------------------------------------------------------ constantes
DEFAULT_GROQ_MODELS = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODELS = "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-4-argon"
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
# Modèle retiré (404) / paramètre refusé (400) : combien de temps l'écarter.
DEAD_MODEL_TTL = 6 * 3600.0
BAD_REQUEST_TTL = 30 * 60.0
# 403 = politique / accès transitoire, pas une clé morte.
POLICY_COOLDOWN = 900.0

# ---------------------------------------------------------------- tâches -----
# Une seule chaîne pour trois usages optimise le mauvais critère : l'agent a
# besoin d'outils, la voix de latence, le code de contexte. On route.
TASK_ORDER: Dict[str, Tuple[str, ...]] = {
    "agent": ("groq", "gemini", "openrouter", "allam"),
    "voice": ("groq", "allam", "gemini", "openrouter"),
    "cheap": ("allam", "gemini", "groq", "openrouter"),
    "code": ("gemini", "groq", "openrouter", "allam"),
}
DEFAULT_TASK = os.getenv("PIDOG_LLM_TASK", "agent").strip().lower() or "agent"
# En voix, le 20b répond plus vite ; la réponse tient de toute façon en 180 s.
TASK_GROQ_HEAD = {"voice": "openai/gpt-oss-20b"}
TASK_MAX_TOKENS = {"voice": 220, "cheap": 400}
# Échéance murale : au-delà, mieux vaut une réponse dégradée qu'un silence.
TASK_DEADLINE = {"agent": 90.0, "voice": 6.0, "cheap": 25.0, "code": 120.0}
# (connexion, lecture) par fournisseur : un Groq qui pend ne doit pas coûter
# 120 s, et OpenRouter gratuit met plus longtemps à démarrer.
TASK_TIMEOUT: Dict[str, Tuple[float, float]] = {
    "groq": (5.0, 25.0),
    "gemini": (5.0, 30.0),
    "openrouter": (5.0, 45.0),
    "allam": (5.0, 20.0),
}


@dataclass(frozen=True)
class Target:
    """Un couple (fournisseur, modèle) prêt à être appelé."""

    provider: str
    model: str
    url: str
    keys: tuple
    headers: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    supports_tools: bool = True
    timeout: Tuple[float, float] = (5.0, 45.0)

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


# Modèles réputés incapables de function calling : les proposer à l'agent
# donnerait une réponse en prose au lieu d'une action. Surchargeable.
_TOOLLESS = {p.lower() for p in _env_list("LLM_NO_TOOLS", "allam")}
# Appris à l'exécution : un 400 « tools » sur une cible la range ici.
_no_tool_labels: set = set()


def _supports_tools(provider: str, model: str) -> bool:
    return (provider.lower() not in _TOOLLESS
            and f"{provider}:{model}" not in _no_tool_labels)


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
    timeout = TASK_TIMEOUT.get(provider, (5.0, 45.0))
    if provider == "groq":
        return [Target("groq", m, GROQ_URL, ("GROQ_KEY",), params=_groq_params(m),
                       supports_tools=_supports_tools("groq", m), timeout=timeout)
                for m in models]
    if provider == "gemini":
        return [Target("gemini", m, GEMINI_URL,
                       ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                       supports_tools=_supports_tools("gemini", m), timeout=timeout)
                for m in models]
    if provider == "openrouter":
        return [Target("openrouter", m, OPENROUTER_URL, ("OPENROUTER_API_KEY",),
                       headers=_openrouter_headers(), params=_openrouter_params(),
                       supports_tools=_supports_tools("openrouter", m),
                       timeout=timeout)
                for m in models]
    if provider == "allam":
        return [Target("allam", m, GROQ_URL, ("GROQ_KEY",),
                       supports_tools=_supports_tools("allam", m), timeout=timeout)
                for m in models]
    raise ValueError(f"fournisseur inconnu : {provider}")


def _openrouter_params() -> Dict[str, Any]:
    # OpenRouter ne renvoie `usage` en streaming que si on le demande.
    return {"usage": {"include": True}}


def chain(model: Optional[str] = None,
          providers: Optional[Iterable[str]] = None,
          task: Optional[str] = None) -> List[Target]:
    """Ordre d'appel pour une tâche (``agent``, ``voice``, ``cheap``, ``code``).

    ``model`` peut forcer un fournisseur avec le préfixe ``gemini/``,
    ``openrouter/``, ``allam/`` ou ``groq/``
    (ex. ``--model gemini/gemini-3.5-flash-lite``).

    Priorité de l'ordre : argument ``providers`` > ``LLM_PROVIDERS`` (s'il est
    renseigné) > ordre de la tâche. Une tâche inconnue retombe sur ``agent``.
    """
    task = (task or DEFAULT_TASK).lower()
    if task not in TASK_ORDER:
        task = "agent"
    env_order = os.getenv("LLM_PROVIDERS", "").strip()
    if providers:
        order = [p.lower() for p in providers]
    elif env_order:
        order = _env_list("LLM_PROVIDERS", "")
    else:
        order = list(TASK_ORDER[task])

    primary = os.getenv("LLM_MODEL_CLI") or os.getenv("LLM_MODEL") or DEFAULT_GROQ_MODELS
    groq_models = [primary, *_env_list("LLM_FALLBACKS", "openai/gpt-oss-20b")]
    # En voix, le petit modèle passe devant : même qualité perçue sur une
    # phrase, latence nettement meilleure, et 180 tokens de sortie suffisent.
    head = TASK_GROQ_HEAD.get(task)
    if head and not model:
        groq_models = [head, *[m for m in groq_models if m != head]]
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
_dead_targets: Dict[str, float] = {}   # label -> expiration (modèle retiré)
_dead_reason: Dict[str, str] = {}


def _cooling(target: Target) -> float:
    """Secondes restantes de cooldown (0 si disponible)."""
    left = _cooldown.get(target.label, 0.0) - time.time()
    return max(0.0, left)


def _set_cooldown(target: Target, seconds: float) -> None:
    """Pose un cooldown, sans jamais raccourcir celui déjà en place."""
    until = time.time() + max(1.0, seconds)
    if until > _cooldown.get(target.label, 0.0):
        _cooldown[target.label] = until


def _mark_dead(target: Target, ttl: float, reason: str) -> None:
    """Écarte une cible précise : inutile de la re-sonder à chaque tour."""
    _dead_targets[target.label] = time.time() + ttl
    _dead_reason[target.label] = reason


def _is_dead(target: Target) -> bool:
    until = _dead_targets.get(target.label)
    if until is None:
        return False
    if until > time.time():
        return True
    _dead_targets.pop(target.label, None)
    _dead_reason.pop(target.label, None)
    return False


def dead_report() -> Dict[str, str]:
    """Cibles écartées et raison (pour ``/stats``)."""
    now = time.time()
    return {label: _dead_reason.get(label, "?")
            for label, until in _dead_targets.items() if until > now}


def reset_quotas() -> None:
    _cooldown.clear()
    _dead_providers.clear()
    _dead_targets.clear()
    _dead_reason.clear()
    _no_tool_labels.clear()


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
# Marqueurs d'un quota **journalier** dans le message d'erreur du fournisseur :
# Groq dit « requests per day (RPD) » / « tokens per day (TPD) ».
_DAY_HINTS = ("per day", "tokens per day", "requests per day", "tpd", "rpd",
              "daily", "par jour")


def _quota_wait(response) -> Tuple[float, str]:
    """Délai conseillé sur un 429, et sa portée ``"minute"`` ou ``"day"``.

    Un quota journalier épuisé ne doit pas être re-sondé toutes les deux
    minutes : on renvoie une attente longue et l'appelant pose un cooldown.
    """
    wait = 20.0
    if response is None:
        return wait, "minute"
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
    match = re.search(r"try again in ([\d.]+)\s*(ms|s|m|h)", message, re.I)
    if match:
        value, unit = float(match.group(1)), match.group(2).lower()
        wait = {"ms": value / 1000.0, "s": value,
                "m": value * 60.0, "h": value * 3600.0}[unit]
    lowered = message.lower()
    if any(hint in lowered for hint in _DAY_HINTS) or wait >= 1800:
        return max(60.0, min(24 * 3600.0, wait)), "day"
    return max(1.0, min(120.0, wait)), "minute"


def _post(target: Target, payload: Dict[str, Any], stream: bool,
          timeout: Optional[Tuple[float, float]] = None):
    key = target.api_key()
    if not key:
        raise RuntimeError(f"{target.missing_key_hint()} absente")
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}",
               **target.headers}
    return requests.post(target.url, headers=headers, json=payload,
                         stream=stream, timeout=timeout or target.timeout)


def _payload(target: Target, messages, max_tokens, temperature, tools,
             stream: bool, include_params: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": target.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    # `include_params=False` sert à la relance « nue » après un 400 : certains
    # modèles refusent reasoning_effort / usage alors que le reste est valide.
    if include_params and target.params:
        payload.update(target.params)
    if tools:
        payload["tools"] = tools
        if include_params:
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


def _timeouts(target: Target,
              deadline: Optional[float]) -> Optional[Tuple[float, float]]:
    """Timeouts (connexion, lecture) bornés par l'échéance de la tâche."""
    connect, read = target.timeout
    if deadline is None:
        return connect, read
    left = deadline - time.monotonic()
    if left <= 0.5:
        return None
    return (max(0.5, min(connect, left)), max(1.0, min(read, left)))


def _attempt(target: Target, messages, max_tokens: float, temperature: float,
             tools, deadline: Optional[float] = None):
    """Essaie un modèle, avec les relances adaptées à chaque incident.

    * 429 « minute » : on retente le même modèle (le quota se recharge).
    * 429 « jour » : cooldown long, inutile d'insister.
    * 400 : relance sans les paramètres exotiques, puis sans les outils.
    * 404 : le modèle est écarté 6 h (plus de round-trip perdu à chaque tour).
    * 5xx : une relance avec jitter avant de basculer.
    * 403 : pause temporaire, la clé n'est pas morte (401 seul l'est).

    Retourne ``(response, erreur, evenements)``.
    """
    events: List[str] = []
    attempts = 0           # relances 429 « minute »
    server_retries = 0     # relance 5xx
    stripped = False       # 400 : sans params exotiques
    tools_dropped = False  # 400 : sans outils

    while True:
        timeout = _timeouts(target, deadline)
        if timeout is None:
            return None, f"{target.label} : échéance de la tâche dépassée", events
        payload = _payload(target, messages, max_tokens, temperature, tools,
                           stream=True, include_params=not stripped)
        try:
            response = _post(target, payload, stream=True, timeout=timeout)
        except Exception as exc:      # réseau, DNS, TLS…
            return None, f"{target.label} : réseau ({type(exc).__name__}: {exc})", events

        code = response.status_code
        if code == 200:
            return response, None, events

        body = _safe_text(response)
        wait, scope = _quota_wait(response) if code == 429 else (0.0, "minute")
        _safe_close(response)
        verdict = _classify_code(code)

        if verdict == "quota":
            if scope == "minute" and wait <= RETRY_MAX_WAIT and attempts < RETRY_MAX:
                attempts += 1
                events.append(f"quota TPM sur {target.label} — nouvelle tentative "
                              f"dans {int(wait)} s ({attempts}/{RETRY_MAX})")
                time.sleep(wait)
                continue
            cooldown = wait if scope == "day" else max(wait, DEFAULT_COOLDOWN)
            _set_cooldown(target, cooldown)
            kind = "quota journalier" if scope == "day" else "quota atteint"
            events.append(f"{target.label} : {kind} (cooldown {int(cooldown)} s)")
            return None, f"{target.label} : {kind}", events

        if verdict == "auth":
            _dead_providers[target.provider] = body or "clé refusée"
            events.append(f"{target.label} : clé refusée par {target.provider} — "
                          f"régénère {target.missing_key_hint()}")
            return None, f"{target.label} : clé refusée", events

        if verdict == "policy":
            # 403 : politique d'accès ou région, souvent transitoire. On ne
            # condamne pas le fournisseur pour toute la session.
            _set_cooldown(target, POLICY_COOLDOWN)
            events.append(f"{target.label} : accès refusé (HTTP {code}) — "
                          f"pause {int(POLICY_COOLDOWN)} s")
            return None, f"{target.label} : HTTP {code} {body}", events

        if verdict == "model":
            if code == 400 and not stripped:
                stripped = True
                events.append(f"{target.label} : paramètre refusé — "
                              f"relance sans options")
                continue
            if code == 400 and tools and not tools_dropped:
                tools_dropped = True
                tools = None
                _no_tool_labels.add(target.label)
                events.append(f"{target.label} ne gère pas les outils — "
                              f"réponse sans action possible")
                continue
            ttl = DEAD_MODEL_TTL if code == 404 else BAD_REQUEST_TTL
            _mark_dead(target, ttl, f"HTTP {code}")
            events.append(f"{target.label} écarté {int(ttl // 60)} min "
                          f"(HTTP {code})")
            return None, f"{target.label} : HTTP {code} {body}", events

        if verdict == "server" and server_retries < 1:
            server_retries += 1
            pause = 0.4 + random.random() * 0.4
            events.append(f"{target.label} : HTTP {code} — relance dans "
                          f"{pause:.1f} s")
            time.sleep(pause)
            continue

        return None, f"{target.label} : HTTP {code} {body}", events


def _classify_code(code: int) -> str:
    if code == 429:
        return "quota"
    if code == 401:
        return "auth"          # clé invalide : le fournisseur est mort
    if code == 403:
        return "policy"        # accès refusé : pause, pas condamnation
    if code in (400, 404, 422):
        return "model"
    if code >= 500:
        return "server"
    return "fatal"


def stream(messages, model: Optional[str] = None, tools=None,
           max_tokens: Optional[int] = None, temperature: float = 0.25,
           task: Optional[str] = None) -> Iterator[tuple]:
    """Générateur d'événements normalisés, avec bascule automatique.

    ``task`` choisit l'ordre des fournisseurs et l'échéance (``agent`` par
    défaut). ``max_tokens`` prend la valeur de la tâche s'il n'est pas fourni.

    Yields ``("text", str)``, ``("tool_calls", [...])``, ``("finish", str)``,
    ``("info", str)``, ``("usage", dict)`` ou ``("error", str)``.
    """
    task = (task or DEFAULT_TASK).lower()
    if task not in TASK_ORDER:
        task = "agent"
    if max_tokens is None:
        max_tokens = TASK_MAX_TOKENS.get(task, 800)
    deadline = time.monotonic() + TASK_DEADLINE.get(task, 90.0)

    targets = chain(model, task=task)
    usable = [t for t in _chain_with_keys(targets) if not _is_dead(t)]
    if not usable:
        yield ("error", "aucune clé LLM disponible — " + unavailable_report(targets))
        return
    if tools:
        # Un modèle sans function calling répondrait en prose : l'agent
        # n'exécuterait rien, sans erreur visible. On l'écarte d'office.
        compatible = [t for t in usable if t.supports_tools]
        if compatible:
            usable = compatible
        else:
            yield ("info", "aucun modèle compatible outils — réponse sans action")

    last_error = ""
    attempted = 0
    for target in usable:
        if time.monotonic() >= deadline:
            yield ("info", "échéance de la tâche atteinte")
            break
        cooling = _cooling(target)
        if cooling:
            yield ("info", f"{target.label} en cooldown ({int(cooling) + 1} s)")
            continue
        attempted += 1
        if attempted > 1:
            STATS["fallbacks"] += 1
            yield ("info", f"bascule → {target.label}")
        response, error, events = _attempt(target, messages, max_tokens,
                                           temperature, tools, deadline)
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
             max_tokens: Optional[int] = None, temperature: float = 0.25,
             task: Optional[str] = None):
    """Appel non streamé. Retourne ``(data, erreur)``.

    Même politique que ``stream()`` : ordre par tâche, cibles mortes écartées,
    outils filtrés, échéance globale et verdicts d'erreur identiques.
    """
    task = (task or DEFAULT_TASK).lower()
    if task not in TASK_ORDER:
        task = "agent"
    if max_tokens is None:
        max_tokens = TASK_MAX_TOKENS.get(task, 800)
    deadline = time.monotonic() + TASK_DEADLINE.get(task, 90.0)

    targets = chain(model, task=task)
    usable = [t for t in _chain_with_keys(targets) if not _is_dead(t)]
    if not usable:
        return None, "aucune clé LLM disponible — " + unavailable_report(targets)
    if tools:
        compatible = [t for t in usable if t.supports_tools]
        if compatible:
            usable = compatible

    last_error = ""
    for target in usable:
        timeout = _timeouts(target, deadline)
        if timeout is None:
            last_error = "échéance de la tâche dépassée"
            break
        if _cooling(target):
            continue
        stripped = False
        while True:
            payload = _payload(target, messages, max_tokens, temperature, tools,
                               stream=False, include_params=not stripped)
            try:
                response = _post(target, payload, stream=False, timeout=timeout)
            except Exception as exc:
                last_error = f"{target.label} : réseau ({type(exc).__name__}: {exc})"
                break
            code = response.status_code
            if code == 200:
                try:
                    data = response.json()
                except Exception as exc:
                    return None, f"réponse illisible : {exc}"
                _record_usage(target, data.get("usage") or {})
                return data, None

            verdict = _classify_code(code)
            body = _safe_text(response)
            if verdict == "model" and code == 400 and not stripped:
                stripped = True
                continue
            if verdict == "quota":
                wait, scope = _quota_wait(response)
                _set_cooldown(target, wait)
                last_error = (f"{target.label} : quota "
                              f"{'journalier' if scope == 'day' else 'atteint'}")
            elif verdict == "auth":
                _dead_providers[target.provider] = body
                last_error = (f"{target.label} : clé invalide ou expirée — "
                              f"régénère {target.missing_key_hint()}")
            elif verdict == "policy":
                _set_cooldown(target, POLICY_COOLDOWN)
                last_error = f"{target.label} : accès refusé (HTTP {code})"
            elif verdict == "model":
                ttl = DEAD_MODEL_TTL if code == 404 else BAD_REQUEST_TTL
                _mark_dead(target, ttl, f"HTTP {code}")
                last_error = f"{target.label} : HTTP {code} {body}"
            else:
                last_error = f"{target.label} : HTTP {code} {body}"
            break
    return None, last_error or "tous les fournisseurs ont échoué"
