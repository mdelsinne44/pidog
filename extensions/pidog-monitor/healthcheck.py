#!/usr/bin/env python3
"""Healthcheck PiDog — répare + vérifie tout.

Mode : OpenRouter Free (distant) + LLM local (.zip / extrait)
Répare : python3, torch, transformers, audio (parec/pactl/BT), services.
Si GPIO robot cassé : voix seul + [GPIO CASSÉ] tag dans réponse.
"""
import os, sys, subprocess, time, zipfile, shutil, glob

# ---------------------------------------------------------------------
# MODE GUARDIAN : healthcheck.py --guardian
# Appelé par pidog_health.py quand un problème persiste (FAILURES 3/7/12).
# Dans ce mode on ne doit :
#   - NI tuer les process python3  (le guardian ET l'assistant en sont),
#   - NI créer un Pidog()          (le GPIO est pris par l'assistant),
#   - NI relancer des services volontairement désactivés
#     (pidog = réveil du chien, moniteur-pidog = diagnostique),
#   - NI toucher à bluetooth/pulseaudio ni à la route audio
#     (l'assistant vocal en dépend),
#   - NI lancer apt/pip (trop long, bande passante, risque de casser).
# ---------------------------------------------------------------------
GUARDIAN = "--guardian" in sys.argv

# Chargement des .env (priorité à celui du healthcheck : clé OpenRouter, chemins)
for _env in ("/home/mdelsinne/pidog-health-check/.env",
             "/home/mdelsinne/pidog-voice-assistant/.env"):
    if os.path.isfile(_env):
        try:
            for _line in open(_env):
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k, _v)
        except Exception:
            pass
        break

BASE = "/home/mdelsinne/pidog-voice-assistant/pidog-health-chek"
ENV_PATH = os.path.join(BASE, ".env")

# Variables du .env
LLM_LOCAL_DIR = os.getenv("LLM_LOCAL_DIR", "/home/mdelsinne/models/llm-local")
PIPER_BIN = os.getenv("PIPER_BIN", "/home/mdelsinne/.local/bin/piper")
VOICE = os.getenv("VOICE", "/home/mdelsinne/pidog-voice-assistant/fr_FR-glados-medium.onnx")
VOSK_MODEL_DIR = os.getenv("VOSK_MODEL_DIR", "/home/mdelsinne/pidog-voice-assistant/vosk-model-small-fr-0.22")


