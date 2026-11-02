# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""PiDog CLI — mode texte pour SSH / terminal.

Usage :
    pidog agent cli
    pidog agent cli --debug        # trace chaque étape (console + fichier)

Le robot n'est JAMAIS instancié ici. Si `pidog start` tourne, les actions
partent en RPC vers le daemon partagé ; sinon le CLI reste en discussion.
"""
import argparse
import datetime
import os
import re
import sys
import time
import traceback

try:
    import readline  # noqa: F401 — active l'historique et les flèches du terminal
except Exception:
    readline = None

# ------------------------------------------------------------------ chemins --
CLI_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(CLI_DIR)                 # extensions/pidog-agent
REPO_ROOT = os.path.dirname(os.path.dirname(BASE))  # racine du dépôt
for _p in (REPO_ROOT, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except Exception:
    pass

import requests

from pidog_prompt import cli_prompt
import env_vision
import memory
from agent_self import self_info
from memory_load import load_memory
from action_runner import execute_action_block, extract_action_code
from local_actions import infer_actions, actions_to_code, describe_actions

# ------------------------------------------------------------------- debug ---
DEBUG = False
LOG_PATH = None  # conversation.log supprimé sur demande
DEBUG_LOG = os.path.join(CLI_DIR, "debug_trace.log")


def debug_log(step, detail=""):
    """Trace horodatée : toujours dans le fichier, console seulement --debug."""
    line = (
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')} "
        f"[{step}] {detail}"
    )
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if DEBUG:
        print(f"\033[90m[debug] {line}\033[0m", flush=True)


def log_line(line):  # conversation.log désactivé
    pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {line}\n")
    except Exception:
        pass


# --------------------------------------------------------------------- LLM ---
GROQ_KEY = os.getenv("GROQ_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")
SYSTEM_PROMPT = cli_prompt()

# Prompt minimal pour les commandes d'action : moins de tokens = moins de 429.
ACTION_SYSTEM_PROMPT = (
    "Tu es PiDog, chien robot. Réponds en français, 1 phrase courte. "
    "Tu DOIS inclure un bloc <<<CODE>>> avec dog.action(...) pour la commande.\n"
    "Actions utiles : posture(name=stand|sit|lie), led(color=red|green|blue|"
    "yellow|cyan|magenta|pink|white|rainbow, style=breath, off=True), "
    "head_gesture(name=look_left|look_right|look_up|look_down|center|nod|shake|"
    "tilt_left|tilt_right), lift_paw(leg=front_left|front_right), "
    "wave_paw(leg=...), handshake, high_five, wag(times=4), bark, howl, "
    "walk(direction=forward|backward), turn(direction=left|right), stretch, "
    "push_up, celebrate, battery, stop.\n"
    "Exemple : <<<CODE>>>dog.action(\"posture\", name=\"sit\")<<<END_CODE>>>"
)

# Quota Groq : après un 429, on attend le délai indiqué au lieu de marteler.
_rate_limited_until = 0.0
_rate_limit_reason = ""


def _quota_wait(response=None):
    """Déduit le délai d'attente d'un 429 (header ou corps JSON)."""
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
                value = float(match.group(1))
                unit = match.group(2).lower()
                wait = value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
        except Exception:
            pass
    return max(1.0, min(120.0, wait))



def build_memory_text():
    """Mémoire + date du jour/demain, comme côté voix."""
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    mem_lines = memory.memory_get()
    mem_text = "; ".join(f"{k}={v}" for k, v, _ in mem_lines[:15])
    return f"[JOUR ACTUEL : {today}, DEMAIN : {tomorrow}] {mem_text}"


def ask_llm(text, env_text="", mem_text="", history=None,
            action_intent=False):
    global _rate_limited_until, _rate_limit_reason
    if not GROQ_KEY:
        debug_log("LLM_SKIP", "GROQ_KEY absente")
        return "[ERREUR] Clé GROQ_KEY non définie. Définissez-la dans .env ou export GROQ_KEY=..."

    now = time.time()
    if now < _rate_limited_until:
        remaining = int(_rate_limited_until - now) + 1
        debug_log("LLM_QUOTA_BLOCKED", f"attente {remaining}s")
        return f"[QUOTA] Quota Groq atteint. Réessaie dans {remaining} s."

    try:
        # En mode action : prompt minimal, sans historique/mémoire/environnement.
        if action_intent:
            sys_msg = ACTION_SYSTEM_PROMPT
            history = None
            env_text = ""
            mem_text = ""
        else:
            sys_msg = SYSTEM_PROMPT
            if env_text:
                sys_msg += f"\n[Environnement] {env_text}"
            if mem_text:
                sys_msg += f"\n[Mémoire] {mem_text}"
        messages = [{"role": "system", "content": sys_msg}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})
        approx_tokens = sum(len(m.get("content", "")) for m in messages) // 4

        debug_log(
            "LLM_REQUEST",
            f"model={LLM_MODEL} messages={len(messages)} approx_in={approx_tokens} user={text[:80]!r}",
        )
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "max_tokens": 200 if action_intent else 280,
                "temperature": 0.3,
            },
            timeout=20,
        )
        debug_log("LLM_RESPONSE", f"http={r.status_code} bytes={len(r.content)}")
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code == 429:
            sec = _quota_wait(r)
            _rate_limited_until = time.time() + sec
            debug_log("LLM_RATE_LIMIT", f"429 reçu, cooldown {sec}s")
            return f"[QUOTA] Quota Groq (429) atteint. Attends {int(sec)} s."
        return f"[ERREUR LLM] {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        debug_log("LLM_EXCEPTION", f"{type(exc).__name__}: {exc}")
        return f"[ERREUR] {exc}"


# ---------------------------------------------------------------- actions ---
# L'exécution est mutualisée avec la voix dans `action_runner.py` : mêmes
# actions autorisées, mêmes garde-fous (paliers de posture + tension).


# ------------------------------------------------------------------- main ---
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pidog agent cli",
        description="CLI texte PiDog (le robot vient du service `pidog start`).",
    )
    parser.add_argument(
        "--debug", "-d", action="store_true",
        help="trace chaque étape (console + debug_trace.log)",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv=None):
    global DEBUG, SYSTEM_PROMPT
    args = parse_args(argv if argv is not None else sys.argv[1:])
    DEBUG = args.debug

    debug_log("START", f"argv={sys.argv[1:]} cwd={os.getcwd()}")
    memory.memory_init()
    debug_log("MEMORY_INIT", "ok")

    # --- état du service / robot (aucune instanciation locale) ---
    try:
        import pidog
        state = pidog.service_state()
        robot_available = bool(state.get("ready"))
        robot_status = pidog.status()
        debug_log(
            "ROBOT_STATUS",
            f"ready={robot_available} state={state.get('state')} "
            f"error={state.get('error')} socket={pidog.service.SOCKET_PATH}",
        )
    except Exception as exc:
        robot_available = False
        robot_status = f"Module service non importable ({exc})"
        debug_log("ROBOT_STATUS_ERROR", f"{type(exc).__name__}: {exc}")

    if robot_available:
        try:
            self_text = self_info()
        except Exception:
            self_text = ""
        if self_text:
            SYSTEM_PROMPT += f"\n[MOI : {self_text}]"
        print("=== PiDog CLI — robot disponible ===")
        print(f"   État : {robot_status}")
        print("   Vous pouvez discuter ET demander des actions au robot (RPC daemon).")
        SYSTEM_PROMPT += (
            "\n[Le robot PiDog est connecté. Pour toute action physique, utilise "
            "dog.action(...) dans un bloc <<<CODE>>> (catalogue complet dans les "
            "règles ci-dessus). Le mot « PiDog » en début de message annonce une "
            "commande : déduis l'action attendue, même si la formulation est "
            "imagée.]"
        )
    else:
        print("=== PiDog CLI — robot non disponible ===")
        print(f"   État : {robot_status}")
        print("   Discussion uniquement (pas de bloc <<<CODE>>> actif).")
        print("   Lancez `pidog start` dans un autre terminal pour activer le robot.")
        SYSTEM_PROMPT += (
            "\n[ATTENTION : le robot PiDog n'est pas connecté. Ne propose pas de "
            "bloc <<<CODE>>> dans ta réponse, car il ne pourra pas être exécuté. "
            "Réponds uniquement par du texte.]"
        )

    print("\n=== PiDog CLI (texte pur / SSH) ===")
    print("Tapez votre message, 'quit' pour quitter. --debug trace les étapes.\n")

    try:
        env_summary = env_vision.env_summary()
        ENV_TEXT = "; ".join(f"{k}:{v}" for k, v in env_summary.items())
        debug_log("ENV", ENV_TEXT[:300])
    except Exception as exc:
        ENV_TEXT = f"(environnement indisponible : {exc})"
        debug_log("ENV_ERROR", str(exc))

    history = []
    # « PiDog » (ou « Pi Dog ») en tête = commande adressée au chien.
    wake_re = re.compile(
        r"^\s*(?:ok(?:ay)?\s+)?(?:pi\s*dog(?:ue)?|pidog|pydog)\b[\s,.:!\-]*",
        re.IGNORECASE,
    )

    while True:
        try:
            raw_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir.")
            debug_log("EXIT", "EOF/KeyboardInterrupt")
            break
        if not raw_input:
            continue
        if raw_input.lower() in ("quit", "q", "exit", "bye"):
            print("Au revoir !")
            debug_log("EXIT", "commande utilisateur")
            break

        wake = wake_re.match(raw_input)
        action_intent = bool(wake) and robot_available
        user_input = raw_input[wake.end():].strip() if wake else raw_input
        if not user_input:
            if action_intent:
                print("Oui ? Dis-moi ce que je dois faire.")
            continue

        # Arrêt explicite du service partagé (le daemon, pas seulement le CLI).
        if any(
            w in user_input.lower()
            for w in (
                "stoppé", "stoppe", "arrête le service", "pi dog terminé",
                "pidog terminé", "terminé pour aujourd'hui", "j'arrête",
            )
        ):
            print("→ STOP SERVICE — arrêt du daemon partagé (robot couché, GPIO libéré).")
            try:
                import pidog
                pidog.stop()
                debug_log("SERVICE_STOP", "ok")
            except Exception as exc:
                print(f"Stop CLI : {exc}")
                debug_log("SERVICE_STOP_ERROR", str(exc))
            break

        mem_text = build_memory_text()
        history.append({"role": "user", "content": user_input})
        debug_log(
            "ASK_LLM_START",
            f"msg={user_input!r} action_intent={action_intent}",
        )

        try:
            # history[:-1] : évite d'envoyer deux fois le message courant
            reply = ask_llm(
                user_input,
                env_text=ENV_TEXT,
                mem_text=mem_text,
                history=history[:-1][-4:],
                action_intent=action_intent,
            )
        except Exception as exc:
            debug_log("ASK_LLM_ERROR", f"{type(exc).__name__}: {exc}")
            print(f"[ERREUR] {exc}")
            reply = f"[ERREUR] {exc}"

        clean_reply, code = extract_action_code(reply)

        # Une commande « PiDog ... » doit produire une action. Si le LLM a
        # oublié le bloc CODE, on lui redemande une fois, sans polluer
        # l'historique de discussion.
        if action_intent and robot_available and not code:
            debug_log("ACTION_RETRY", "bloc CODE absent malgré le préfixe PiDog")
            try:
                retry = ask_llm(
                    user_input + (
                        "\n\n[ACTION OBLIGATOIRE : réponds avec un bloc "
                        "<<<CODE>>> contenant dog.action(...). Choisis l'action "
                        "la plus proche de ma demande, même si elle est imagée.]"
                    ),
                    env_text=ENV_TEXT,
                    mem_text=mem_text,
                    history=history[:-1][-4:],
                    action_intent=True,
                )
                retry_clean, retry_code = extract_action_code(retry)
                if retry_code:
                    reply, clean_reply, code = retry, retry_clean, retry_code
                    debug_log("ACTION_RETRY_OK", retry_code[:120])
            except Exception as exc:
                debug_log("ACTION_RETRY_ERROR", f"{type(exc).__name__}: {exc}")

        history.append({"role": "assistant", "content": clean_reply or reply})
        debug_log(
            "ASK_LLM_END",
            f"len_reply={len(reply)} code_block={code is not None}",
        )

        if code:
            if robot_available:
                ok, detail = execute_action_block(code, debug_log=debug_log)
                if ok:
                    log_line(f"IN={raw_input!r} OUT=[ACTION OK] {detail}")
                    print("[PI DOG] Action exécutée via le daemon.", flush=True)
                    debug_log("ACTION_OK", detail)
                else:
                    log_line(f"IN={raw_input!r} OUT=[ACTION FAIL] {detail}")
                    print(f"[PI DOG] Échec action : {detail}", flush=True)
                    debug_log("ACTION_FAIL", detail)
            else:
                print(
                    "[PI DOG] Robot indisponible — action non exécutée "
                    "(lancez `pidog start`).",
                    flush=True,
                )
                debug_log("ACTION_SKIP", "robot non disponible")

        log_line(f"IN={raw_input!r} OUT={clean_reply!r}")
        print(clean_reply if clean_reply else reply, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
