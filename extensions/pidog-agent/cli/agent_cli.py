# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""PiDog CLI — agent de développement (terminal / SSH).

Différence avec l'ancienne version : ce n'est plus un « chat » qui bricole des
blocs ``<<<CODE>>>``. C'est un agent qui **appelle des outils** (function
calling Groq) : état du robot, actions physiques, mémoire, services systemd,
logs, shell en lecture seule. Il boucle observer → agir → vérifier, et streame
sa réponse pendant qu'il travaille.

Usage :
    pidog agent cli                 # session normale
    pidog agent cli --debug         # trace des appels LLM/outils
    pidog agent cli --model qwen/qwen3.8-27b
    pidog agent cli --no-stream     # réponse en un bloc (debug réseau)

Commandes de session (préfixe « / ») :
    /help /state /actions /mem <q> /remember clé = valeur
    /forget <clé> /stats /model [nom] /reset /debug /quit

Le robot n'est JAMAIS instancié ici : les actions partent en RPC vers le
daemon partagé (`pidog start`). Sans daemon, l'agent reste un binôme de dev.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

try:
    import readline  # noqa: F401 — historique + flèches
except Exception:  # pragma: no cover
    readline = None

# ------------------------------------------------------------------ chemins --
CLI_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(CLI_DIR)                      # extensions/pidog-agent
REPO_ROOT = os.path.dirname(os.path.dirname(BASE))    # racine du dépôt
for _p in (REPO_ROOT, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except Exception:
    pass

import requests

import memory
import tools
from pidog_prompt import cli_prompt

# ------------------------------------------------------------------- debug ---
DEBUG = False
DEBUG_LOG = os.path.join(CLI_DIR, "debug_trace.log")
DEBUG_LOG_MAX = 2 * 1024 * 1024

C_DIM = "\033[90m"
C_GREEN = "\033[32m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{C_RESET}" if sys.stdout.isatty() else text


def debug_log(step: str, detail: str = "") -> None:
    line = (f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')} "
            f"[{step}] {detail}")
    try:
        if os.path.isfile(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > DEBUG_LOG_MAX:
            os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
        if DEBUG:
            with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
    if DEBUG:
        print(_color(f"[debug] {line}", C_DIM), flush=True)


# --------------------------------------------------------------------- LLM ---
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_KEY", "")
MODEL = os.getenv("LLM_MODEL_CLI") or os.getenv("LLM_MODEL") or "openai/gpt-oss-120b"
SYSTEM_PROMPT = cli_prompt()
MAX_ROUNDS = 6
# 800 tokens suffisent largement pour un tour de terminal, et chaque token de
# complétion compte dans le quota TPM du tier gratuit (8 000/min).
MAX_TOKENS = int(os.getenv("PIDOG_MAX_TOKENS", "800"))
# Un résultat d'outil est renvoyé au modèle : 1200 car. ≈ 300 tokens. Sans ce
# plafond, `sys_info` ou `journalctl` font exploser le budget en un seul appel.
MAX_TOOL_CHARS = int(os.getenv("PIDOG_MAX_TOOL_CHARS", "1200"))
TEMPERATURE = 0.25
# Repli automatique quand le modèle principal sature son quota TPM.
FALLBACK_MODELS = [m.strip() for m in
                   os.getenv("LLM_FALLBACKS", "openai/gpt-oss-20b").split(",")
                   if m.strip()]
RETRY_MAX = 2          # tentatives supplémentaires sur 429
RETRY_MAX_WAIT = 35.0  # au-delà, on rend la main plutôt que de bloquer le shell

_rate_limited_until = 0.0


def _quota_wait(response=None) -> float:
    """Déduit le délai d'un 429 (header ou corps JSON)."""
    wait = 20.0
    if response is not None:
        for header in ("retry-after", "x-ratelimit-reset-requests",
                       "x-ratelimit-reset-tokens"):
            raw = response.headers.get(header)
            if not raw:
                continue
            try:
                wait = max(1.0, float(str(raw).rstrip("s")))
                break
            except Exception:
                pass
        try:
            message = response.json().get("error", {}).get("message", "")
            match = re.search(r"try again in ([\d.]+)\s*(ms|s|m)", message, re.I)
            if match:
                value, unit = float(match.group(1)), match.group(2).lower()
                wait = (value / 1000 if unit == "ms"
                        else value * 60 if unit == "m" else value)
        except Exception:
            pass
    return max(1.0, min(90.0, wait))


def _post(payload, stream: bool):
    return requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_KEY}",
                 "Content-Type": "application/json"},
        json=payload,
        stream=stream,
        timeout=(10, 120),
    )


