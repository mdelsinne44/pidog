#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dernier recours IA du moniteur PiDog.

Appelé par ``monitor.sh`` **uniquement** quand les réparations déterministes
ont échoué. Le helper ne répare rien lui-même : il choisit une action dans une
whitelist FERMÉE, que ``monitor.sh`` revalide et exécute (budget, garde-fous).
Aucune commande shell n'est jamais construite à partir du texte du modèle.

Deux backends :

* **cloud** (réseau disponible) : OpenRouter → Gemini → Groq, au format
  OpenAI-compatible, avec les clés de ``extensions/pidog-agent/.env`` ;
* **local** (plus de réseau) : ``llama-cli`` + un ``*.gguf`` dans
  ``$LLM_LOCAL_DIR``, ou ``transformers`` + ``config.json``. Le modèle n'est
  chargé qu'à cet instant (processus séparé, jamais résident) et sa mémoire est
  plafonnée. Absent = pas d'IA, on le dit et on rend la main.

Sorties :

* par défaut : un objet JSON (debug / humain) ;
* ``--machine`` : ``action<TAB>confiance<TAB>source<TAB>raison`` (monitor.sh) ;
* ``--print-prompt`` : le prompt seul (tests).

Tout ce qui n'est pas une décision valide donne ``action=none`` : le monitor
préfère ne rien faire plutôt que d'exécuter une bêtise.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

# Actions que l'IA a le droit de proposer. ``reboot`` en est exclu par défaut :
# redémarrer la machine est une décision déterministe et budgétée, pas un avis
# de modèle. monitor.sh revalide de toute façon cette liste.
ACTIONS = {
    "rfkill_unblock": "débloque le Wi-Fi coupé par rfkill (soft block)",
    "wifi_reapply": "réapplique la configuration Wi-Fi de wlan0 (DHCP/DNS)",
    "wifi_provision": "recrée le profil Wi-Fi depuis SSID+clé (monitor.env) si le profil a disparu",
    "wifi_up": "réactive le profil Wi-Fi enregistré",
    "nm_restart": "redémarre NetworkManager (réseau/DNS bloqué)",
    "dns_restart": "redémarre systemd-resolved si présent",
    "tailscale_restart": "redémarre tailscaled",
    "tailscale_up": "relance `tailscale up` (relogin si la clé est encore valide)",
    "ssh_restart": "redémarre sshd (bannière absente)",
    "bluetooth_restart": "redémarre bluetooth (audio du stream)",
    "audio_restart": "relance PulseAudio",
    "none": "ne rien faire (aucune action justifiée)",
}
FORBIDDEN = {"reboot"}          # jamais proposé par l'IA (sauf --allow-reboot)

DEFAULT_OPENROUTER = "deepseek/deepseek-v4.1-flash-1017,openrouter/free"
DEFAULT_GEMINI = "gemini-3.5-flash-lite,gemini-3.8-flash"
DEFAULT_GROQ = "openai/gpt-oss-20b,allam-2-7b"

PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions",
                   ("OPENROUTER_API_KEY",), "LLM_OPENROUTER_MODELS", DEFAULT_OPENROUTER),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "LLM_GEMINI_MODELS", DEFAULT_GEMINI),
    "groq": ("https://api.groq.com/openai/v1/chat/completions",
             ("GROQ_KEY",), "LLM_MODEL_CLI", DEFAULT_GROQ),
}

# (connexion, lecture) : OpenRouter gratuit met plus longtemps à démarrer.
TIMEOUTS = {"openrouter": (5.0, 40.0), "gemini": (5.0, 25.0), "groq": (5.0, 20.0)}
# Échéance globale : mieux vaut renoncer que bloquer le timer de 15 min.
DEADLINE = float(os.getenv("MON_AI_DEADLINE", "70"))
MAX_REASON = 240


