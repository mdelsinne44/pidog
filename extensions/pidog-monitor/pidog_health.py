#!/usr/bin/env python3
"""
PiDog Health Guardian Service
Runs continuously (checks every 5 minutes), ensuring:
- Network (WiFi / wlan0) & Tailscale & SSH (port 22) are healthy
- Bluetooth and PulseAudio are running
- Voice Assistant (pidog-stream.service) is started ONLY when audio/mic source is ready
- Prevents infinite loops or GPIO busy states
"""

import subprocess
import time
import os
import sys
import logging
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"
if not ENV_FILE.exists():
    ENV_FILE = Path("/home/mdelsinne/pidog-voice-assistant/.env")
if ENV_FILE.exists():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
        print(f"Chargé .env : {ENV_FILE}")
    except Exception as e:
        print(f"Impossible de charger .env : {e}")

# Le guardian lit OPENROUTER_API_KEY, le .env fourni utilise OPENROUTER_KEY :
# on normalise les deux noms pour qu'une seule clé suffise.
if not os.environ.get("OPENROUTER_API_KEY") and os.environ.get("OPENROUTER_KEY"):
    os.environ["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_KEY"]

FAILURES = 0
DEEP_REPAIR_DONE = False   # réparation profonde déjà tentée dans cette session
OPENROUTER_OK = None       # None = clé non testée, True/False après le probe

# Arrêt volontaire de l'assistant (créé par soundsign.stop_service())
STOPPED_FLAG = Path("/home/mdelsinne/pidog-voice-assistant/.assistant_stopped")
# Compteur de reboots persistant : survit au reboot ET au restart cron horaire
REBOOT_FILE = Path.home() / ".cache" / "pidog" / "reboot_count"
REBOOT_MAX_PER_DAY = 3
HEALTHCHECK = "/home/mdelsinne/pidog-health-check/healthcheck.py"

LOG_FILE = "/home/mdelsinne/pidog-voice-assistant/health.log"
WIFI_KEY = "znkaDw5NoD3Ayd2Uhp"  # Clé WiFi Livebox-C210 (Orange) récupérée localement
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def log(msg):
    print(msg, flush=True)
    logging.info(msg)

def run_cmd(cmd, timeout=10):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return res.returncode == 0, res.stdout.strip()
    except Exception as e:
        return False, str(e)

# =====================================================================
# --- GARDE-FOU : arrêt VOLONTAIRE de l'assistant ----------------------
# =====================================================================
def assistant_held_stopped():
    """Vrai si l'assistant a été stoppé volontairement (flag créé par
    soundsign.stop_service()). Le flag n'est respecté que s'il date du boot
    en cours : il est purgé sinon (pas de blocage après un redémarrage)."""
    try:
        if not STOPPED_FLAG.exists():
            return False
        boot_time = time.time() - float(open("/proc/uptime").read().split()[0])
        if STOPPED_FLAG.stat().st_mtime < boot_time:
            log("Flag .assistant_stopped antérieur au boot — purge (l'assistant pourra redémarrer)")
            STOPPED_FLAG.unlink()
            return False
        return True
    except Exception:
        return False


# =====================================================================
# --- RÉPARATION PROFONDE (healthcheck.py en mode guardian) -----------
# =====================================================================
def run_deep_repair(reason):
    """Réparation profonde = healthcheck.py --guardian.
    Ce mode ne tue NI les process python3, NI le GPIO du robot, et ne
    relance que les services attendus (voir healthcheck.py --guardian)."""
    global DEEP_REPAIR_DONE
    log(f"Réparation profonde lancée (healthcheck.py --guardian) — motif : {reason}")
    try:
        res = subprocess.run(["python3", HEALTHCHECK, "--guardian"],
                             capture_output=True, text=True, timeout=300)
        tail = " | ".join((res.stdout or "").strip().splitlines()[-6:])
        log(f"Réparation profonde terminée (rc={res.returncode}) : {tail[:500]}")
        DEEP_REPAIR_DONE = True
    except subprocess.TimeoutExpired:
        log("Réparation profonde : timeout 300s — on continue sans elle")
    except Exception as e:
        log(f"Réparation profonde impossible : {e}")


# =====================================================================
# --- COMPTEUR DE REBOOT PERSISTANT (max 3 par jour) ------------------
# =====================================================================
def reboot_count_get():
    """Survit au reboot ET au restart cron horaire ; remis à zéro chaque jour."""
    try:
        date, n = REBOOT_FILE.read_text().split()
        return int(n) if date == time.strftime("%Y-%m-%d") else 0
    except Exception:
        return 0