def _model_chain(model=None) -> list[str]:
    """Modèle demandé, puis replis (dédoublonnés, ordre conservé)."""
    chain: list[str] = []
    for candidate in [model or MODEL, *FALLBACK_MODELS]:
        if candidate and candidate not in chain:
            chain.append(candidate)
    return chain


def _post_with_retry(payload):
    """POST non-streamé avec la même politique 429 que ``groq_stream``.

    Retourne ``(data, erreur)`` ; ``data`` vaut ``None`` en cas d'échec.
    """
    chain = _model_chain(payload.get("model"))
    retries = 0
    while True:
        payload["model"] = chain[0]
        try:
            response = _post(payload, stream=False)
        except Exception as exc:
            return None, f"réseau : {type(exc).__name__}: {exc}"
        if response.status_code == 200:
            try:
                return response.json(), None
            except Exception as exc:
                return None, f"réponse illisible : {exc}"
        if response.status_code != 429:
            return None, f"Groq HTTP {response.status_code} : {response.text[:300]}"
        wait = _quota_wait(response)
        if wait <= RETRY_MAX_WAIT and retries < RETRY_MAX:
            retries += 1
            print(_color(f"[info] quota TPM atteint — nouvelle tentative dans "
                         f"{int(wait)} s ({retries}/{RETRY_MAX})", C_YELLOW),
                  flush=True)
            time.sleep(wait)
            continue
        if len(chain) > 1:
            dropped = chain.pop(0)
            retries = 0
            print(_color(f"[info] {dropped} saturé — bascule sur {chain[0]}",
                         C_YELLOW), flush=True)
            continue
        return None, f"quota Groq atteint (429) — réessaie dans {int(wait)} s"


