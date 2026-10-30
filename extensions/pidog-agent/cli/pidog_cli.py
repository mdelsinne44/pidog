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

try:
    from dotenv import load_dotenv
    load_dotenv()
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
            break
        # Arrêt complet du service (pas healthcheck en commande directe — interne)
        if any(w in user_input.lower() for w in ("stoppé", "stoppe", "arrête le service",
                                                 "pi dog terminé", "pidog terminé",
                                                 "terminé pour aujourd'hui", "j'arrête")):
            print("→ STOP SERVICE CLI — arrêt complet, RAM/ GPIO libérés, zombies tués (healthcheck interne).")
            try:
                from soundsign import stop_service as ss
                ss()
            except Exception as e:
                print("Stop CLI:", e)
            break

        mem_text = build_memory_text()
        history.append({"role": "user", "content": user_input})
        reply = ask_llm(user_input, env_text=ENV_TEXT, mem_text=mem_text, history=history[-10:])
        history.append({"role": "assistant", "content": reply})

        import re
        m = re.search(r"<<<CODE>>>(.*?)<<<END_CODE>>>", reply, re.DOTALL)
        if m:
            code = m.group(1).strip()
            clean_reply = reply[:m.start()] + reply[m.end():]
            clean_reply = clean_reply.strip()
            try:
                import subprocess
                from soundsign import get_pidog
                dog = get_pidog()

                def retropie_start(): subprocess.run(["systemctl", "start", "retropie"], capture_output=True)
                def retropie_stop(): subprocess.run(["systemctl", "stop", "retropie"], capture_output=True)
                def retropie_game_cmd(game=""): pass  # placeholder
                def retropie_list_cmd(): pass
                def kodi_start(): subprocess.run(["systemctl", "start", "kodi"], capture_output=True)
                def kodi_stop(): subprocess.run(["systemctl", "stop", "kodi"], capture_output=True)
                def kodi_tv_on(): pass
                def kodi_tv_off(): pass
                def kodi_video_cmd(): pass
                def kodi_pause(): pass
                # reboot / shutdown RETIRES (demande de Martin, 2026-10-30)

                # Mémoire SQLite (base existante dans GoogleDrive)
                def memory_add(key, value): memory.memory_add(str(key), str(value))
                def memory_get(key=""):
                    rows = memory.memory_get(str(key) if key else None)
                    return [(k, v, u) for k, v, u in rows]
                def memory_check_topic(topic=""):
                    return memory.memory_check_topic(str(topic))
                def memory_all():
                    return memory.memory_get()
                def memory_today():
                    return memory.memory_for_date(datetime.datetime.now().strftime("%Y-%m-%d"))

                def stop_service():
                    import subprocess, time, gc, os, sys
                    print("[CLI] stop_service — arrêt complet (healthcheck interne tué, pas voice/cli)", flush=True)
                    for p in ("parec","aplay","mpv","piper","python3"):
                        subprocess.run(["killall","-SIGTERM",p], capture_output=True)
                    try:
                        subprocess.run(["systemctl","stop","moniteur-pidog"], capture_output=True, timeout=3)
                        subprocess.run(["systemctl","stop","pidog-health"], capture_output=True, timeout=3)
                    except Exception:
                        pass
                    time.sleep(0.5)
                    try:
                        from soundsign import release_pidog
                        release_pidog()
                    except Exception:
                        pass
                    try:
                        gc.collect()
                    except Exception:
                        pass

                env = {
                    "__builtins__": None,
                    "dog": dog,
                    "get_pidog": get_pidog,
                    "soundsign": __import__("soundsign"),
                    "retropie_start": retropie_start,
                    "retropie_stop": retropie_stop,
                    "retropie_game_cmd": retropie_game_cmd,
                    "retropie_list_cmd": retropie_list_cmd,
                    "kodi_start": kodi_start,
                    "kodi_stop": kodi_stop,
                    "kodi_tv_on": kodi_tv_on,
                    "kodi_tv_off": kodi_tv_off,
                    "kodi_video_cmd": kodi_video_cmd,
                    "kodi_pause": kodi_pause,
                    "stop_service": stop_service,
                    "memory_add": memory_add,
                    "memory_get": memory_get,
                    "memory_check_topic": memory_check_topic,
                    "memory_all": memory_all,
                    "memory_today": memory_today,
                    "self_info": self_info,
                    "datetime": datetime,
                }
                for line in code.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    exec(line, env)
                print(clean_reply if clean_reply else "(action exécutée)")
            except Exception as e:
                print(clean_reply if clean_reply else reply)
                print(f"[ACTION] Erreur robot : {e}")
        else:
            print(reply)