# ------------------------------------------------------------------ .env -----
def load_env(path: str) -> None:
    """Charge un ``.env`` minimal sans écraser l'environnement existant."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def env_list(name: str, default: str) -> List[str]:
    raw = os.getenv(name) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


def first_key(names) -> Optional[str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


# ------------------------------------------------------------- contexte -----
def read_tsv(path: str, limit: int = 0) -> List[List[str]]:
    rows: List[List[str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                rows.append(raw.split("\t"))
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def build_context(args) -> dict:
    checks = [
        {"name": row[0], "ok": row[1] == "1",
         "severity": row[2] if len(row) > 2 else "info",
         "detail": row[3] if len(row) > 3 else ""}
        for row in read_tsv(args.checks) if len(row) >= 4
    ]
    hardware = {
        row[0]: row[1]
        for row in read_tsv(args.hardware) if len(row) >= 2
    }
    attempts = [
        {"ts": row[0], "action": row[1], "ok": row[2] == "1",
         "source": row[3] if len(row) > 3 else "",
         "detail": row[4] if len(row) > 4 else ""}
        for row in read_tsv(args.audit, limit=12) if len(row) >= 3
    ]
    return {"online": bool(args.online), "checks": checks,
            "hardware": hardware, "attempts": attempts}


def allowed_actions(args) -> dict:
    actions = dict(ACTIONS)
    if args.allow_reboot:
        actions["reboot"] = "redémarrer la machine (dernier recours, budgété)"
    return actions


def build_prompt(context: dict, actions: dict) -> str:
    catalogue = "\n".join(f"- {name} : {desc}" for name, desc in actions.items())
    failed = [c for c in context["checks"]
              if not c["ok"] and c["severity"] in ("critical", "important")]
    state = json.dumps({
        "reseau_disponible": context["online"],
        "problemes": failed,
        "materiel": context["hardware"],
        "tentatives_recentes": context["attempts"],
        "autres_controles": [{"name": c["name"], "ok": c["ok"]}
                           for c in context["checks"] if c not in failed],
    }, ensure_ascii=False)
    return (
        "Tu es le diagnosticien d'un Raspberry Pi qui héberge un robot (PiDog). "
        "Le monitor a déjà tenté les réparations déterministes : elles ont échoué "
        "ou le budget est épuisé. Choisis AU PLUS UNE action pour rétablir le "
        "service, ou 'none' si rien n'est justifié.\n\n"
        "Actions autorisées (aucune autre n'existe) :\n"
        f"{catalogue}\n\n"
        "État courant (JSON) :\n"
        f"{state}\n\n"
        "Contraintes :\n"
        "- Réponds UNIQUEMENT par un objet JSON, sans texte autour, sans markdown.\n"
        "- Schéma exact : {\"action\": \"<nom>\", \"confidence\": 0.0, \"reason\": \"<20 mots max>\"}\n"
        "- N'invente pas de commande, de chemin, d'option ni d'IP.\n"
        "- Ne propose pas une action déjà tentée deux fois sans succès.\n"
        "- Si le matériel est en alerte (sous-tension, surchauffe), ne propose rien "
        "qui redémarre des services : 'none'.\n"
        "- En cas de doute, 'none' avec une confiance basse.\n"
    )


# ---------------------------------------------------------------- cloud -----
def post_cloud(url: str, key: str, model: str, prompt: str,
               json_mode: bool, timeout=(5.0, 30.0)) -> Tuple[Optional[str], str]:
    try:
        import requests
    except ImportError:
        return None, "module requests absent"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Tu réponds uniquement en JSON valide."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 220,
        "temperature": 0.0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}"}
    if "openrouter.ai" in url:
        headers["HTTP-Referer"] = os.getenv("OPENROUTER_REFERER",
                                            "https://github.com/pidog")
        headers["X-Title"] = "PiDog Monitor"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as exc:                     # réseau, DNS, TLS…
        return None, f"réseau ({type(exc).__name__}: {exc})"
    if response.status_code == 400 and json_mode:
        return post_cloud(url, key, model, prompt, json_mode=False, timeout=timeout)
    if response.status_code != 200:
        return None, f"HTTP {response.status_code} {(response.text or '')[:160]}"
    try:
        data = response.json()
        return data["choices"][0]["message"]["content"], ""
    except Exception as exc:
        return None, f"réponse illisible ({type(exc).__name__})"


def cloud_decision(prompt: str, args) -> Tuple[Optional[str], str, str]:
    import time
    deadline = time.monotonic() + DEADLINE
    order = [p.strip().lower() for p in
             (os.getenv("MON_AI_PROVIDERS") or "openrouter,gemini,groq").split(",")
             if p.strip()]
    errors = []
    for name in order:
        if name not in PROVIDERS:
            continue
        url, keys, model_env, default = PROVIDERS[name]
        key = first_key(keys)
        if not key:
            continue
        for model in env_list(model_env, default):
            if time.monotonic() >= deadline:
                errors.append(f"{name}: échéance IA ({int(DEADLINE)}s) atteinte")
                return None, "", " ; ".join(errors[-4:])
            text, error = post_cloud(url, key, model, prompt,
                                     json_mode=(name != "gemini"),
                                     timeout=TIMEOUTS.get(name, (5.0, 30.0)))
            if text:
                return text, f"{name}:{model}", ""
            errors.append(f"{name}:{model} → {error}")
            if "HTTP 401" in error or "HTTP 404" in error:
                break                       # clé morte / modèle retiré
    return None, "", " ; ".join(errors[-4:]) or "aucune clé cloud disponible"


# ---------------------------------------------------------------- local -----
def local_decision(prompt: str, actions: dict) -> Tuple[Optional[str], str, str]:
    directory = os.path.expanduser(
        os.getenv("LLM_LOCAL_DIR", "~/models/llm-local"))
    gguf = sorted(glob.glob(os.path.join(directory, "*.gguf")))
    # ~/.local/bin n'est PAS dans le PATH d'un shell non interactif (SSH,
    # timer systemd) : on cherche le wrapper install-local-llm.sh en direct.
    cli = shutil.which(os.getenv("LLM_LOCAL_CLI", "llama-cli"))
    if cli is None:
        for candidate in ("~/.local/bin/llama-cli",
                          "~/.local/lib/pidog-llama/llama-cli"):
            candidate = os.path.expanduser(candidate)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                cli = candidate
                break
    if gguf and cli:
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".gbnf", delete=False, encoding="utf-8")
        try:
            handle.write(build_grammar(actions))
            handle.close()
            output, error = _run_llama(cli, gguf[0], prompt, handle.name)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        if output is None:
            return None, "", error
        if prompt and prompt in output:
            output = output.split(prompt, 1)[-1]
        return output, f"local:{os.path.basename(gguf[0])}", ""
    if os.path.isfile(os.path.join(directory, "config.json")):
        try:
            from transformers import pipeline  # type: ignore
        except ImportError:
            return None, "", "transformers absent"
        try:
            pipe = pipeline("text-generation", model=directory,
                            tokenizer=directory, local_files_only=True)
            output = pipe(prompt, max_new_tokens=160, do_sample=False)[0]["generated_text"]
            if prompt and prompt in output:
                output = output.split(prompt, 1)[-1]
            return output, "local:transformers", ""
        except Exception as exc:
            return None, "", f"transformers ({type(exc).__name__}: {exc})"
    return None, "", (f"modèle local absent ({directory}) : ni *.gguf + llama-cli, "
                      f"ni transformers + config.json")


# ------------------------------------------------------------- décision -----
def _json_objects(text: str):
    """Itère les objets JSON équilibrés du texte, du dernier au premier."""
    for start in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:index + 1]
                    break


def extract_json(text: str) -> Optional[dict]:
    """Dernier objet JSON valide (llama-cli peut recracher le prompt)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    for candidate in _json_objects(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------- local -----
def _gbnf_quote(text: str) -> str:
    """Littéral GBNF (guillemets + antislashs échappés)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_grammar(actions) -> str:
    """Grammaire GBNF : JSON garanti, action limitée à la whitelist."""
    alternatives = " | ".join(_gbnf_quote('"' + name + '"') for name in actions)
    return "\n".join([
        "root ::= \"{\" ws " + _gbnf_quote('"action"') + " ws \":\" ws action"
        + " ws \",\" ws " + _gbnf_quote('"confidence"') + " ws \":\" ws confidence"
        + " ws \",\" ws " + _gbnf_quote('"reason"') + " ws \":\" ws reason ws \"}\"",
        "action ::= " + alternatives,
        'confidence ::= "0" ( "." [0-9]+ )? | "1" ( "." "0"+ )?',
        "reason ::= " + _gbnf_quote('"') + ' [^"]* ' + _gbnf_quote('"'),
        "ws ::= [ \\t\\n]*",
    ]) + "\n"


def _limit_memory():
    """Plafonne l'espace d'adressage de l'enfant llama-cli (anti-OOM)."""
    try:
        import resource
        megabytes = int(os.getenv("MON_LOCAL_MEM_MB", "1300"))
        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass


def _run_llama(cli: str, model: str, prompt: str, grammar_path: str):
    """Essaie plusieurs jeux d'options selon la version de llama.cpp."""
    threads = os.getenv("MON_LOCAL_THREADS", "3")
    context = os.getenv("MON_LOCAL_CTX", "2048")
    tokens = os.getenv("MON_LOCAL_N", "200")
    timeout = float(os.getenv("MON_LOCAL_TIMEOUT", "120"))
    base = [cli, "-m", model, "-p", prompt, "-n", tokens,
            "--temp", "0", "-c", context, "-t", threads]
    attempts = [
        base + ["--grammar-file", grammar_path, "-st", "--no-display-prompt"],
        base + ["--grammar-file", grammar_path, "-st"],
        base + ["--grammar-file", grammar_path],
        base,
    ]
    last_error = ""
    for args in attempts:
        try:
            result = subprocess.run(
                args, capture_output=True, timeout=timeout, text=True,
                encoding="utf-8", errors="replace",
                preexec_fn=_limit_memory)
        except Exception as exc:
            return None, f"llama-cli ({type(exc).__name__}: {exc})"
        if result.returncode == 0 and (result.stdout or "").strip():
            return result.stdout, ""
        stderr = (result.stderr or "").strip()
        last_error = f"rc={result.returncode} {stderr[-300:]}"
        lowered = stderr.lower()
        if result.returncode != 0 and not any(
                hint in lowered for hint in
                ("unknown argument", "invalid argument", "unrecognized",
                 "error: invalid", "unknown option")):
            return None, last_error
    return None, last_error or "llama-cli n'a rien produit"



def normalize(data: Optional[dict], actions: dict) -> Tuple[str, float, str]:
    if not isinstance(data, dict):
        return "none", 0.0, "réponse non JSON"
    action = str(data.get("action", "")).strip().lower()
    reason = str(data.get("reason", "") or "").replace("\n", " ").strip()
    reason = reason[:MAX_REASON]
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if action not in actions:
        return "none", 0.0, f"action hors whitelist ({action or 'vide'}) — {reason}"[:MAX_REASON]
    return action, confidence, reason or "sans justification"


def main() -> int:
    parser = argparse.ArgumentParser(description="IA de dernier recours PiDog Monitor")
    parser.add_argument("--checks", default="")
    parser.add_argument("--audit", default="")
    parser.add_argument("--hardware", default="")
    parser.add_argument("--online", type=int, default=0)
    parser.add_argument("--env", default="")
    parser.add_argument("--machine", action="store_true")
    parser.add_argument("--print-prompt", action="store_true")
    parser.add_argument("--print-grammar", action="store_true")
    parser.add_argument("--offline", action="store_true",
                        help="ignore le cloud et n'utilise que le modèle local")
    parser.add_argument("--allow-reboot", action="store_true")
    args = parser.parse_args()

    if args.env:
        load_env(args.env)
    if args.offline:
        args.online = 0

    actions = allowed_actions(args)
    context = build_context(args)
    prompt = build_prompt(context, actions)

    if args.print_prompt:
        print(prompt)
        return 0
    if args.print_grammar:
        print(build_grammar(actions), end="")
        return 0

    if args.online:
        text, source, error = cloud_decision(prompt, args)
    else:
        text, source, error = local_decision(prompt, actions)

    if not text:
        decision = {"ok": False, "action": "none", "confidence": 0.0,
                    "source": source, "reason": f"IA indisponible : {error}"}
    else:
        data = extract_json(text)
        action, confidence, reason = normalize(data, actions)
        decision = {"ok": True, "action": action, "confidence": confidence,
                    "source": source or "ia", "reason": reason}

    if args.machine:
        def clean(value: str) -> str:
            return str(value).replace("\t", " ").replace("\n", " ")[:MAX_REASON]
        print("\t".join(clean(decision[k]) if k != "confidence" else str(decision[k])
                        for k in ("action", "confidence", "source", "reason")))
    else:
        print(json.dumps(decision, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