def reboot_count_set(n):
    try:
        REBOOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REBOOT_FILE.write_text(f"{time.strftime('%Y-%m-%d')} {n}\n")
    except Exception as e:
        log(f"Compteur de reboot non persisté : {e}")


# =====================================================================
# --- OPENROUTER : test unique de la clé (zéro appel inutile) ---------
# =====================================================================
def openrouter_usable():
    """Teste la clé et retourne True/False. Retries façon OpenCode : 4,5 s
    entre chaque essai (5 essais max) — ça fait passer les limites par minute.
    - 401/403 → clé refusée : plus aucun appel (zéro 401 dans le log).
    - 429 daily → quota JOURNALIER : les retries ne suffisent pas, on attend
      le reset (minuit UTC) — le cron horaire relance le guardian → re-test."""
    global OPENROUTER_OK
    if OPENROUTER_OK is not None:
        return OPENROUTER_OK
    import urllib.request, urllib.error, json
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        log("OpenRouter : aucune clé (OPENROUTER_API_KEY / OPENROUTER_KEY) — appel désactivé")
        OPENROUTER_OK = False
        return False
    url = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
    payload = json.dumps({
        "model": os.environ.get("OPENROUTER_MODEL", "openrouter/free").split("#")[0].strip(),
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 1,
    }).encode()
    attempts, delay = 5, 4.5
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                OPENROUTER_OK = (resp.status == 200)
            if OPENROUTER_OK:
                log(f"OpenRouter : clé acceptée (essai {i}/{attempts})")
                return True
            log(f"OpenRouter : réponse inattendue (essai {i}/{attempts})")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="ignore")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                log(f"OpenRouter : clé refusée (HTTP {e.code}) — plus aucun appel OpenRouter")
                OPENROUTER_OK = False
                return False
            if e.code == 429 and "daily" in body:
                # Limite par jour : inutile d'insister dans la seconde.
                log("OpenRouter : quota JOURNALIER épuisé (429 daily) — re-test au prochain "
                    "cycle du guardian (cron horaire) ; reset à minuit UTC / 01h locale")
                OPENROUTER_OK = False
                return False
            log(f"OpenRouter : HTTP {e.code} (essai {i}/{attempts}) — {body[:120]}")
        except Exception as e:
            log(f"OpenRouter : {e} (essai {i}/{attempts})")
        if i < attempts:
            time.sleep(delay)
    log(f"OpenRouter : toujours en échec après {attempts} essais (4,5 s d'écart) — désactivé pour cette session")
    OPENROUTER_OK = False
    return OPENROUTER_OK


def check_ssh_tailscale():
    # Check ssh service
    ok, _ = run_cmd("systemctl is-active --quiet ssh")
    if not ok:
        log("SSH service down. Restarting...")
        run_cmd("sudo systemctl restart ssh")

    # Check tailscale service
    ok, _ = run_cmd("systemctl is-active --quiet tailscaled")
    if not ok:
        log("Tailscale service down. Restarting...")
        run_cmd("sudo systemctl restart tailscaled")

def check_network():
    # Check if wlan0 has an IP
    ok, out = run_cmd("ip -4 addr show wlan0")
    if "inet" not in out:
        log("WiFi wlan0 has no IP. Attempting reconnection/bring up...")
        run_cmd("sudo ip link set wlan0 up")
        run_cmd("sudo systemctl restart networking || sudo systemctl restart dhcpcd || true")

def check_audio_bluetooth():
    # Check bluetooth service
    ok, _ = run_cmd("systemctl is-active --quiet bluetooth")
    if not ok:
        log("Bluetooth service down. Restarting...")
        run_cmd("sudo systemctl restart bluetooth")

    # Check PulseAudio
    ok, _ = run_cmd("pulseaudio --check")
    if not ok:
        log("PulseAudio not running. Starting...")
        run_cmd("pulseaudio --start")