def _build_payload(messages, model, stream, tools_enabled=True):
    """Construit le corps de requête (paramètres communs streaming/non-streaming).

    ``reasoning_effort`` n'est envoyé qu'aux modèles gpt-oss : sur ce type de
    modèle, le « reasoning » consomme le budget ``max_tokens`` et, sans ce
    réglage, la réponse finale peut arriver **vide** (finish_reason=length
    après ~800 tokens de réflexion). Constaté sur gpt-oss-120b le 03/11.
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": stream,
    }
    if str(model).startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = os.getenv("PIDOG_REASONING_EFFORT", "low")
        payload["include_reasoning"] = False
    if tools_enabled:
        payload["tools"] = tools.TOOLS
        payload["tool_choice"] = "auto"
    return payload


def groq_stream(messages, model=None, tools_enabled=True):
    """Générateur d'événements normalisés.

    Yields ``("text", str)``, ``("tool_calls", [{id,name,arguments}])``,
    ``("info", str)`` ou ``("error", str)``.

    Politique 429 : on attend le délai annoncé par Groq et on réessaie
    (``RETRY_MAX`` fois) ; au-delà on change de modèle (replis plus légers) ;
    en dernier recours on rend la main avec un cooldown global, plutôt que de
    bloquer le shell indéfiniment.
    """
    global _rate_limited_until
    if not GROQ_KEY:
        yield ("error", "GROQ_KEY absente — renseigne extensions/pidog-agent/.env")
        return
    now = time.time()
    if now < _rate_limited_until:
        yield ("error", f"quota Groq en cooldown ({int(_rate_limited_until - now) + 1}s)")
        return

    chain = _model_chain(model)
    response = None
    retries = 0
    while True:
        payload = _build_payload(messages, chain[0], True, tools_enabled)

        debug_log("LLM_REQUEST", f"model={payload['model']} "
                                 f"messages={len(messages)} "
                                 f"chars={sum(len(str(m.get('content') or '')) for m in messages)}")
        try:
            response = _post(payload, stream=True)
        except Exception as exc:
            yield ("error", f"réseau : {type(exc).__name__}: {exc}")
            return

        if response.status_code == 200:
            break
        if response.status_code != 429:
            yield ("error", f"Groq HTTP {response.status_code} : {response.text[:300]}")
            return

        wait = _quota_wait(response)
        try:
            response.close()
        except Exception:
            pass
        if wait <= RETRY_MAX_WAIT and retries < RETRY_MAX:
            retries += 1
            yield ("info", f"quota TPM atteint sur {chain[0]} — "
                           f"nouvelle tentative dans {int(wait)} s "
                           f"({retries}/{RETRY_MAX})")
            time.sleep(wait)
            continue
        if len(chain) > 1:
            dropped = chain.pop(0)
            retries = 0
            yield ("info", f"{dropped} saturé — bascule sur {chain[0]}")
            continue
        _rate_limited_until = time.time() + wait
        yield ("error", f"quota Groq atteint (429) — réessaie dans {int(wait)} s")
        return

    calls: dict[int, dict] = {}
    finish_reason = None
    try:
        for raw in response.iter_lines():
            if not raw or not raw.startswith(b"data: "):
                continue
            payload_raw = raw[6:]
            if payload_raw == b"[DONE]":
                break
            try:
                chunk = json.loads(payload_raw)
                choice = chunk["choices"][0]
                delta = choice["delta"]
            except Exception:
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            content = delta.get("content")
            if content:
                yield ("text", content)
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                slot = calls.setdefault(index, {"id": "", "name": "",
                                                "arguments": ""})
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

    if calls:
        yield ("tool_calls", [calls[i] for i in sorted(calls)])
    yield ("finish", finish_reason)


# ---------------------------------------------------------------- contexte ---
def _robot_brief() -> str:
    try:
        import pidog
        state = pidog.service_state()
        if state.get("ready"):
            try:
                volts = pidog.get_pidog().get_battery_voltage()
                return f"prêt ({volts:.2f} V)"
            except Exception:
                return "prêt (tension illisible)"
        return f"{state.get('state')} ({state.get('error') or 'non prêt'})"
    except Exception as exc:
        return f"service non interrogeable ({type(exc).__name__})"


def _git_brief() -> str:
    try:
        import subprocess
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=3).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               cwd=REPO_ROOT, capture_output=True, text=True,
                               timeout=3).stdout.strip()
        return f"{branch or '?'} ({'modifs en cours' if dirty else 'propre'})"
    except Exception:
        return "git indisponible"


def build_context(user_text: str) -> str:
    now = datetime.datetime.now()
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
            "dimanche"]
    parts = [
        f"Date : {now.strftime('%Y-%m-%d %H:%M')} ({days[now.weekday()]})",
        f"Répertoire : {os.getcwd()}",
        f"Dépôt ~/pidog : {_git_brief()}",
        f"Robot : {_robot_brief()}",
        f"Profil (résumé) : {memory.memory_profile() or '—'}",
    ]
    return ("[CONTEXTE ACTUEL — résumé automatique, PAS une mesure]\n"
            + "\n".join(parts)
            + "\nToute valeur demandée (température, RAM, service, tension, "
              "posture, logs) doit être lue via un outil pendant ce tour. "
              "Utilise memory_search pour les détails de mémoire.")


# --------------------------------------------------------------- historique --
def _trim_history(history, max_messages: int = 12, max_chars: int = 6000):
    history[:] = history[-max_messages:]
    total = sum(len(str(m.get("content") or "")) for m in history)
    while len(history) > 2 and total > max_chars:
        total -= len(str(history[0].get("content") or ""))
        history.pop(0)


def _tool_content(result: dict, budget: int = MAX_TOOL_CHARS) -> str:
    """Sérialise un résultat d'outil dans le budget TPM (≈300 tokens).

    On réduit d'abord les plus grosses chaînes (logs, sorties shell) pour
    garder un JSON valide et les champs structurés (ok, temp_c, count…).
    """
    blob = json.dumps(result, ensure_ascii=False)
    if len(blob) <= budget:
        return blob
    slim = dict(result)
    for _ in range(8):
        key = max((k for k, v in slim.items() if isinstance(v, str)),
                  key=lambda k: len(slim[k]), default=None)
        if key is None:
            break
        blob = json.dumps(slim, ensure_ascii=False)
        if len(blob) <= budget:
            return blob
        overflow = len(blob) - budget + 40
        text = slim[key]
        keep = max(80, len(text) - overflow)
        slim[key] = text[:keep] + f"… (+{len(text) - keep} car.)"
    return json.dumps(slim, ensure_ascii=False)[:budget]


def _summarize_result(result: dict) -> str:
    if not isinstance(result, dict):
        return str(result)[:120]
    if result.get("ok"):
        for key in ("result", "active", "count", "temp_c", "removed", "info"):
            if key in result:
                value = result[key]
                return str(value)[:160]
        return "ok"
    return "échec : " + str(result.get("error"))[:160]


# ------------------------------------------------------------- boucle agent --
def run_turn(user_text, history, model=None, stream=True):
    """Un tour complet : LLM ⇄ outils, avec streaming de la réponse finale."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": build_context(user_text)},
        *history[-10:],
        {"role": "user", "content": user_text},
    ]
    printed: list[str] = []

    for round_index in range(1, MAX_ROUNDS + 1):
        calls = []
        text = []
        finish_reason = None
        if not stream:
            # Repli non-streaming : même politique de quota, réponse d'un bloc.
            payload = _build_payload(messages, model or MODEL, False)
            data, error = _post_with_retry(payload)
            if error:
                print(_color(f"[erreur] {error}", C_RED))
                return
            try:
                choice = data["choices"][0]
                message = choice["message"]
            except Exception as exc:
                print(_color(f"[erreur] réponse inattendue : {exc}", C_RED))
                return
            finish_reason = choice.get("finish_reason")
            if message.get("content"):
                text.append(message["content"])
                sys.stdout.write(message["content"])
                sys.stdout.flush()
            for call in message.get("tool_calls") or []:
                calls.append({"id": call["id"],
                              "name": call["function"]["name"],
                              "arguments": call["function"]["arguments"]})
        else:
            for kind, payload in groq_stream(messages, model=model):
                if kind == "text":
                    sys.stdout.write(payload)
                    sys.stdout.flush()
                    text.append(payload)
                elif kind == "tool_calls":
                    calls = payload
                elif kind == "finish":
                    finish_reason = payload
                elif kind == "info":
                    sys.stdout.write("\n")
                    print(_color(f"[info] {payload}", C_YELLOW), flush=True)
                elif kind == "error":
                    sys.stdout.write("\n")
                    print(_color(f"[erreur] {payload}", C_RED))
                    return

        if text:
            printed.append("".join(text))
        if not calls:
            break

        # Trace assistant (tool_calls) puis exécution séquentielle.
        messages.append({
            "role": "assistant",
            "content": "".join(text) or None,
            "tool_calls": [{
                "id": call["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": call["name"],
                             "arguments": call["arguments"] or "{}"},
            } for i, call in enumerate(calls)],
        })
        if text:
            sys.stdout.write("\n")
            sys.stdout.flush()

        for call in calls:
            pretty = _color(
                f"  ⤷ {call['name']}({call['arguments'][:120]})", C_DIM)
            print(pretty)
            debug_log("TOOL_CALL", f"{call['name']} {call['arguments'][:400]}")
            started = time.time()
            result = tools.execute_tool(call["name"], call["arguments"])
            elapsed = (time.time() - started) * 1000
            summary = _summarize_result(result)
            mark = _color("✓", C_GREEN) if result.get("ok") else _color("✗", C_RED)
            print(_color(f"  {mark} {summary} ({elapsed:.0f} ms)", C_DIM))
            debug_log("TOOL_RESULT", json.dumps(result, ensure_ascii=False)[:800])
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"] or f"call_{calls.index(call)}",
                "content": _tool_content(result),
            })
    else:
        print(_color(f"[info] {MAX_ROUNDS} allers-retours d'outils atteints — "
                     "je m'arrête là.", C_YELLOW))

    final = "".join(printed).strip()
    if not final:
        if finish_reason == "length":
            print(_color(f"[info] budget de sortie épuisé ({MAX_TOKENS} tokens) "
                         "avant la réponse — reformule ou augmente "
                         "PIDOG_MAX_TOKENS.", C_YELLOW))
        else:
            print(_color("[info] réponse vide du modèle.", C_YELLOW))
    elif stream:
        print()
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": final[:2000]})
    _trim_history(history)
    memory._maybe_snapshot()  # noqa: SLF001 — export JSON opportuniste


