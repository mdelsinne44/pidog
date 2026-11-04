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
import hashlib
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


def _log_usage(usage) -> None:
    """Trace les tokens consommés et l'efficacité du cache du fournisseur."""
    if not isinstance(usage, dict) or not usage:
        return
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    hit = f"{100 * cached / prompt:.0f} %" if prompt else "—"
    debug_log("LLM_USAGE", f"in={prompt} cached={cached} ({hit}) out={completion}")


# --------------------------------------------------------------------- LLM ---
import llm_providers as llm

MODEL = os.getenv("LLM_MODEL_CLI") or os.getenv("LLM_MODEL") or llm.DEFAULT_GROQ_MODELS
SYSTEM_PROMPT = cli_prompt()
MAX_ROUNDS = 6
# 800 tokens suffisent largement pour un tour de terminal, et chaque token de
# complétion compte dans le quota TPM du tier gratuit (8 000/min chez Groq).
MAX_TOKENS = int(os.getenv("PIDOG_MAX_TOKENS", "800"))
# Un résultat d'outil est renvoyé au modèle : 1200 car. ≈ 300 tokens. Sans ce
# plafond, `sys_info` ou `journalctl` font exploser le budget en un seul appel.
MAX_TOOL_CHARS = int(os.getenv("PIDOG_MAX_TOOL_CHARS", "1200"))
TEMPERATURE = 0.25
RETRY_MAX = llm.RETRY_MAX
RETRY_MAX_WAIT = llm.RETRY_MAX_WAIT

# Empreinte du préfixe stable (prompt système + schémas d'outils). Groq, Gemini
# et OpenRouter cachent automatiquement un préfixe **identique** : si cette
# empreinte change d'un tour à l'autre, le cache ne peut pas mordre. Elle est
# loguée en debug pour vérifier, et le préfixe doit rester en tête de requête.
_PREFIX_SHA = hashlib.sha256(
    (SYSTEM_PROMPT + json.dumps(tools.TOOLS, sort_keys=True,
                                ensure_ascii=False)).encode()
).hexdigest()[:12]


def _budget(messages) -> str:
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    tools_chars = len(json.dumps(tools.TOOLS, ensure_ascii=False))
    return (f"messages={len(messages)} chars={chars} "
            f"tools={len(tools.TOOLS)}={tools_chars} car "
            f"≈ {(chars + tools_chars) // 4} tokens")


def llm_stream(messages, model=None, tools_enabled=True):
    """Événements normalisés, routés pour la tâche « agent ».

    ``model`` peut forcer un fournisseur : ``gemini/gemini-3.5-flash-lite``,
    ``openrouter/…`` ou ``groq/qwen/qwen3.8-27b``.
    """
    debug_log("LLM_REQUEST", f"prefix={_PREFIX_SHA} {_budget(messages)}")
    debug_log("LLM_CHAIN",
              " → ".join(t.label for t in llm.chain(model, task="agent")))
    yield from llm.stream(messages, model=model, task="agent",
                          tools=tools.TOOLS if tools_enabled else None,
                          max_tokens=MAX_TOKENS, temperature=TEMPERATURE)


# Nom conservé pour les imports existants (tests, scripts externes).
groq_stream = llm_stream


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

    On réduit d'abord les plus grosses valeurs (chaînes, puis listes/dicts)
    pour garder un JSON valide et les champs structurés (ok, temp_c, count…).
    """
    def dump(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    blob = dump(result)
    if len(blob) <= budget:
        return blob
    slim = dict(result)
    for _ in range(12):
        blob = dump(slim)
        if len(blob) <= budget:
            return blob
        key = max(slim, key=lambda k: len(dump(slim[k])), default=None)
        if key is None:
            break
        value = slim[key]
        if isinstance(value, str):
            overflow = len(blob) - budget + 40
            keep = max(80, len(value) - overflow)
            slim[key] = value[:keep] + f"… (+{len(value) - keep} car.)"
        elif isinstance(value, list) and value:
            slim[key] = value[:-1] or "…"
        elif isinstance(value, dict):
            slim[key] = dump(value)[:160] + "…"
        else:
            slim[key] = "…"
    return dump(slim)[:budget]


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
            # Repli non-streaming : même politique de bascule entre providers.
            debug_log("LLM_REQUEST", f"prefix={_PREFIX_SHA} {_budget(messages)} (no-stream)")
            data, error = llm.complete(messages, model=model or MODEL,
                                       task="agent",
                                       tools=tools.TOOLS,
                                       max_tokens=MAX_TOKENS,
                                       temperature=TEMPERATURE)
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
                elif kind == "usage":
                    _log_usage(payload)
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
  /web <requête>      recherche Internet (DuckDuckGo)
  /run <code python>  exécute un script Python dans ~/pidog
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
    elif cmd == "web":
        print(json.dumps(tools.execute_tool("web_search", {"query": arg}),
                         ensure_ascii=False, indent=2))
    elif cmd in ("run", "python", "py"):
        print(json.dumps(tools.execute_tool("run_python", {"code": arg}),
                         ensure_ascii=False, indent=2))
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
        cache = llm.cache_stats()
        print(f"modèle : {state['model']}  |  outils : {len(tools.TOOLS)}  |  "
              f"historique : {len(history)} messages")
        print(f"llm    : {cache['calls']} appels, "
              f"{cache['prompt_tokens']} tok entrée, "
              f"{cache['completion_tokens']} tok sortie, "
              f"cache {cache['cache_hit_pct']} % "
              f"({cache['cached_tokens']} tok), "
              f"{cache['fallbacks']} bascules")
        for label, row in cache["by_target"].items():
            hit = (f"{100 * row['cached'] / row['prompt']:.0f} %"
                   if row["prompt"] else "—")
            print(f"  {label:38} {row['calls']:>3} appels  "
                  f"{row['prompt']:>7} tok  cache {hit}")
        print("chaîne : " + " → ".join(t.label for t in llm.chain(state['model'])))
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
                        help="modèle de départ (défaut : LLM_MODEL_CLI). "
                             "Préfixe possible : gemini/…, allam/…, "
                             "openrouter/… pour forcer un fournisseur")
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
    chain = [t.label for t in llm.chain(state["model"]) if t.available()]
    print(f"  chaîne : {' → '.join(chain) if chain else '— (aucune clé)'}")
    print(f"  robot  : {robot}")
    print(f"  mémoire: {stats['active']} faits actifs — {stats['db']}")
    print(f"  outils : {len(tools.TOOLS)} "
          "(robot, mémoire, systemd, shell, code Python, Internet)")
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