def check_and_clear_gpio():
    import subprocess, time, os
    my_pid = os.getpid()
    try:
        res = subprocess.run(["pgrep", "-f", "assistant_stream.py"], capture_output=True, text=True)
        pids = [p for p in res.stdout.strip().split("\n") if p and int(p) != my_pid]
        if len(pids) == 1:
            log("GPIO OK : 1 PID assistant_stream (service normal) — rien à faire")
            return
        elif len(pids) > 1:
            log(f"GPIO occupé par {len(pids)} PIDs assistan — kill all + restart")
            for pid in pids:
                try:
                    log(f"Kill PID {pid} (ancien assistant/bloquant)")
                    subprocess.run(["kill", "-TERM", pid])
                except Exception:
                    pass
            time.sleep(2)
            if assistant_held_stopped():
                log("Flag STOP présent — on ne redémarre pas l'assistant (arrêt volontaire).")
                return
            log("Restart pidog-stream.service après libération GPIO")
            subprocess.run(["sudo", "systemctl", "restart", "pidog-stream.service"], capture_output=True)
        else:
            log("Pas de PID assistant trouvé — rien à tuer")
    except Exception as e:
        log(f"Erreur check GPIO : {e}")

def check_and_kill_zombies():
    try:
        # Scanner tous les zombies python et agir sur leur parent
        res = subprocess.run(["ps", "-eo", "pid,ppid,stat,args"], capture_output=True, text=True)
        zombies = []
        parents = set()
        for line in res.stdout.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) >= 3:
                pid_str, ppid_str, stat = parts[0], parts[1], parts[2]
                args = parts[3] if len(parts) > 3 else ""
                if stat.startswith("Z") and ("python" in args.lower() or "defunct" in args):
                    zombies.append(int(pid_str))
                    parents.add(int(ppid_str))
        for ppid in parents:
            if assistant_held_stopped():
                log(f"Zombie (parent PID {ppid}) — flag STOP présent, pas de redémarrage (arrêt volontaire)")
                continue
            log(f"Zombie détecté (parent PID {ppid}) — redémarrage assistant_stream pour éliminer zombie")
            subprocess.run(["sudo", "systemctl", "restart", "pidog-stream.service"], capture_output=True)
            log(f"Redémarrage assistant_stream lancé pour nettoyer parent {ppid}")
        # Tenter kill -9 sur le zombie directement (sans effet mais pour trace)
        for z in zombies:
            try:
                subprocess.run(["kill", "-9", str(z)], capture_output=True)
                log(f"Tentative kill -9 zombie {z} (déjà mort, parent doit récupérer)")
            except Exception as inner:
                log(f"Impossible de toucher zombie {z} : {inner}")
    except Exception as e:
        log(f"Erreur check_zombies : {e}")

def check_voice_assistant():
    # Arrêt volontaire (flag .assistant_stopped posé par soundsign.stop_service)
    # → le guardian respecte l'arrêt et ne relance ni ne stoppe le service.
    if assistant_held_stopped():
        log("Assistant STOPPÉ volontairement — le guardian respecte l'arrêt.")
        return
    # Check if a valid audio source (JBL or alsa mic) is available
    ok, sources = run_cmd("pactl list short sources 2>/dev/null")
    has_mic = ok and len(sources.strip()) > 0

    # Check if pidog-stream service is active
    stream_active, _ = run_cmd("systemctl is-active --quiet pidog-stream.service")

    if has_mic and not stream_active:
        check_and_clear_gpio()
        log("Audio source available et robot libre. Démarrage assistant...")
        run_cmd("sudo systemctl start pidog-stream.service")
    elif not has_mic and stream_active:
        log("No audio source available, but pidog-stream is running. Stopping to prevent looping...")
        run_cmd("sudo systemctl stop pidog-stream.service")