# ------------------------------------------------------------- commandes -----
HELP = """\
Commandes de session :
  /state              état robot + services
  /actions            catalogue d'actions physiques
  /mem <texte>        recherche mémoire (retrieval scoré)
  /remember k = v     ajoute un fait durable
  /forget <clé>       désactive une entrée
  /stats              stats mémoire + modèle courant
  /model [nom]        affiche/change le modèle
  /reset              vide l'historique de conversation
  /debug              bascule la trace détaillée
  /quit               quitter
Tout le reste est envoyé à l'agent (qui peut utiliser ses outils)."""


def handle_command(line: str, history, state: dict) -> bool:
    """Retourne False quand il faut quitter."""
    cmd, _, arg = line[1:].partition(" ")
    cmd, arg = cmd.strip().lower(), arg.strip()

    if cmd in ("quit", "q", "exit"):
        return False
    if cmd in ("help", "h", "?"):
        print(HELP)
    elif cmd == "state":
        print(json.dumps(tools.execute_tool("robot_state", {}),
                         ensure_ascii=False, indent=2))
        print(json.dumps(tools.execute_tool("sys_services", {}),
                         ensure_ascii=False, indent=2))
    elif cmd == "actions":
        print(tools.execute_tool("robot_actions", {}))
    elif cmd == "mem":
        rows = memory.memory_search(arg)
        if not rows:
            print("(rien trouvé)")
        for row in rows:
            print(f"  {row['key']:22} {row['value']}  "
                  f"[{str(row['updated_at'])[:16]}]")
    elif cmd in ("remember", "add"):
        key, _, value = arg.partition("=")
        if not value:
            print("usage : /remember clé = valeur")
        else:
            memory.memory_add(key.strip(), value.strip())
            print("noté.")
    elif cmd == "forget":
        print(f"{memory.memory_forget(arg)} entrée(s) désactivée(s).")
    elif cmd == "stats":
        print(json.dumps(memory.memory_stats(), ensure_ascii=False, indent=2))
        print(f"modèle : {state['model']}  |  outils : {len(tools.TOOLS)}  |  "
              f"historique : {len(history)} messages")
    elif cmd == "model":
        if arg:
            state["model"] = arg
            print(f"modèle → {arg}")
        else:
            print(state["model"])
    elif cmd == "reset":
        history.clear()
        print("historique vidé.")
    elif cmd == "debug":
        global DEBUG
        DEBUG = not DEBUG
        print(f"debug {'ON' if DEBUG else 'OFF'}")
    else:
        print(f"commande inconnue : /{cmd} ( /help )")
    return True


