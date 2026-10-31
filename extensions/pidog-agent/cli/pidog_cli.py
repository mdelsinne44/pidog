#!/usr/bin/env python3
"""PiDog CLI — mode texte pur pour SSH / terminal.

Usage:
    python3 pidog_cli.py
    # puis tapez vos questions ; tapez 'quit' ou Ctrl-D pour sortir.

Le prompt de personnalité est partagé avec l'assistant vocal via
pidog_prompt.cli_prompt() — même chien, deux médiums.
"""
import os, sys, requests
import datetime

# Modules partagés avec la voix : racine de l'extension (env_vision, memory,
# pidog_prompt, pidog_core) + voice/ (pidog_self). Nécessaire quand on lance
# depuis cli/.
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BASE, os.path.join(BASE, "voice")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except Exception:
    pass

GROQ_KEY = os.getenv("GROQ_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")

# Prompt partagé (personnalité unifiée avec la voix)
from pidog_prompt import cli_prompt
SYSTEM_PROMPT = cli_prompt()

import env_vision
from pidog_self import self_info
from memory_load import load_memory
import memory

memory.memory_init()
# Libération agressive au démarrage (évite les résidus d'une session précédente)
try:
    from pidog_core import release_pidog
    release_pidog()
except Exception:
    pass

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli_conversation.log")
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_trace.log")

def log_line(line):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {line}\n")

def debug_log(step, detail=""):
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')} [{step}] {detail}\n")


def ask_llm(text, env_text="", mem_text="", history=None):
    if not GROQ_KEY:
        return "[ERREUR] Clé GROQ_KEY non définie. Définissez-la dans .env ou export GROQ_KEY=..."
    try:
        sys_msg = SYSTEM_PROMPT
        if env_text:
            sys_msg += f"\n[Environnement] {env_text}"
        if mem_text:
            sys_msg += f"\n[Mémoire] {mem_text}"
        messages = [{"role": "system", "content": sys_msg}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.3,
            },
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        else:
            return f"[ERREUR LLM] {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"[ERREUR] {e}"


def build_memory_text():
    """Mémoire + date du jour/demain, comme côté voix."""
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    mem_lines = memory.memory_get()
    mem_text = "; ".join(f"{k}={v}" for k, v, _ in mem_lines[:15])
    return f"[JOUR ACTUEL : {today}, DEMAIN : {tomorrow}] {mem_text}"


if __name__ == "__main__":
    print("=== PiDog CLI (texte pur / SSH) ===")
    print("Tapez votre message, 'quit' pour quitter.\n")

    # L'environnement (BT / audio / réseau / robot) est statique en SSH :
    # on le calcule une seule fois pour ne pas pénaliser chaque réponse.
    try:
        env_summary = env_vision.env_summary()
        ENV_TEXT = "; ".join(f"{k}:{v}" for k, v in env_summary.items())
    except Exception as e:
        ENV_TEXT = f"(environnement indisponible : {e})"

    history = []  # mini contexte de discussion, comme côté voix

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "q", "exit", "bye"):
            print("Au revoir !")
            try:
                from pidog_core import release_pidog
                release_pidog()
            except Exception:
                pass
            break
        # Arrêt complet du service (pas healthcheck en commande directe — interne)
        if any(w in user_input.lower() for w in ("stoppé", "stoppe", "arrête le service",
                                                 "pi dog terminé", "pidog terminé",
                                                 "terminé pour aujourd'hui", "j'arrête")):
            print("→ STOP SERVICE CLI — arrêt complet, RAM/ GPIO libérés, zombies tués (healthcheck interne).")
            try:
                from pidog_core import stop_service as ss
                ss()
            except Exception as e:
                print("Stop CLI:", e)
            break

        mem_text = build_memory_text()
        history.append({"role": "user", "content": user_input})
        debug_log("ASK_LLM_START", f"msg={user_input!r} env={ENV_TEXT[:60]} mem={mem_text[:60]}")
        try:
            reply = ask_llm(user_input, env_text=ENV_TEXT, mem_text=mem_text, history=history[-10:])
            debug_log("ASK_LLM_END", f"len_reply={len(reply)} code_block={'<<<CODE>>>' in reply}")
        except Exception as e:
            try:
                from pidog_core import release_pidog
                release_pidog()
            except Exception:
                pass
            print(f"[ERREUR] {e}")
            reply = f"[ERREUR] {e}"
        history.append({"role": "assistant", "content": reply})

        import re
        m = re.search(r"<<<CODE>>>(.*?)<<<END_CODE>>>", reply, re.DOTALL)
        # Bloc code désactivé — évite le blocage get_pidog() / GPIO busy
        clean_reply = reply[:m.start()] + reply[m.end():] if m else reply
        clean_reply = clean_reply.strip()
        log_line(f"IN={user_input!r} OUT={clean_reply!r}")
        print(clean_reply if clean_reply else reply, flush=True)