# --- OpenRouter / Free Models Router : correction automatique si le problème persiste ---
def try_openrouter_fix(context_text: str):
    # Un seul test de clé par processus : si elle est refusée / quota épuisé,
    # on n'essaie même plus (fini les 401 toutes les 15 min dans le log).
    if not openrouter_usable():
        return
    try:
        import urllib.request, json, subprocess, os
        # Endpoint libre / configurable (adaptation selon clé dispo)
        url = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
        key = os.environ.get("OPENROUTER_API_KEY", "")
        # Le .env peut contenir un commentaire en fin de ligne (ex: "...  # ou qwen")
        model = os.environ.get("OPENROUTER_MODEL", "openrouter/free").split("#")[0].strip()
        retries = 4
        timeout = 8  # Free budget, petit temps
        payload = json.dumps({
            "model": model,  # Free Models Router par défaut
            "messages": [{"role":"system","content":"Tu es un administrateur système Raspberry Pi. Réponds UNIQUEMENT avec la commande bash à exécuter (pas d'explication)."},
                          {"role":"user","content":context_text}]
        }).encode()
        last_err = None
        # Boucle : max 5 retries (free tier quota), 5 sec entre
        for attempt in range(1, 6):
            try:
                log(f"OpenRouter itération #{attempt}/5 (timeout {timeout}s) — problème persistant")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type":"application/json","Authorization":f"Bearer {key}"}, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read())
                    reply = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if reply:
                        log(f"OpenRouter commande #{attempt} reçue : {reply[:200]}")
                        if "systemctl" in reply or "service" in reply or "ip link" in reply or "bluetooth" in reply or "pulse" in reply:
                            subprocess.run(f"sudo {reply}" if "sudo" not in reply else reply, shell=True, timeout=10, capture_output=True)
                            log("Commande OpenRouter exécutée (timeout 10s)")
                    else:
                        log("OpenRouter réponse vide — arrêt itération")
                        break
                    # Vérifier si le problème principal est résolu (ex: SSH OK, BT OK)
                    ssh_fixed = run_cmd('systemctl is-active --quiet ssh')[0]
                    bt_fixed = run_cmd('systemctl is-active --quiet bluetooth')[0]
                    if ssh_fixed and bt_fixed:
                        log("Problème résolu (SSH + BT OK) — arrêt itération OpenRouter")
                        break
            except Exception as e:
                last_err = e
                log(f"OpenRouter #{attempt}/5 échoué ({e}) — retry dans 4,5 s")
                time.sleep(4.5)   # retry façon OpenCode : 4,5 s entre chaque essai
        else:
            log(f"OpenRouter non disponible après 5 tentatives : {last_err}")
    except Exception as e:
        log(f"OpenRouter non disponible ou erreur : {e}")


def check_cam_stream():
    ok = False
    try:
        ok = bool(subprocess.run(["pgrep", "-f", "camstream/server.py"], capture_output=True).returncode == 0)
        if ok:
            port_ok = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True).stdout.find(":8089") != -1
            ok = port_ok
    except Exception:
        pass
    return ok


# --- VÉRIF COMPOSANTS MATÉRIELS (alim, SD, mémoire, température) ---

# --- VÉRIF CRON / REDÉMARRAGE AUTOMATIQUE DU HEALTH ---
def check_cron_health():
    try:
        cron_active = subprocess.run(["systemctl", "is-active", "--quiet", "cron"], capture_output=True).returncode == 0
        cron_file = os.path.exists("/etc/cron.d/pidog-health")
        if not cron_active:
            log("ALERTE : service cron non actif — redémarrage")
            subprocess.run(["sudo", "systemctl", "start", "cron"], capture_output=True)
        if not cron_file:
            arret = "/etc/cron.d/pidog-health.arret"
            if os.path.exists(arret):
                log("Fichier cron en pause (.arret) détecté — restauration")
                subprocess.run(["sudo", "mv", arret, "/etc/cron.d/pidog-health"],
                               capture_output=True, timeout=10)
            else:
                # Le guardian tourne en mdelsinne : impossible d'écrire dans /etc
                log("ALERTE : fichier /etc/cron.d/pidog-health manquant — recréation (sudo tee)")
                content = ("0 * * * * root /bin/systemctl restart pidog-health.service "
                           "2>/dev/null || /bin/systemctl start pidog-health.service\n")
                subprocess.run(["sudo", "tee", "/etc/cron.d/pidog-health"],
                               input=content, text=True, capture_output=True, timeout=10)
        # (branche OK silencieuse : pas de "Cron OK" toutes les 15 min dans le log)
    except Exception as e:
        log(f"Check cron ignoré : {e}")

def check_hardware():
    try:
        # SD / Disque
        df_res = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        for line in df_res.stdout.splitlines():
            if "/dev/" in line:
                parts = line.split()
                used = parts[4].replace("%", "")
                try:
                    if int(used) > 90:
                        log(f"ALERTE SD : disque {used}% plein")
                except ValueError:
                    pass
        # Tension : 'vcgencmd measure_volts' renvoie la tension du CORE
        # (0.6-1.3 V) sur les firmwares récents → tester "< 4.65 V" donnait
        # une fausse alerte permanente. On utilise get_throttled (vrai critère
        # de sous-tension, bits 0x1 = actuelle / 0x10000 = historique).
        thr = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True)
        if "throttled=" in thr.stdout:
            val = int(thr.stdout.split("=")[1].strip(), 16)
            if val & 0x1:
                log(f"ALERTE SOUS-TENSION : get_throttled=0x{val:X} (sous-tension en cours)")
            elif val & 0x10000:
                log(f"ALERTE SOUS-TENSION : get_throttled=0x{val:X} (sous-tension historique au dernier boot)")
        # Température CPU
        temp_res = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
        if "temp=" in temp_res.stdout:
            t = temp_res.stdout.split("=")[1].replace("'C", "").strip()
            try:
                if float(t) > 80:
                    log(f"ALERTE TEMPÉRATURE : CPU {t}°C")
            except ValueError:
                pass
        # Mémoire
        mem_res = subprocess.run(["free", "-m"], capture_output=True, text=True)
        for line in mem_res.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                avail = int(parts[6])
                if avail < 100:
                    log(f"ALERTE MÉMOIRE : {avail}MB disponibles")
                break
    except Exception as e:
        log(f"Check hardware ignoré (pas d'accès composant) : {e}")