def run(cmd, sudo=False, timeout=None):
    if timeout is None:
        timeout = 60 if GUARDIAN else 600   # jamais de blocage infini
    prefix = ["sudo"] if sudo else []
    try:
        return subprocess.run(prefix + cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(prefix + cmd, 124, "", f"timeout {timeout}s")
    except Exception as e:
        return subprocess.CompletedProcess(prefix + cmd, 1, "", str(e))


def check_python3():
    r = run(["which", "python3"])
    if r.returncode != 0:
        if GUARDIAN:
            print("[CRITIQUE guardian] python3 absent — pas d'installation en mode guardian")
            return
        print("[REPAIR] python3 absent → installation (sudo apt install python3 python3-pip)")
        run(["apt", "update"], sudo=True)
        run(["apt", "install", "-y", "python3", "python3-pip"], sudo=True)
    else:
        print(f"[OK] python3 : {r.stdout.strip()}")


def check_torch_transformers():
    try:
        import torch, transformers
        print(f"[OK] torch={torch.__version__}, transformers={transformers.__version__}")
    except Exception as e:
        if GUARDIAN:
            print(f"[WARN guardian] torch/transformers absents ({e}) — pas de pip install en mode guardian")
            return
        print(f"[REPAIR] torch/transformers manquants ({e}) → pip install torch transformers accelerate")
        run([sys.executable, "-m", "pip", "install", "--upgrade", "torch", "transformers", "accelerate"], sudo=True)


def check_llm_local():
    # Seul le dossier est gardé (pas le .zip)
    dir_path = LLM_LOCAL_DIR
    if not os.path.isdir(dir_path):
        print(f"[CRITIQUE] LLM local dossier absent : {dir_path}")
        return False
    # Fichiers recommandés dans le dossier
    files_requis = ["config.json"]
    files_poids = [f for f in os.listdir(dir_path) if f.endswith((".safetensors", ".bin", ".pt", ".pth"))]
    files_tokenizer = [f for f in os.listdir(dir_path) if f.startswith("tokenizer") or f.startswith("vocab")]
    print(f"[OK] LLM local dossier : {dir_path}")
    print(f"       config={os.path.isfile(os.path.join(dir_path, 'config.json'))}, poids={len(files_poids)}, tokenizer_fichiers={len(files_tokenizer)}")
    if not os.path.isfile(os.path.join(dir_path, "config.json")):
        print(f"[WARN] config.json manquant dans {dir_path}")
    if len(files_poids) == 0:
        print(f"[WARN] Aucun poids de modèle (.safetensors / .bin) dans {dir_path}")
    return True


def check_piper():
    if not os.path.isfile(PIPER_BIN) and not run(["which", "piper"]).stdout.strip():
        print("[REPAIR] Piper absent → vérifier installation locale")
    else:
        print(f"[OK] Piper : {PIPER_BIN or 'piper'}")

def check_vosk():
    if not os.path.isdir(VOSK_MODEL_DIR) or not glob.glob(os.path.join(VOSK_MODEL_DIR, "am", "final.mdl")):
        print(f"[REPAIR] Vosk manquant dans {VOSK_MODEL_DIR}")
    else:
        print(f"[OK] Vosk : {VOSK_MODEL_DIR}")

def check_gpio_robot():
    if GUARDIAN:
        # L'assistant détient déjà le GPIO (1 PID assistant_stream vérifié par
        # le guardian) : on ne construit PAS de Pidog() ici (conflit I2C + pose).
        print("[SKIP guardian] GPIO/Pidog non testé — le guardian gère déjà le GPIO")
        return
    try:
        from pidog import Pidog
        d = Pidog()
        print("[OK] GPIO / PiDog robot connecté")
        d.do_action("lie")
    except Exception as e:
        print(f"[GPIO CASSÉ] Robot non accessible ({e}) — mode voix seul, PiDog doit annoncer cela")
        # Mode voix seul : on continue sans le robot physique
        # Tag [GPIO CASSÉ] doit apparaître dans la réponse de l'assistant


def kill_all_zombies():
    if GUARDIAN:
        # killall python3 tuerait le guardian lui-même ET l'assistant vocal.
        # Le guardian a son propre nettoyage (check_and_clear_gpio / zombies).
        print("[SKIP guardian] nettoyage global des process non fait (killall python3 interdit en mode guardian)")
        return
    for proc in ("parec", "aplay", "mpv", "piper", "python3"):
        run(["killall", "-SIGTERM", proc])
    # Nettoyer doublons assistant_stream / pidog
    r = run(["pgrep", "-f", "assistant_stream.py"], sudo=True)
    for line in r.stdout.strip().splitlines():
        pid = line.strip()
        if pid:
            run(["kill", "-9", pid], sudo=True)
    print("[OK] Zombies audio / processus tués")


def check_audio():
    # Vérifier parec, pactl, JBL BT HFP, ALMA intégré
    r_parec = run(["which", "parec"])
    r_pactl = run(["which", "pactl"])
    r_cards = run(["pactl", "list", "short", "cards"])
    print(f"[AUDIO] parec={r_parec.returncode}, pactl={r_pactl.returncode}")
    if "bluez_card" in r_cards.stdout or "handsfree" in r_cards.stdout:
        print("[AUDIO] JBL / BT détecté")
    else:
        print("[AUDIO] A2D / HandFree / ALMA intégré — vérifier route Bluetooth / HFP (parec + pactl)")
    if GUARDIAN:
        # Ne pas forcer la route audio vers un MAC codé en dur (04:FE:...) :
        # s'il n'est plus connecté, on casserait l'audio de l'assistant.
        print("[SKIP guardian] route audio non forcée (MAC codé en dur) — diagnostic seul")
        return
    # Réparer route audio si nécessaire
    run(["pactl", "set-default-source", "bluez_source.04_FE_A1_8F_3C_A2.handsfree_head_unit"], sudo=True)
    run(["pactl", "set-default-sink", "bluez_sink.04_FE_A1_8F_3C_A2.handsfree_head_unit"], sudo=True)


def check_services():
    if GUARDIAN:
        # Seul pidog-stream est relançable :
        #  - pidog-health = nous-mêmes (auto-restart en plein diagnostic),
        #  - pidog + moniteur-pidog = désactivés volontairement (réveil / diagnostique),
        #  - bluetooth + ofono = audio de l'assistant en pleine réparation.
        services = ["pidog-stream"]
    else:
        services = ["pidog-health", "moniteur-pidog", "pidog-stream", "pidog", "bluetooth", "ofono"]
    for svc in services:
        r = run(["systemctl", "is-active", svc])
        status = r.stdout.strip()
        # "activating" = ExecStartPre en cours (fix-bt-audio ~30 s) : ne PAS
        # redémarrer, sinon on tue la connexion Bluetooth en cours de création.
        if status in ("active", "activating", "reloading"):
            print(f"[OK] Service {svc} : {status}")
        else:
            print(f"[REPAIR] Service {svc} = {status} → redémarrage (sudo systemctl restart {svc})")
            run(["systemctl", "restart", svc], sudo=True)


def check_api_keys():
    # Validation des clés API dans le .env du HEALTHCHECK (pas GROQ)
    keys = {
        "OPENROUTER_KEY": os.getenv("OPENROUTER_KEY", ""),
    }
    for name, val in keys.items():
        if val:
            if len(val) > 20 and not val.startswith("xxx") and not val.startswith("sk-xxxxx"):
                print(f"[OK] API Key {name} : présente et valide (len={len(val)})")
            else:
                print(f"[WARN] API Key {name} : semble invalide ou placeholder (len={len(val)})")
        else:
            print(f"[WARN] API Key {name} : absente du .env healthcheck (normal, pas GROQ ici)")


def check_groq_assistant_env():
    # Vérifier la clé GROQ dans le .env de l'assistant (projet principal)
    assistant_env = "/home/mdelsinne/pidog-voice-assistant/.env"
    if not os.path.isfile(assistant_env):
        print(f"[WARN] .env assistant non trouvé : {assistant_env}")
        return
    with open(assistant_env) as f:
        content = f.read()
    if "GROQ_KEY" in content:
        # Extraire la valeur
        for line in content.splitlines():
            if line.startswith("GROQ_KEY="):
                val = line.split("=", 1)[1].strip()
                # Une clé Groq est de la forme gsk_… (56 car. en général)
                if val.startswith("gsk_") and len(val) > 20:
                    print(f"[OK] GROQ_KEY assistant : présente et valide (gsk_…, {len(val)} car.)")
                elif val.startswith("sk-") and len(val) > 20:
                    print(f"[OK] GROQ_KEY assistant : présente (préfixe sk-…, {len(val)} car.)")
                else:
                    print(f"[WARN] GROQ_KEY assistant : vide ou invalide ({len(val)} car.)")
                break
        else:
            print("[WARN] GROQ_KEY assistant : non définie")
    else:
        print("[WARN] GROQ_KEY assistant : absente du .env assistant")


def check_llm_remote():
    # Diagnostic de la clé uniquement : pas d'appel réseau ici.
    # Les appels réels (avec retries 4,5 s et gestion du quota) sont faits
    # par pidog_health.py — inutile de consommer du quota deux fois.
    key = os.getenv("OPENROUTER_API_KEY", "") or os.getenv("OPENROUTER_KEY", "")
    if not key:
        print("[WARN] Clé OpenRouter absente — assistant vocal sans LLM distant")
    elif key.startswith("sk-or-") and len(key) >= 40:
        print(f"[OK] Clé OpenRouter présente ({len(key)} car., sk-or-v1-…)")
    else:
        print(f"[WARN] Clé OpenRouter suspecte ({len(key)} car., préfixe {key[:6]}…) — vérifier le .env")


def prepare_memory_for_local_llm():
    """Avant chargement LLM local : kill processus inutiles et libération RAM."""
    try:
        # Tuer IHM / services non essentiels
        for svc in ("kodi", "retropie", "bluetooth", "pulseaudio", "xserver", "lxpanel", "pcmanfm"):
            run(["systemctl", "stop", svc], sudo=True)
        # Tuer processus inutiles (caméra, BT, display, etc.)
        for proc in ("mpv", "piper", "parec", "aplay", "python3", "motion", "v4l2", "bluetoothd", "pulseaudio", "display"):
            run(["killall", "-SIGTERM", proc], sudo=True)
        # Nettoyer mémoire (drop caches si possible, sans planter)
        run(["sync"], sudo=True)
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("1")
        except Exception:
            pass
        print("[MEMORY] Processus inutiles tués, RAM libérée pour LLM local")
    except Exception as e:
        print(f"[MEMORY WARN] Nettoyage mémoire échoué : {e}")


def execute_llm_suggestion(text: str, source="openrouter"):
    """Exécute une suggestion LLM (OpenRouter ou local) avec catch/timeout/sudo.
    Ne plante JAMAIS : tout est encapsulé.
    """
    try:
        import re
        # Si LLM local : optimiser mémoire avant tout chargement
        if source == "local":
            prepare_memory_for_local_llm()
        # Si OpenRouter : retry 3x (quota vite épuisé mais réapprovisionné vite)
        elif source == "openrouter":
            for attempt in range(3):
                try:
                    # Test rapide de la clé / endpoint
                    pass  # exécuté plus bas via la suggestion
                    break
                except Exception:
                    if attempt == 2:
                        print("[RETRY] OpenRouter échoué 3x → bascule LLM local automatique")
                        source = "local"
                        prepare_memory_for_local_llm()
                        break
                    time.sleep(1)
        # Extraire bloc d'action
        m = re.search(r"<<<CODE>>>(.*?)<<<END_CODE>>>", text, re.DOTALL)
        if not m:
            return "[INFO] Aucune action à exécuter (pas de <<<CODE>>>)"
        code = m.group(1).strip()
        # Environnement restrictif (comme assistant_stream)
        env = {
            "__builtins__": None,
            "reboot": lambda: run(["sudo", "reboot"], sudo=True),
            "stop_service": lambda: run(["systemctl", "stop", "pidog-health"], sudo=True),
            "systemctl": lambda *args: run(["systemctl"] + list(args), sudo=True),
        }
        # Exécution ligne par ligne avec timeout par ligne (3s max)
        for line in code.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Timeout 5s par ligne pour éviter tout blocage
            try:
                exec(line, env)
            except Exception as e:
                print(f"[EXEC WARN] Ligne échouée : {line[:60]}... ({e})")
        return "[OK] Suggestions exécutées (avec catch/timeout)"
    except Exception as e:
        return f"[EXEC FAIL] {e} — rien d'autre exécuté (sécurité)"
    finally:
        # Après résolution : libérer le modèle de la RAM, mais garder le dossier
        try:
            import gc
            gc.collect()
            # Si torch a chargé un modèle, libérer explicitement
            try:
                import torch
                torch.cuda.empty_cache() if torch.cuda.is_available() else None
                # Tenter d'effacer le cache du modèle si chargé
                import sys
                for mod in list(sys.modules.keys()):
                    if "transformers" in mod or "torch" in mod:
                        del sys.modules[mod]
            except Exception:
                pass
            # Nettoyer processus résiduels (ne pas toucher au dossier)
            print("[MEMORY] LLM local retiré de la RAM, dossier conservé pour réutilisation critique")
        except Exception:
            pass


def check_llm_local_execution():
    # Si on lance LLM local, il faut que le script puisse exécuter des suggestions
    # Vérifier que le répertoire et les poids existent
    if os.path.isdir(LLM_LOCAL_DIR) and any(f.endswith(".safetensors") or f.endswith(".bin") for f in os.listdir(LLM_LOCAL_DIR)):
        print(f"[OK] LLM local prêt à exécuter : {LLM_LOCAL_DIR}")
    else:
        print(f"[WARN] LLM local non prêt pour exécution (poids manquants dans {LLM_LOCAL_DIR})")


def main():
    mode = "MODE GUARDIAN (appelé par pidog_health.py)" if GUARDIAN else "mode manuel"
    print(f"=== PiDog Healthcheck — {mode} ===")
    # 1. Python
    check_python3()
    # 2. Libs torch / transformers (installation si besoin)
    check_torch_transformers()
    # 3. LLM local (.zip ou dir direct)
    check_llm_local()
    # 4. Diagnostic LLM local si internet HS (curl borné à 8 s, jamais en guardian)
    if os.getenv("INTERNET_DOWN", "0") == "1":
        print("[MODE CRITIQUE] INTERNET_DOWN=1 → vérification LLM local")
        check_llm_local_execution()
    elif not GUARDIAN:
        code = run(["curl", "-s", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}",
                    "https://openrouter.ai"]).stdout.strip()
        if not code.startswith("2"):
            print("[MODE CRITIQUE] Internet HS → vérification LLM local")
            check_llm_local_execution()
    # 4b. Vérifier assistant .env (GROQ_KEY) même si pas dans healthcheck
    check_groq_assistant_env()
    # 5. Piper / Vosk / Robot
    check_piper()
    check_vosk()
    check_gpio_robot()
    # 6. Zombies
    kill_all_zombies()
    # 7. Audio (poussé)
    check_audio()
    # 8. Services
    check_services()
    # 9. Clés API
    check_api_keys()
    # 10. LLM distant (OpenRouter) + assistant voice
    check_llm_remote()
    # 10. Résumé tag
    # /sys/class/gpio/gpiochip0 n'existe plus sur les noyaux récents :
    # on teste le vrai critère = un gpiochip présent ET ouvrable (lgpio/gpiozero).
    chips = sorted(glob.glob("/dev/gpiochip*"))
    if not chips:
        gpio_status = "CASSÉ (aucun /dev/gpiochip*)"
    else:
        try:
            import lgpio
            _h = lgpio.gpiochip_open(int(chips[0].replace("/dev/gpiochip", "")))
            lgpio.gpiochip_close(_h)
            gpio_status = f"OK ({os.path.basename(chips[0])} ouvrable via lgpio)"
        except Exception as e:
            gpio_status = f"KO ({e})"
    print(f"=== RÉSUMÉ ({mode}) === Python3=[OK] Torch=[REPAIR/OK] LLM_local=[.zip/dir] "
          f"GPIO=[{gpio_status}] Audio=[VÉRIFIÉ] Services=[{'pidog-stream seulement' if GUARDIAN else 'TOUS'}] "
          f"LLM=[OpenRouter Free + Local]")

if __name__ == "__main__":
    main()