# ------------------------------------------------------------------- main ----
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pidog agent cli",
        description="PiDog CLI — agent de dev avec fonction calling.",
    )
    parser.add_argument("--debug", "-d", action="store_true",
                        help="trace LLM + outils dans debug_trace.log")
    parser.add_argument("--model", "-m", default=None,
                        help="modèle Groq (défaut : LLM_MODEL_CLI)")
    parser.add_argument("--no-stream", action="store_true",
                        help="réponse non streamée (diagnostic)")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv=None) -> int:
    global DEBUG
    args = parse_args(argv if argv is not None else sys.argv[1:])
    DEBUG = args.debug

    memory.memory_init()
    state = {"model": args.model or MODEL}
    debug_log("START", f"argv={sys.argv[1:]} cwd={os.getcwd()} model={state['model']}")

    robot = _robot_brief()
    stats = memory.memory_stats()
    print(_color("PiDog CLI — agent de dev", C_CYAN))
    print(f"  modèle : {state['model']}")
    print(f"  robot  : {robot}")
    print(f"  mémoire: {stats['active']} faits actifs — {stats['db']}")
    print(f"  outils : {len(tools.TOOLS)} "
          "(robot, mémoire, systemd, logs, shell lecture seule)")
    print("  /help pour les commandes, Ctrl+D pour quitter.\n")

    history: list[dict] = []
    while True:
        try:
            raw = input(_color("vous › ", C_CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir.")
            break
        if not raw:
            continue
        if raw.startswith("/"):
            if not handle_command(raw, history, state):
                break
            continue
        try:
            run_turn(raw, history, model=state["model"],
                     stream=not args.no_stream)
        except KeyboardInterrupt:
            print(_color("\n[interrompu]", C_YELLOW))
        except Exception as exc:
            debug_log("TURN_ERROR", f"{type(exc).__name__}: {exc}")
            print(_color(f"[erreur] {type(exc).__name__}: {exc}", C_RED))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