def check_output_bus():
    """Vérifie que le bus HDMI / périphérique de sortie est bien présent (évite reboot après branchement)."""
    try:
        # Vérifie présence du node DRM HDMI
        res = subprocess.run(["ls", "/sys/class/drm/"], capture_output=True, text=True)
        hdmi_nodes = [n for n in res.stdout.split() if "HDMI" in n]
        if hdmi_nodes:
            # Vérifie le statut du premier bus HDMI trouvé
            status_file = f"/sys/class/drm/{hdmi_nodes[0]}/status"
            if os.path.exists(status_file):
                status = open(status_file).read().strip()
                if status == "connected" or status == "disconnected":
                    log(f"HDMI bus OK : {hdmi_nodes[0]} → status={status}")
                    return True
            else:
                log(f"HDMI bus présent (node {hdmi_nodes[0]}) mais pas de fichier status")
                return True
        else:
            # Fallback : tvservice si dispo (Raspberry Pi)
            ok, out = run_cmd("tvservice -s 2>/dev/null || true")
            if ok and ("HDMI" in out or "State" in out):
                log(f"HDMI bus OK via tvservice : {out}")
                return True
            log("ALERTE HDMI : aucun bus HDMI détecté dans /sys/class/drm/ — vérifier câble / écran")
            return False
    except Exception as e:
        log(f"Check HDMI/bus sortie ignoré : {e}")
        return False

def reboot_policy(wifi_ok, ssh_ok):
    """Reboot en DERNIER recours (jamais le premier réflexe) :
    - exige WiFi ET SSH down (un micro muet ne justifie pas un reboot),
    - exige une réparation profonde déjà tentée (FAILURES >= 3),
    - max REBOOT_MAX_PER_DAY par jour, compteur persistant dans ~/.cache/pidog/
      (survit aux reboot ET au restart cron horaire → pas de boucle),
    - au-delà : plus aucun reboot ce jour-là, backoff seul."""
    count = reboot_count_get()
    if count >= REBOOT_MAX_PER_DAY:
        log(f"Plafond reboot atteint ({count}/{REBOOT_MAX_PER_DAY}) — plus aucun reboot aujourd'hui, backoff seul")
        return
    if not DEEP_REPAIR_DONE:
        log("Reboot différé : réparation profonde pas encore tentée (FAILURES >= 3 requis)")
        return
    count += 1
    reboot_count_set(count)
    log(f"Tentative reboot #{count}/{REBOOT_MAX_PER_DAY} (WiFi={wifi_ok}, SSH={ssh_ok})")
    time.sleep(10)
    subprocess.run(["sudo", "reboot"], capture_output=True)


def main():
    log("PiDog Health Guardian started.")
    while True:
        # --- PRIORITÉ RÉSEAU : si pas de WiFi, pas de check SSH/Tailscale (logique) ---
        try:
            wifi_ok = 'inet' in run_cmd('ip -4 addr show wlan0')[1]
        except Exception:
            wifi_ok = False

        # Compteur d'échecs consécutifs (pilote le backoff + réparation profonde)
        global FAILURES
        ssh_ok = False   # évite un NameError si une exception survient avant son calcul
        try:
            # Vérif ALSA / carte intégrée morte (évite erreurs de log / underrun)
            ok, out = run_cmd("aplay -l 2>/dev/null | grep -i 'platform' || true")
            if ok and out: log("ALSA: carte intégrée détectée (morte) — OK car dtparam=audio=off")
            check_and_clear_gpio()
            check_and_kill_zombies()
            # PRIORITÉ RÉSEAU : d'abord récupérer WiFi/Ethernet
            check_network()
            wifi_ok = 'inet' in run_cmd('ip -4 addr show wlan0')[1]
            
            # Résoudre SSH seulement si connexion existante (local ou web)
            if wifi_ok:
                check_ssh_tailscale()
                ssh_ok = run_cmd('systemctl is-active --quiet ssh')[0]
                if not ssh_ok:
                    log("SSH KO malgré connexion — redémarrage propre tenté")
                    # Si toujours KO après restart : OpenRouter si internet, sinon LLM local
                    internet_ok, _ = run_cmd("ping -c 1 -W 2 8.8.8.8 2>/dev/null")
                    if internet_ok:
                        log("Connexion web OK — appel OpenRouter pour SSH")
                        try_openrouter_fix("Problème SSH persistant après restart propre (systemctl restart ssh). Réponds UNIQUEMENT avec la commande bash pour réparer SSH sur Raspberry Pi, sans casser WiFi.")
                    else:
                        log("Connexion locale seulement — essai LLM local pour SSH")
                        try:
                            subprocess.run(["python3", "/home/mdelsinne/pidog_local_agent.py"], timeout=30, capture_output=True)
                        except Exception as e:
                            log(f"LLM local erreur : {e}")
            else:
                log("Pas de connexion (WiFi/Ethernet) — pas de résolution SSH ; focus récupération réseau")
                # Après persistance 24h (FAILURES >= 12 → backoff 24h) : tenter LLM local
                if FAILURES >= 12:
                    log("Échec réseau persistant 24h+ — lancement LLM local pour réparation")
                    try:
                        subprocess.run(["python3", "/home/mdelsinne/pidog_local_agent.py"], timeout=30, capture_output=True)
                        log("LLM local lancé après 24h sans connexion")
                    except Exception as e:
                        log(f"LLM local erreur (pas de connexion) : {e}")
            # --- Vérifs PRÉSENTES dans le fichier mais JAMAIS appelées avant ---
            check_cron_health()   # auto-réparation du cron horaire
            check_hardware()      # SD plein / tension basse / temp / mémoire

            check_audio_bluetooth()
            check_output_bus()
            check_voice_assistant()

            # --- ÉTAT RÉSEAU FINAL DE CE CYCLE ---
            wifi_ok = 'inet' in run_cmd('ip -4 addr show wlan0')[1]
            ssh_ok = run_cmd('systemctl is-active --quiet ssh')[0]

            # ============ ÉCHELLE DE SURVIE ============================
            # 1) réseau/SSH down     → FAILURES++
            # 2) FAILURES 3/7/12     → RÉPARATION PROFONDE (healthcheck --guardian)
            # 3) reboot = DERNIER    → WiFi ET SSH down, APRÈS réparation,
            #    recours                max 3/jour (compteur persistant)
            # 4) au-delà             → backoff seul (1h / 6h / 24h)
            # ===========================================================
            if (not wifi_ok) or (not ssh_ok):
                FAILURES += 1
                log(f"Réseau ou SSH down (WiFi={wifi_ok}, SSH={ssh_ok}) — FAILURES={FAILURES}")
                if FAILURES in (3, 7, 12):
                    run_deep_repair(f"FAILURES={FAILURES} (WiFi={wifi_ok}, SSH={ssh_ok})")
                if (not wifi_ok) and (not ssh_ok):
                    reboot_policy(wifi_ok, ssh_ok)
            elif FAILURES > 0:
                log("WiFi + SSH OK — reset cycle normal (FAILURES=0 et quota reboot remis à zéro)")
                FAILURES = 0
                reboot_count_set(0)
        except Exception as e:
            FAILURES += 1
            log(f"Error in health check loop: {e} (FAILURES={FAILURES})")
            if FAILURES in (3, 7, 12):
                run_deep_repair(f"exception FAILURES={FAILURES} : {e}")
        
        # --- PROGRESSIVE BACKOFF SI PERSISTANT DOWN ---
        # Normal : 5 min. Si >=5 échecs (reboot phase) : 1h x3, 6h x8, 24h
        if FAILURES >= 5:
            backoff_count = min(FAILURES - 4, 11)  # 1..11
            if backoff_count <= 3:
                sleep_time = 3600  # 1h
            elif backoff_count <= 11:
                sleep_time = 21600  # 6h (x8 fois après 3x 1h)
            else:
                sleep_time = 86400  # 24h
            log(f"Mode backoff (FAILURES={FAILURES}) : sleep {sleep_time}s")
        else:
            sleep_time = 900  # 15 min normal
        # (Le reset de FAILURES + du quota reboot est fait plus haut, UNIQUEMENT
        #  quand un cycle se termine sans exception et avec WiFi+SSH OK.)

        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
