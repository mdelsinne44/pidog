#!/usr/bin/env python3
"""PiDog — agent vocal STREAMING (wake word « PiDog »).

Chaîne temps réel :
    parec (micro JBL, 16 kHz mono, flux permanent)
      -> Vosk wake-word (grammaire « pidog ») en veille
      -> Vosk streaming, fin de phrase par endpointing (silence)
      -> routage : commande locale  |  LLM Groq (stream=True)
      -> TTS Piper phrase-par-phrase (synthèse chevauchée à la lecture)
      -> aplay sur la JBL

Design :
  * 100 % asyncio : mic, LLM et TTS ne se bloquent jamais mutuellement.
  * Pendant la playback (`SPEAKING`), le micro reste consommé mais ignoré
    (la JBL est à la fois haut-parleur et micro en HFP → anti-écho).
    Barge-in (couper la parole en cours) : non implémenté — prochaine étape.
  * Un seul modèle Vosk light chargé (~66 Mo), partagé par les deux
    recognizers (wake + commande) : léger et réactif sur Pi4.
  * Robustesse Bluetooth : parec relancé automatiquement si le flux meurt.

Usage :  python3 pidog_stream.py
"""
import sys
sys.path.append("/home/mdelsinne")  # robot_guardian.py (hors repo)
from robot_guardian import RobotGuardian
guardian = RobotGuardian()

import asyncio
import json
import os
import re
import subprocess
import time
from collections import deque
from typing import Optional

import env_vision
from memory_load import load_memory
import memory

memory.memory_init()

import requests
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)  # Vosk silencieux (on a nos propres logs)

BASE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(BASE), ".env"))  # racine de l'extension
    load_dotenv(os.path.join(BASE, ".env"))  # éventuel .local dans voice/
except Exception:
    pass

import atexit
import signal


def _reset_pidog_on_exit():
    # Coupe le micro (parec) et réinitialise le chien
    try:
        subprocess.run(["killall", "-SIGTERM", "parec"], capture_output=True)
        subprocess.run(["killall", "-SIGTERM", "aplay"], capture_output=True)
    except Exception:
        pass
    try:
        from pidog_core import get_pidog
        dog = get_pidog()
        dog.do_action("lie")
        dog.head_move([[0, 0, 0]], immediately=True, speed=3)
        try:
            dog.stable()
        except Exception:
            pass
        dog.rgb_strip.set_mode(style="listen", color="black", brightness=0)
        print("[atexit] PiDog réinitialisé couché. Mic coupé.", flush=True)
    except Exception as e:
        pass

atexit.register(_reset_pidog_on_exit)

# ------------------------------------------------------------------ FIX GPIO BUSY

def fix_gpio_busy():
    """Tue les processus audio bloquants et les instances concurrentes.

    NB : on ne libère PAS le singleton Pidog ici. Dans un process frais il
    n'existe pas, et release_pidog() fermait le pin factory gpiozero via
    mcu.deinit() — ce qui rendait toute construction Pidog() impossible dans
    ce process : le chien restait sans posture init (couché/tête droite)
    à chaque démarrage du service.
    """
    for proc in ("parec", "aplay", "mpv", "piper"):
        subprocess.run(["killall", "-SIGTERM", proc], capture_output=True)
    try:
        my_pid = os.getpid()
        result = subprocess.run(["pgrep", "-f", "pidog_stream.py"],
                              capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if line.strip() and int(line.strip()) != my_pid:
                try:
                    subprocess.run(["kill", "-SIGTERM", line.strip()],
                                  capture_output=True, timeout=2)
                except Exception:
                    pass
    except Exception:
        pass
    time.sleep(1.0)

fix_gpio_busy()

# ------------------------------------------------------------------ config --
GROQ_KEY = os.getenv("GROQ_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")

PIPER_BIN = os.getenv("PIPER_BIN", "/home/mdelsinne/.local/bin/piper")
VOICE = os.getenv("VOICE", os.path.join(BASE, "fr_FR-glados-medium.onnx"))
VOICE_JSON = VOICE + ".json"
if not os.path.exists(VOICE_JSON):
    VOICE_JSON = ""  # piper accepte de tourner sans .json de voix

# Modèle Vosk light (~66 Mo décompressé) : le plus rapide sur Pi4.
VOSK_MODEL_DIR = os.getenv(
    "VOSK_MODEL_DIR", os.path.join(BASE, "vosk-model-small-fr-0.22"))

# Audio JBL (HFP : la JBL est à la fois sortie et entrée)
JBL_ADDR = "04_FE_A1_8F_3C_A2"
JBL_SINK = f"bluez_sink.{JBL_ADDR}.handsfree_head_unit"
JBL_SOURCE = f"bluez_source.{JBL_ADDR}.handsfree_head_unit"

RATE = 8000  # JBL HFP (micro + sortie) en 8 kHz mono
CHUNK_BYTES = 2000          # 250 ms en s16le mono — plus réactif pour Vosk endpointing
WAKE_GRAMMAR = json.dumps(
    ["pi dog", "pi dogue", "pidog", "pi dogue"])

WAKE_RESET_SEC = 8.0        # reset périodique du recognizer de veille
LISTEN_TIMEOUT_SEC = 8.0    # max pour formuler une commande après réveil
CONVERSATION_TIMEOUT_SEC = 12.0  # temps pour enchaîner sans redire « PiDog »
CONVERSATION_SETTLE_SEC = 0.45   # laisse retomber l'écho du HP avant d'écouter
MAX_TTS_SENTENCES = 6       # garde-fou anti-réponse interminable
WATCHDOG_SEC = 30

# Vosk endpointing : silence requis pour valider une phrase (ms)
SILENCE_MS = 600            # silence requis avant de valider (ms)
SILENCE_BYTES = int(SILENCE_MS / 1000 * RATE * 2)  # 960 bytes en s16le mono
SILENCE_RMS_THRESHOLD = 400  # seuil RMS en dessous duquel on considère le son comme du silence

# Mots qui closent la discussion : on répond puis on rend la parole à la veille.
CONVERSATION_END_WORDS = (
    "au revoir", "aurevoir", "à plus", "a plus", "bonne nuit", "bonne journée",
    "bye", "ciao", "salut à toi", "c'est tout", "terminé", "termine",
)

SYSTEM_PROMPT = (
    "Tu es PiDog, un petit chien robot amical basé sur une Raspberry Pi 4. "
    "Tu connais ton créateur : il s'appelle Martin. "
    "Réponds en français, 1 à 2 phrases très courtes et naturelles (max 20 mots), jamais de listes. "
    "Tu peux aussi inclure un bloc d'action physique entre <<CODE>> et <<END_CODE>> :\n"
    "<<<CODE>>>\ndog = pidog_core.get_pidog()\ndog.do_action('stand')\nreboot()\n<<<END_CODE>>>\n"
    "Fonctions autorisées (dans <<<CODE>>>) : dog (get_pidog, do_action, head_move), retropie_start, retropie_stop, retropie_game_cmd, retropie_list_cmd, kodi_start, kodi_stop, kodi_tv_on, kodi_tv_off, kodi_video_cmd, kodi_pause, reboot, memory_add, memory_get, memory_check_topic, memory_all, memory_today. "
    "Tu PEUX et DOIS exécuter ces commandes directement dans <<<CODE>>>, même pour assis/debout/couché (!), Kodi, RetroPie, robot, Raspberry, mémoire. N'oublie pas la mémoire : si l'utilisateur te dit 'demain j'ai rdv coiffeur', tu notes avec memory_add('coiffeur','Demain j'ai rdv coiffeur') ET tu le répètes naturellement dans ta réponse (ex 'Bon, je note : demain rdv coiffeur'). Quand il te demande quelque chose sur un sujet mémorisé, relis-le avec memory_get('coiffeur') et réponds en le répétant. "
    "Toutes les commandes passent par toi (LLM) : assis → dog.do_action('sit'), debout → 'stand', couché → 'lie', Kodi→kodi_start, RetroPie→retropie_start, robot→dog.do_action, raspberry→reboot, mémoire→memory_add/get. "
    "Regle stricte : traite TOUJOURS la commande physique dans <<<CODE>>>, meme si cest juste assis. Ne dis jamais que tu ne peux pas.\n\n"
    "TU CONTRÔLES TON CORPS AVEC DES BALISES D'HUMEUR. "
    "Écris UNE SEULE balise, au tout début de ta réponse (jamais au milieu, "
    "jamais plusieurs). Balises autorisées : "
    "[JOIE] (content, blague, bonne nouvelle), [TRISTE] (l'utilisateur est "
    "triste, mauvaise nouvelle), [COLERE], [SURPRISE], [CURIEUX], "
    "[EXCITE], [FATIGUE], [CALME], [NEUTRE]. "
    "Exemple : si l'utilisateur est content, écris [JOIE] dans ta réponse. "
    "Exemple : « Pidog, je suis triste » -> « [TRISTE] Oh, qu'est-ce qui se "
    "passe ? ». Les balises sont des ordres pour ton corps : ne les prononce "
    "jamais, ne les explique jamais, ne mets pas d'espace avant.\n\n"
    "MODE DISCUSSION : après « PiDog », l'utilisateur peut enchaîner plusieurs "
    "répliques sans redire le mot de réveil. Tiens compte du contexte des "
    "échanges précédents et reste naturel ; tu peux poser une petite question "
    "pour relancer la conversation quand c'est pertinent. Si l'utilisateur "
    "prend congé, réponds brièvement et termine.\n\n"
    "VISION ENVIRONNEMENT : tu as accès à l'état local (Bluetooth Flip 4, "
    "audio PulseAudio, réseau IP, robot PiDog). Quand tu réponds, mentionne brièvement "
    "l'état pertinent si ça aide (ex. « Flip 4 est connectée » ou « robot présent »)."
)

# ----------------------------------------------------------------- globals --
# Références fortes aux tâches de fond : sinon le GC d'asyncio peut les
# collecter avant qu'elles n'aient tourné (create_task seul ne suffit pas).
BG_TASKS: "set[asyncio.Task]" = set()


def spawn(coro) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


SPEAKING = False            # True pendant la playback (micro ignoré, anti-écho)
pending_audio = 0           # phrases déjà synthétisées/en file pour le speaker

# Humeur courante : pilote la prosodie TTS, la LED au repos et le contexte LLM.
CURRENT_EMOTION = "NEUTRE"
EMOTION_STYLE = {
    # émotion : (length_scale Piper, couleur LED, luminosité repos)
    "JOIE":     (0.94, "green",   0.35),
    "EXCITE":   (0.90, "yellow",  0.45),
    "TRISTE":   (1.14, "blue",    0.25),
    "COLERE":   (0.92, "red",     0.40),
    "SURPRISE": (0.96, "white",   0.40),
    "CURIEUX":  (1.02, "cyan",    0.35),
    "FATIGUE":  (1.18, "blue",    0.18),
    "CALME":    (1.06, "green",   0.20),
    "NEUTRE":     (0.90, "black",   0.0),
}

tts_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=20)
audio_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=20)
history = deque(maxlen=16)                              # contexte de discussion

last_activity = time.time()

PIPELINE: "Optional[MicPipeline]" = None                # pour rouvrir le micro

# ------------------------------------------------------- PiDog (lazy + safe) --
d = None  # lazy : initialisé au premier usage via get_pidog()

def _lazy_d():
    global d
    if d is None:
        try:
            import socket, json
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect("/tmp/pidog.sock")
            payload = (json.dumps({"cmd":"status"}) + "\n").encode()
            s.sendall(len(payload).to_bytes(4, "big") + payload)
            s.recv(4)
            s.close()
            from pidog.service import my_dog
            d = my_dog()
            print("PiDog connecté via service (lazy).", flush=True)
        except Exception:
            try:
                from pidog_core import get_pidog
                d = get_pidog()
                print("PiDog connecté (lazy, fallback).", flush=True)
            except Exception as e2:
                d = None
                print(f"PiDog absent — mode voix seul.", flush=True)
    return d


def led(color: str, brightness: int = 1):
    dog = _lazy_d()
    if dog is None:
        return
    try:
        dog.rgb_strip.set_mode(style="listen", color=color, brightness=brightness)
    except Exception:
        pass


def dog_action(action: str):
    """Blocant (I2C) : toujours appeler via asyncio.to_thread()."""
    dog = _lazy_d()
    if dog is None:
        return
    try:
        dog.do_action(action)
    except Exception as e:
        print(f"dog_action({action}): {e}", flush=True)


def dog_head(yaw: float = 0, pitch: float = 0):
    dog = _lazy_d()
    if dog is None:
        return
    try:
        dog.head_move([[yaw, pitch, 0]], immediately=True, speed=60)
    except Exception:
        pass


def dog_sound(name: str = "r2d2_confused_2"):
    dog = _lazy_d()
    if dog is None:
        return
    try:
        dog.music.sound_play(name)
    except Exception:
        pass


# ---------------------------------------------------------- émotions / corps --
# Groq écrit des balises [JOIE], [TRISTE]… dans le flux. On les retire du texte
# (jamais prononcées) et on les traduit ici en mouvements du PiDog.
EMOTION_TAGS = (
    "JOIE", "TRISTE", "COLERE", "SURPRISE", "CURIEUX",
    "EXCITE", "FATIGUE", "CALME", "NEUTRE",
)

# Variantes / accents / anglais que le LLM peut produire malgré la consigne.
EMOTION_ALIASES = {
    "JOIE": "JOIE", "JOY": "JOIE", "HAPPY": "JOIE", "CONTENT": "JOIE",
    "TRISTE": "TRISTE", "TRISTESSE": "TRISTE", "SAD": "TRISTE",
    "COLERE": "COLERE", "COLÈRE": "COLERE", "ANGRY": "COLERE",
    "SURPRISE": "SURPRISE", "SURPRIS": "SURPRISE", "SURPRISED": "SURPRISE",
    "CURIEUX": "CURIEUX", "CURIEUSE": "CURIEUX", "CURIOUS": "CURIEUX",
    "EXCITE": "EXCITE", "EXCITÉ": "EXCITE", "EXCITED": "EXCITE",
    "FATIGUE": "FATIGUE", "FATIGUÉ": "FATIGUE", "TIRED": "FATIGUE",
    "CALME": "CALME", "CALM": "CALME",
    "NEUTRE": "NEUTRE", "NEUTRAL": "NEUTRE",
}

# Balise complète : [JOIE], [ joie ], [SURPRISE]…
EMOTION_TAG_RE = re.compile(r"\[\s*([^\]]{1,30}?)\s*\]")
# Balise encore incomplète en fin de flux : on la garde en attente (elle ne
# doit surtout pas partir au TTS).
OPEN_TAG_RE = re.compile(r"\[[^\[\]]{0,30}$")

emotion_lock = asyncio.Lock()   # une animation à la fois (I2C non réentrant)


def _norm_emotion(raw: str) -> Optional[str]:
    key = raw.strip().upper().replace("-", " ")
    key = re.sub(r"\s+", " ", key)
    return EMOTION_ALIASES.get(key)


def split_emotion_tags(buf: str):
    """Retire les balises du texte parlé et retourne les émotions trouvées.

    Retourne (texte_nettoyé, [émotions], queue_de_balise_incomplète).
    """
    tail = ""
    m = OPEN_TAG_RE.search(buf)
    if m:
        tail = buf[m.start():]
        buf = buf[:m.start()]

    emotions: list[str] = []

    def _sub(mo):
        name = _norm_emotion(mo.group(1))
        if name:
            emotions.append(name)
        # On retire TOUTE balise entre crochets : le TTS ne doit jamais lire
        # « crochet JOIE crochet », même si le modèle invente un nom.
        return ""

    return EMOTION_TAG_RE.sub(_sub, buf), emotions, tail


def _emotion_moves(name: str):
    """Séquence de mouvements (bloquant : appelé dans un thread)."""
    dog = _lazy_d()
    if dog is None:
        return

    def head(yaw=0, roll=0, pitch=0, speed=35, immediately=True):
        dog.head_move([[yaw, roll, pitch]], immediately=immediately, speed=speed)

    def tail(angle, speed=45):
        dog.tail_move([[angle]], immediately=True, speed=speed)

    def wag(times=3, amplitude=35, delay=0.16):
        for i in range(times):
            tail(amplitude)
            time.sleep(delay)
            tail(-amplitude)
            time.sleep(delay)
        tail(0, speed=30)

    def sound(n):
        try:
            dog.music.sound_play(n)
        except Exception:
            pass

    try:
        if name == "JOIE":
            led("green")
            dog.do_action("stand", speed=35)
            head(pitch=18, speed=45)
            wag(3, 38)
            sound("r2d2_happy_1")
        elif name == "EXCITE":
            led("yellow")
            dog.do_action("stand", speed=60)
            head(yaw=15, pitch=22, speed=60)
            wag(5, 45, 0.11)
            head(yaw=-15, pitch=22, speed=60)
            sound("r2d2_beep_2")
        elif name == "TRISTE":
            led("blue")
            dog.do_action("lie", speed=25)
            head(pitch=-25, speed=18)
            tail(-25, speed=18)
            time.sleep(0.4)
            sound("r2d2_sad_1")
        elif name == "COLERE":
            led("red")
            dog.do_action("stand", speed=50)
            head(pitch=-20, speed=50)
            tail(-35, speed=60)
            sound("growl_1")
        elif name == "SURPRISE":
            led("white")
            head(pitch=28, speed=70)
            tail(30, speed=70)
            sound("r2d2_panic_1")
            time.sleep(0.45)
            head(pitch=10, speed=40)
        elif name == "CURIEUX":
            led("cyan")
            head(roll=22, pitch=12, speed=30)
            time.sleep(0.35)
            head(roll=-18, pitch=12, speed=30)
            sound("r2d2_question_1")
        elif name == "FATIGUE":
            led("blue", 0.4)
            dog.do_action("lie", speed=18)
            head(pitch=-20, speed=12)
            tail(-20, speed=12)
            time.sleep(0.3)
            sound("pant")
        elif name == "CALME":
            led("green", 0.5)
            head(pitch=0, speed=15)
            tail(-10, speed=15)
        else:  # NEUTRE
            led("black", 0)
            head(pitch=0, speed=20)
            tail(0, speed=20)
    except Exception as e:
        print(f"Émotion {name}: {e}", flush=True)


async def apply_emotion(name: str):
    """Applique une émotion sans jamais bloquer la boucle asyncio."""
    async with emotion_lock:
        await asyncio.to_thread(_emotion_moves, name)


def queue_emotion(name: str):
    """Appelé depuis le thread Groq via call_soon_threadsafe."""
    trigger_emotion(name)


# Réflexe local : on réagit à l'émotion de l'utilisateur AVANT la réponse Groq
# (masque la latence réseau). Le tag du LLM confirmera ensuite l'humeur.
USER_EMOTION_HINTS = (
    ("TRISTE", ("triste", "tristesse", "déprimé", "deprime", "malheureux",
                "pleure", "chagrin", "morose")),
    ("JOIE", ("content", "contente", "heureux", "heureuse", "joie", "génial",
              "genial", "super", "bravo", "diplôme", "diplome", "réussi")),
    ("COLERE", ("colère", "colere", "énervé", "enervé", "furieux", "marre",
                "ras-le-bol")),
    ("FATIGUE", ("fatigué", "fatigue", "épuisé", "epuise", "dors", "sommeil")),
    ("EXCITE", ("excité", "excite", "impatient", "vite", "incroyable")),
    ("SURPRISE", ("surpris", "surprise", "incroyable", "oh là")),
    ("CURIEUX", ("curieux", "curieuse", "pourquoi", "comment ça marche")),
)

last_emotion_at = 0.0


def detect_user_emotion(text: str) -> Optional[str]:
    t = text.lower()
    for emo, words in USER_EMOTION_HINTS:
        if any(w in t for w in words):
            return emo
    return None


def trigger_emotion(name: str):
    """Met à jour l'humeur et lance l'animation (dédupliquée sur 4 s)."""
    global CURRENT_EMOTION, last_emotion_at
    now = time.time()
    if name == CURRENT_EMOTION and now - last_emotion_at < 4.0:
        print(f"  [HUMEUR] {name} (déjà en cours)", flush=True)
        return
    CURRENT_EMOTION = name
    last_emotion_at = now
    print(f"  [HUMEUR] {name}", flush=True)
    spawn(apply_emotion(name))


def mood_led():
    """LED discrète qui rappelle l'humeur quand le chien ne parle pas."""
    _, color, brightness = EMOTION_STYLE.get(CURRENT_EMOTION,
                                             EMOTION_STYLE["NEUTRE"])
    led(color, brightness)


def speak_led():
    """LED pendant la parole : couleur de l'humeur, un peu plus vive."""
    _, color, _ = EMOTION_STYLE.get(CURRENT_EMOTION, EMOTION_STYLE["NEUTRE"])
    led(color if color != "black" else "blue", 0.8)


# ------------------------------------------------------------ audio route --
def hard_reset_audio():
    """Tuer tout le monde (parec/aplay/mpv/piper) + forcer JBL + couper le sink intégré cassé."""
    for proc in ("parec", "aplay", "mpv", "piper"):
        subprocess.run(["killall", "-SIGTERM", proc], capture_output=True)
    time.sleep(0.3)
    try:
        subprocess.run(["pactl", "set-default-sink", JBL_SINK], capture_output=True, timeout=3)
        # Réveiller la source HFP (SUSPENDED au démarrage après reconnect BT)
        try:
            subprocess.run(["pactl", "set-source-state", JBL_SOURCE, "RUNNING"],
                           capture_output=True, timeout=3)
        except Exception:
            pass
        subprocess.run(["pactl", "set-default-source", JBL_SOURCE], capture_output=True, timeout=3)
        cards = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True, timeout=3).stdout
        for line in cards.splitlines():
            if "analog-output" in line or "hdmi-output" in line or "jack-output" in line:
                sink = line.split()[0]
                subprocess.run(["pactl", "set-sink-mute", sink, "1"],
                               capture_output=True, timeout=2)
    except Exception as e:
        print("Audio reset:", e, flush=True)
    time.sleep(0.5)


def ensure_bt_profiles() -> None:
    """Profils BT imposés : Flip4 → HFP (mic), toute autre carte BT → A2DP.

    Idempotent : appelé au démarrage puis à chaque retry de la source, car
    PulseAudio peut basculer la carte en a2dp_sink au reconnexion BT.
    """
    try:
        out = subprocess.run(["pactl", "list", "short", "cards"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return
    jbl = f"bluez_card.{JBL_ADDR}"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith("bluez_card."):
            continue
        want = "handsfree_head_unit" if parts[1] == jbl else "a2dp_sink"
        try:
            subprocess.run(["pactl", "set-card-profile", parts[1], want],
                           capture_output=True, timeout=3)
        except Exception:
            pass


def setup_audio_route() -> str:
    """Force la JBL en HFP (mic + haut-parleur). Retourne la source retenue."""
    # Attendre que la carte BT apparaisse (reconnect après reboot)
    for _ in range(30):
        try:
            cards = subprocess.run(["pactl", "list", "short", "cards"],
                                   capture_output=True, text=True, timeout=5).stdout
            if f"bluez_card.{JBL_ADDR}" in cards:
                break
        except Exception:
            pass
        time.sleep(1)
    try:
        # Applique le profile même si la carte semble absente temporairement
        # (les profils BT : Flip4 → HFP, le reste → A2DP)
        ensure_bt_profiles()
        time.sleep(1.5)
        subprocess.run(["pactl", "set-default-sink", JBL_SINK],
                       capture_output=True)
        subprocess.run(["pactl", "set-default-source", JBL_SOURCE],
                       capture_output=True)
        # Vérifie après coup que la source est bien définie
        result = subprocess.run(["pactl", "get-default-source"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if result == JBL_SOURCE:
            print("Audio : JBL en HFP (mic + sortie).", flush=True)
            return JBL_SOURCE
        print(f"JBL absente ou source non définie ({result}) → source par défaut.", flush=True)
    except Exception as e:
        print("Audio routing:", e, flush=True)
    try:
        return subprocess.run(["pactl", "get-default-source"],
                              capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


async def wait_for_source(source: str, timeout: float = 30.0) -> bool:
    """Attend que la source PulseAudio existe et soit RUNNING."""
    for _ in range(int(timeout / 1)):
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True, text=True, timeout=2)
            if source in result.stdout:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


def start_parec(source: str) -> asyncio.subprocess.Process:
    """parec en sous-process asynchrone : 16 kHz mono s16le sur stdout."""
    return subprocess.Popen(
        ["parec", f"--device={source}", "--format=s16le",
         f"--rate={RATE}", "--channels=1", "--raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------- TTS ---
def piper_synth(text: str, out_path: str, length_scale: float = 0.85):
    """Synthèse d'UNE phrase via le binaire Piper (bloquant → to_thread).

    `length_scale` module le débit : < 1 plus rapide/énergique, > 1 plus lent.
    --noise-scale / --noise-w réduisent la charge de calcul du générateur.
    """
    cmd = [PIPER_BIN, "--model", VOICE,
           "--length_scale", f"{length_scale:.3f}",
           "--noise-scale", "0.333",
           "--noise-w-scale", "0.333",
           "--sentence-silence", "0.2"]
    if VOICE_JSON:
        cmd += ["--config", VOICE_JSON]
    cmd += ["--output_file", out_path]
    p = subprocess.run(cmd, input=text, text=True,
                       capture_output=True, timeout=30)
    if p.returncode != 0:
        err = p.stderr or ""
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="ignore")
        print(f"Piper rc={p.returncode}: {err[-200:]}", flush=True)


def aplay_file(path: str):
    # Buffer généreux (-B 500 ms) : évite les underruns ALSA sur le sink BT HFP
    subprocess.run(["aplay", "-q", "-B", "500000", path],
                   capture_output=True, timeout=30)


async def tts_worker():
    """Phrase -> wav (Piper dans un thread) -> audio_queue.
    La synthèse de la phrase N+1 chevauche la lecture de la N."""
    global pending_audio
    n = 0
    while True:
        text = await tts_queue.get()
        pending_audio += 1              # dès réception : anti-trou d'écho
        n += 1
        out = f"/tmp/pidog_tts_{os.getpid()}_{n}.wav"
        try:
            length_scale = EMOTION_STYLE.get(
                CURRENT_EMOTION, EMOTION_STYLE["NEUTRE"])[0]
            await asyncio.to_thread(piper_synth, text, out, length_scale)
            if os.path.exists(out):
                await audio_queue.put(out)
            else:
                pending_audio -= 1
        except Exception as e:
            print("TTS:", e, flush=True)
            pending_audio -= 1


async def playback_worker():
    """wav -> aplay ; gère SPEAKING (anti-écho) et la couleur de la LED."""
    global SPEAKING, pending_audio
    while True:
        path = await audio_queue.get()
        SPEAKING = True
        speak_led()
        try:
            await asyncio.to_thread(aplay_file, path)
        except Exception as e:
            print("aplay:", e, flush=True)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            pending_audio -= 1
            # reste-t-il du son ? sinon on rouvre le micro
            SPEAKING = pending_audio > 0 or not tts_queue.empty()
            if not SPEAKING:
                mood_led()


async def say(text: str):
    """Convenience : une seule phrase dans le TTS."""
    await tts_queue.put(text)
    print(f"  [TTS] {text}", flush=True)


# -------------------------------------------------------------------- LLM --
WEATHER_FR = {
    0: "ciel dégagé", 1: "peu nuageux", 2: "partiellement nuageux",
    3: "nuageux", 45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine dense",
    61: "pluie faible", 63: "pluie", 65: "pluie forte",
    71: "neige faible", 73: "neige", 75: "neige forte",
    80: "averses faibles", 81: "averses", 82: "averses fortes",
    95: "orage", 96: "orage avec grêle", 99: "orage violent",
}


async def weather_reply(text: str) -> str:
    """Météo open-meteo (ville détectée dans la phrase, sinon Paris)."""
    # Ville = ce qui suit « à » (ou « a », « d' ») — PAS « de » : le STT
    # produit souvent des phrases tordues où « de X » donnait des villes farfelues.
    m = re.search(
        r"(?:météo|meteo|temps|weather).{0,25}?(?:\bà\b|\ba\b|d')\s+"
        r"([A-Za-zÀ-ÿ'\- ]{2,30})", text, re.I)
    city = (m.group(1).strip().rstrip(".!?") if m else "Nantes")

    def fetch():
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "fr"},
            timeout=6).json()
        r0 = geo["results"][0]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": r0["latitude"], "longitude": r0["longitude"],
                    "current": "temperature_2m,weather_code,wind_speed_10m"},
            timeout=6).json()["current"]
        return r0["name"], w

    try:
        name, w = await asyncio.to_thread(fetch)
        t = round(w["temperature_2m"])
        desc = WEATHER_FR.get(w.get("weather_code"), "temps variable")
        return f"Il fait {t}°C à {name}, {desc}."
    except Exception as e:
        print("Météo:", e, flush=True)
        return "La météo n'est pas disponible pour l'instant."


async def llm_stream_reply(user_text: str):
    """Groq en streaming : chaque phrase part immédiatement dans le TTS."""
    global last_activity
    if not GROQ_KEY:
        await say("Je n'ai pas de clé Groq configurée.")
        return
    loop = asyncio.get_running_loop()
    import datetime
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    # Charge la mémoire récente pour l'injecter au LLM
    mem_lines = memory.memory_get()
    mem_text = "\n".join([f"- {k} = {v}" for k, v, _ in mem_lines[:15]])
    memory_hint = (
        f"\n[JOUR ACTUEL : {today}, DEMAIN : {tomorrow}]\n"
        f"[MÉMOIRE GD018/GoogleDrive] : {mem_text}\n"
        "Si l'utilisateur te donne un renseignement (ex : demain rdv coiffeur), utilise memory_add('coiffeur', 'Demain j\'ai rdv coiffeur') puis répète-le dans ta réponse. "
        "Pour relire un sujet mémorisé (coiffeur, rdv, rendez-vous), appelle memory_get('coiffeur')."
    )
    history.append({"role": "user", "content": user_text})
    env_summary = env_vision.env_summary()
    env_text = "; ".join(f"{k}:{v}" for k,v in env_summary.items())
    mem = load_memory()
    mem_text = "; ".join(f"{k}={v}" for k,v in mem.items()) if mem else ""
    mood_hint = (
        f"\nContexte interne : ta dernière humeur affichée était "
        f"{CURRENT_EMOTION}. Choisis la balise adaptée au nouveau message.\n"
        f"[Environnement actuel] {env_text}\n"
        f"[Mémoire] {mem_text}"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT + mood_hint},
                *history]
    spoken: list[str] = []

    def _stream():
        buf, n = "", 0
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                json={"model": LLM_MODEL, "messages": messages,
                      "max_tokens": 110, "temperature": 0.3, "stream": True},
                stream=True, timeout=30)
            if r.status_code != 200:
                print(f"LLM HTTP {r.status_code}: {r.text[:150]}", flush=True)
                loop.call_soon_threadsafe(tts_queue.put_nowait,
                                          "Je n'ai pas compris.")
                return
            stream_start = time.time()
            for raw in r.iter_lines():
                if time.time() - stream_start > 30:
                    print("LLM stream timeout (30s) — coupure", flush=True)
                    break
                if not raw or not raw.startswith(b"data: "):
                    continue
                payload = raw[6:]
                if payload == b"[DONE]":
                    break
                try:
                    delta = (json.loads(payload)["choices"][0]["delta"]
                             .get("content") or "")
                except Exception:
                    continue
                buf += delta
                # 1) On extrait les balises d'humeur au vol : elles pilotent
                #    le corps et ne doivent jamais être prononcées.
                buf, emotions, tag_tail = split_emotion_tags(buf)
                buf += tag_tail
                for emo in emotions:
                    loop.call_soon_threadsafe(queue_emotion, emo)
                # 2) Dès qu'une phrase complète est là, elle part au TTS
                while True:
                    # Détecte les fins de phrase : . ! ? … (trois points) + éventuellement des espaces
                    m = re.search(r"([.!?…]{1,3})\s*", buf)
                    if not m:
                        break
                    sentence = buf[:m.end()].strip()
                    buf = buf[m.end():]
                    if sentence:
                        spoken.append(sentence)
                        loop.call_soon_threadsafe(tts_queue.put_nowait, sentence)
                        n += 1
                    if n >= MAX_TTS_SENTENCES:
                        return
        except Exception as e:
            print("LLM:", e, flush=True)
            loop.call_soon_threadsafe(tts_queue.put_nowait,
                                      "Je n'ai pas compris.")
            return
        # Balise éventuellement coupée en fin de flux : on la supprime.
        rest = re.sub(r"\[[^\]]*$", "", buf)
        rest, emotions, _ = split_emotion_tags(rest)
        for emo in emotions:
            loop.call_soon_threadsafe(queue_emotion, emo)
        rest = rest.strip()
        if rest and n < MAX_TTS_SENTENCES:
            spoken.append(rest)
            loop.call_soon_threadsafe(tts_queue.put_nowait, rest)

    await asyncio.to_thread(_stream)
    full_text = " ".join(spoken)
    # --- Exécution du bloc d'action physique (stream) ---
    m = re.search(r"<<<CODE>>>(.*?)<<<END_CODE>>>", full_text, re.DOTALL)
    if m:
        try:
            import subprocess
            from pidog_core import get_pidog
            dog = get_pidog()
            def retropie_start(): subprocess.run(["systemctl", "start", "retropie"], capture_output=True)
            def retropie_stop(): subprocess.run(["systemctl", "stop", "retropie"], capture_output=True)
            def retropie_game_cmd(game=""): pass
            def retropie_list_cmd(): pass
            def kodi_start(): subprocess.run(["systemctl", "start", "kodi"], capture_output=True)
            def kodi_stop(): subprocess.run(["systemctl", "stop", "kodi"], capture_output=True)
            def kodi_tv_on(): pass
            def kodi_tv_off(): pass
            def kodi_video_cmd(): pass
            def kodi_pause(): pass
            def reboot(): subprocess.run(["sudo", "reboot"], capture_output=True, timeout=5)
            def stop_service():
                import subprocess, time, gc, os
                print("[CODE] stop_service appelé", flush=True)
                for p in ("parec","aplay","mpv","piper","python3"):
                    subprocess.run(["killall","-SIGTERM",p], capture_output=True)
                # Healthcheck interne purgé le 01/11/2026 (pidog-health /
                # moniteur-pidog supprimés) : plus rien à arrêter ici.
                time.sleep(0.5)
                try:
                    from pidog_core import release_pidog
                    release_pidog()
                except Exception:
                    pass
                try:
                    gc.collect()
                except Exception:
                    pass
            def shutdown(): subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True, timeout=5)
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
                return memory.memory_for_date(datetime.now().strftime("%Y-%m-%d"))
            env = {
                "__builtins__": None,
                "dog": dog,
                "get_pidog": get_pidog,
                "pidog_core": __import__("pidog_core"),
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
                "reboot": reboot,
                "stop_service": stop_service,
                "shutdown": shutdown,
                "memory_add": memory_add,
                "memory_get": memory_get,
                "memory_check_topic": memory_check_topic,
                "memory_all": memory_all,
                "memory_today": memory_today,
                "datetime": __import__("datetime"),
            }
            code = m.group(1).strip()
            for line in code.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                exec(line, env)
        except Exception as e:
            print("[ACTION_STREAM] Erreur robot:", e, flush=True)
    if spoken:
        history.append({"role": "agent",
                        "content": " ".join(spoken)[:300]})
    last_activity = time.time()


# Le TTS commence presque immédiatement, on attend la fin de la playback
async def wait_speech_then_followup():
    """Après une réponse : attend la fin de la parole puis rouvre le micro.

    C'est ce qui permet une vraie discussion : on peut enchaîner sans redire
    « PiDog ». Le pipeline refermera l'écoute après CONVERSATION_TIMEOUT_SEC.
    """
    # Attendre la fin de tout le TTS (synthèse + lecture) puis rouvrir le micro
    for _ in range(500):  # max 50 s
        if (not SPEAKING and pending_audio == 0
                and tts_queue.empty() and audio_queue.empty()):
            break
        await asyncio.sleep(0.1)
    if PIPELINE is not None:
        PIPELINE.begin_followup()


# ------------------------------------------------------------ commandes ----
async def handle_utterance(text: str, followup: bool = False):
    """Routage : stop > volume > radio > posture > météo > LLM."""
    global last_activity
    last_activity = time.time()
    t = text.lower()
    print(f"[ENTENDU] {text!r}" + ("  (discussion)" if followup else ""),
          flush=True)

    # STOP / silence
    if any(w in t for w in ("arrête", "arrete", "stop", "coupe",
                            "pause", "silence")):
        print("→ STOP", flush=True)
        try:
            subprocess.run(["pkill", "-f", "mpv"], capture_output=True)
        except Exception as e:
            print("Stop:", e, flush=True)
        led("green")
        await say("Musique arrêtée.")
        return

    # VOLUME
    if any(w in t for w in ("baisse", "moins", "un peu", "bas", "réduis")):
        print("→ VOLUME BAS", flush=True)
        try:
            subprocess.run(["pactl", "set-sink-volume", JBL_SINK, "30%"],
                           capture_output=True, timeout=5)
        except Exception as e:
            print("Vol-:", e, flush=True)
        led("green")
        await say("Volume baissé.")
        return
    if any(w in t for w in ("augmente", "monte", "fort", "haut")):
        print("→ VOLUME HAUT", flush=True)
        try:
            subprocess.run(["pactl", "set-sink-volume", JBL_SINK, "80%"],
                           capture_output=True, timeout=5)
            subprocess.run(["pactl", "set-sink-mute", JBL_SINK, "0"],
                           capture_output=True, timeout=5)
        except Exception as e:
            print("Vol+:", e, flush=True)
        led("green")
        await say("Volume monté.")
        return

    # RADIO (FIP / NOVA) — lancée pendant qu'on parle, pour la fluidité
    station = None
    if "fip" in t:
        station = ("FIP", "https://icecast.radiofrance.fr/fip-hifi.aac")
    elif "nova" in t:
        station = ("NOVA", "https://novazz.ice.infomaniak.ch/novazz-128.mp3")
    if station:
        print(f"→ RADIO {station[0]}", flush=True)
        try:
            subprocess.Popen(["mpv", "--no-video", "--volume=60", station[1]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("Radio:", e, flush=True)
        spawn(asyncio.to_thread(dog_action, "stand"))
        led("green")
        await say(f"{station[0]} en route.")
        return

    # POSTURE — tout passe au LLM via <<<CODE>>> pour intégrer mémoire+robot
    # Bloc supprimé : "assis / debout / couché" traité localement → LLM le fait.
    # RÉFLEXE ÉMOTIONNEL (avant même l'appel Groq, pour la réactivité)
    pre_emotion = detect_user_emotion(text)
    if pre_emotion:
        trigger_emotion(pre_emotion)

    # REBOOT / REDÉMARRE
    if any(w in t for w in ("reboot", "redémarre", "redemarre", "redémarre-toi",
                             "redemarre-toi", "reboot-toi", "reboot le robot")):
        print("→ REBOOT", flush=True)
        led("red")
        await say("Je redémarre. À tout de suite.")
        try:
            # Lance le redémarrage du système (nécessite sudo, mais le service
            # peut être configuré pour autoriser le reboot sans mdp)
            subprocess.run(["sudo", "reboot"], capture_output=True, timeout=10)
        except Exception as e:
            print("Reboot fail:", e, flush=True)
            # Si sudo échoue (mdp), on prévient l'utilisateur
            await say("Redémarrage demandé, mais autorisation refusée.")
        return

    # MÉTÉO
    if any(w in t for w in ("météo", "meteo", "temps", "weather")):
        print("→ MÉTÉO", flush=True)
        led("magenta")
        await say(await weather_reply(text))
        return

    # LLM (streaming) + enchaînement de la discussion
    print("→ LLM", flush=True)
    led("magenta")
    await llm_stream_reply(text)
    closing = any(w in t for w in CONVERSATION_END_WORDS)
    if closing:
        print("  [DISCUSSION] clôturée → veille (« PiDog »)", flush=True)
        return
    spawn(wait_speech_then_followup())


# ------------------------------------------------- pipeline micro / Vosk ----
class MicPipeline:
    """Veille (wake word) <-> Écoute (commande), sur le flux parec."""

    IDLE, LISTEN = 0, 1

    def __init__(self, source: str):
        self.source = source
        self.state = self.IDLE
        self.model = Model(VOSK_MODEL_DIR)
        print(f"Vosk chargé : {VOSK_MODEL_DIR}", flush=True)
        self.wake_rec = KaldiRecognizer(self.model, RATE)
        # Vocabulaire connu du modèle + variantes attendues
        self.wake_rec.SetWords(["pi", "dog", "dogue", "pi dog", "pi dogue"])
        self.wake_rec.SetGrammar(WAKE_GRAMMAR)
        # Grammaire contrainte sur vocabulaire présent : "pi dog" reconnu
        self.cmd_rec: Optional[KaldiRecognizer] = None
        self.chunks: list[str] = []
        self.listen_start = 0.0
        self.listen_timeout = LISTEN_TIMEOUT_SEC
        self.followup = False          # écoute enchaînée (sans wake word)
        self.followup_pending = False  # à rouvrir dès que le HP se tient
        self.followup_ready_at = 0.0
        self.last_partial_log = 0.0
        self.wake_reset_at = time.time()
        self._silence_start = None     # pour endpointing par silence
        self._has_speech = False      # indique si du son a été détecté

    # -- entrée audio (parec asynchrone, redémarré si le flux BT meurt) --
    async def frames(self):
        proc = None
        missing_count = 0
        while True:
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    print("parec mort → relance dans 2 s", flush=True)
                    await asyncio.sleep(2)
                # Attend que la source BT soit vraiment prête (évite la boucle
                # infinie au boot si la JBL n'est pas connectée)
                if not await wait_for_source(self.source, timeout=5):
                    missing_count += 1
                    # PulseAudio a peut-être rebasculé la Flip4 en A2DP au
                    # reconnexion BT → on réimpose Flip4=HFP / autres=A2DP.
                    ensure_bt_profiles()
                    print(f"Source BT pas prête ({missing_count}/5)…", flush=True)
                    if missing_count >= 5:
                        print("JBL / micro indisponible après plusieurs essais — arrêt propre du service agent (pas de boucle).", flush=True)
                        sys.exit(0)
                    await asyncio.sleep(3)
                    continue
                missing_count = 0
                try:
                    proc = start_parec(self.source)
                    print(f"parec lancé sur {self.source.split('.')[0]}", flush=True)
                except Exception as e:
                    print("parec:", e, flush=True)
                    await asyncio.sleep(2)
                    continue
            try:
                # IMPORTANT : lecture BLOQUANTE dans un thread, sinon la
                # boucle asyncio ne rend jamais la main aux autres tâches
                # (tts_worker, playback_worker, handle_utterance) — Pidog
                # transcrivait mais ne répondait jamais.
                data = await asyncio.to_thread(proc.stdout.read, CHUNK_BYTES)
                if data:
                    yield data
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                proc = None

    # -- veille : détection du wake word via grammaire --
    def idle_step(self, data: bytes):
        if time.time() - self.wake_reset_at > WAKE_RESET_SEC:
            self.wake_rec.Reset()
            self.wake_reset_at = time.time()
        # [DÉSACTIVÉ] Réveil sonore — désactivé pour éviter tremblements/head_toward
        # if d is not None:
        #     try:
        #         if d.ears.isdetected():
        #             direction = d.ears.read()
        #             print(f"★ RÉVEIL SON (dir={direction})", flush=True)
        #             self.wake_rec.Reset()
        #             self.enter_listen()
        #             return
        #     except Exception:
        #         pass
        # Réveil rapide aussi sur PartialResult (avant endpoint)
        partial_text = json.loads(self.wake_rec.PartialResult())
        partial = partial_text.get("partial", partial_text.get("text", "")).strip().lower()
        # Détecte "pi dog" / "pi dogue" même si reconnus séparément
        if ("pi" in partial and any(d in partial for d in ("dog", "dogue"))) or "pidog" in partial:
            print(f"★ RÉVEIL partiel ({partial})", flush=True)
            self.wake_rec.Reset()
            self.enter_listen()
            return
        if self.wake_rec.AcceptWaveform(data):
            word = json.loads(self.wake_rec.Result()).get("text", json.loads(self.wake_rec.Result()).get("partial", "")).strip().lower()
            # Détection du wake word dans le vocabulaire normal
            if ("pi" in word and any(d in word for d in ("dog", "dogue"))) or "pidog" in word:
                print(f"★ RÉVEIL ({word})", flush=True)
                self.wake_rec.Reset()
                self.enter_listen()
            else:
                self.wake_rec.Reset()

    def enter_listen(self, followup: bool = False):
        global last_activity
        last_activity = time.time()
        self.state = self.LISTEN
        self.cmd_rec = KaldiRecognizer(self.model, RATE)
        # Pas de SetEndpointing (non dispo dans cette version Vosk) — on gère
        # l'endpointing nous-mêmes par détection de silence RMS dans listen_step.
        self.chunks = []
        self.listen_start = time.time()
        self.last_partial_log = 0.0
        self.followup = followup
        self.listen_timeout = (CONVERSATION_TIMEOUT_SEC if followup
                               else LISTEN_TIMEOUT_SEC)
        self._silence_start = None   # pas encore de silence détecté
        self._has_speech = False    # réinitialiser le suivi du son
        led("cyan" if followup else "magenta")
        print(("→ discussion : à toi…" if followup
               else "→ écoute de la commande…"), flush=True)

    def begin_followup(self):
        """Demande la réouverture du micro après la réponse (mode discussion)."""
        if self.state == self.LISTEN:
            return
        self.followup_pending = True
        self.followup_ready_at = time.time() + CONVERSATION_SETTLE_SEC
        print("→ discussion : j'écoute (sans « PiDog »)", flush=True)

    # -- écoute : transcription streaming avec endpointing silence --
    def listen_step(self, data: bytes):
        now = time.time()
        # 1) RMS du chunk courant (détection de silence)
        rms = _rms(data)
        is_silent = rms < SILENCE_RMS_THRESHOLD

        # 3) Alimente le recognizer Vosk (une seule fois par chunk)
        has_result = self.cmd_rec.AcceptWaveform(data)
        if has_result:
            res = json.loads(self.cmd_rec.Result())
            text = res.get("text", "").strip()
            if text:
                self.chunks.append(text)
                self._has_speech = True
                if now - self.last_partial_log > 0.3:
                    print(f"  [STT] {text}", flush=True)
                    self.last_partial_log = now
        else:
            partial_res = json.loads(self.cmd_rec.PartialResult())
            partial = partial_res.get("partial", partial_res.get("text", "")).strip()
            self._has_speech = bool(partial) or self._has_speech
            if partial and now - self.last_partial_log > 0.3:
                print(f"  … {partial}", flush=True)
                self.last_partial_log = now

        # 4) Endpointing : si silence prolongé + texte partiel reconnu → valide
        if is_silent:
            if self._silence_start is None:
                self._silence_start = now
            silence_duration = now - self._silence_start
            # Prendre le partial actuel (texte reconnu mais pas encore validé)
            partial_now = json.loads(self.cmd_rec.PartialResult()).get("partial", "").strip()
            if (silence_duration >= SILENCE_MS / 1000.0
                    and self._has_speech
                    and partial_now):
                text = partial_now.strip()
                if text.strip().lower() == "ainsi":
                    text = "assis"
                elif text.strip().lower() == "attire":
                    text = "assis"
                if self.followup:
                    text = re.sub(
                        r"^\s*(?:ok(?:ay)?\s+)?(?:pi\s*dog(?:ue)?|pidog|pydog"
                        r"|piwog)\b[\s,.:!-]*", "", text, flags=re.I).strip()
                print(f"  [STT] {text}", flush=True)
                was_followup = self.followup
                self.exit_listen()
                spawn(handle_utterance(text, followup=was_followup))
                return
        else:
            self._silence_start = None

        # 4) Timeout d'écoute (silence total ou rien de clair)
        if now - self.listen_start > self.listen_timeout:
            was_followup = self.followup
            if was_followup:
                print("  [DISSCUSION] silence — retour en veille "
                      "(« PiDog » pour reprendre)", flush=True)
            else:
                print("  [STT] timeout — rien de clair", flush=True)
            self._silence_start = None
            self.exit_listen()
            if not was_followup:
                spawn(self.nothing_heard())
            else:
                mood_led()

    def exit_listen(self):
        global last_activity
        last_activity = time.time()
        self.state = self.IDLE
        self.cmd_rec = None
        self.followup = False
        self.wake_rec.Reset()
        self.wake_reset_at = time.time()

    async def nothing_heard(self):
        led("red")
        dog_sound()
        await say("Je n'ai rien entendu.")
        led("black", 0)

    async def announce_ready(self):
        """Annonce vocale quand PiDog est réellement prêt à dialoguer.

        Déclenchée après le PREMIER flux audio reçu par parec (donc carte
        HFP posée + micro en écoute effective) : sans ça, impossible de
        savoir depuis l'extérieur si l'agent est en état de parler.
        """
        print("  [PRÊT] micro en écoute → annonce vocale.", flush=True)
        led("blue", 0.8)
        await say("Je suis prêt à échanger. Dis PiDog pour me parler.")

    # -- boucle principale --
    async def run(self):
        print("Veille : dis « PiDog » pour me réveiller.", flush=True)
        ready_announced = False
        async for data in self.frames():
            if not ready_announced:
                # frames() n'a yieldé qu'une fois parec productif : tout est OK
                ready_announced = True
                spawn(self.announce_ready())
            # Pendant la playback : on consomme le flux pour vider le buffer
            # PulseAudio, mais on ne reconnaît pas (anti-écho JBL↔micro).
            if SPEAKING:
                continue
            if self.state == self.IDLE:
                if self.followup_pending:
                    # On laisse d'abord retomber l'écho de la dernière phrase
                    if time.time() >= self.followup_ready_at:
                        self.followup_pending = False
                        self.enter_listen(followup=True)
                    continue
                self.idle_step(data)
            else:
                self.listen_step(data)


# ------------------------------------------------------------- watchdog ----
# (retiré — le watchdog causait des mouvements/bruits)
GENTLE_SPEED = 20


def dog_startup_pose():
    """Position de démarrage : couché, tête à zéro (servo 0).

    Blocant (I2C) : appeler via asyncio.to_thread().
    L'I2C peut être encore occupé juste après le boot (bluez, autres
    services) : on réessaie la construction Pidog() quelques fois.
    """
    dog = _lazy_d()
    for attempt in range(3):
        if dog is not None:
            break
        time.sleep(3)
        dog = _lazy_d()
    if dog is None:
        print("Posture init : PiDog absent, on continue en mode voix seul.",
              flush=True)
        return
    try:
        # 1. Tête droite d'abord (servo à 0, immédiat, lent : évite le penché)
        dog.head_move([[0, 0, 0]], immediately=True, speed=3)
        # 2. Puis la posture couchée (lie inclut jambes + queue)
        dog.do_action("lie")
        # Stabilisation anti-vibration après posture init
        dog.stable()
        print("Posture init : couché, tête à zéro (servo 0).", flush=True)
    except Exception as e:
        print(f"Posture init : {e}", flush=True)


# ------------------------------------------------------- audio utilitaires --
def _rms(data: bytes) -> float:
    """RMS du chunk audio s16le mono — pour la détection de silence."""
    if not data:
        return 0.0
    # s16le little-endian
    import struct
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[:n * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


# ------------------------------------------------------------------ main ---
async def main():
    global last_activity, PIPELINE
    hard_reset_audio()  # kill tout + JBL + mute le Pi intégré cassé
    await asyncio.sleep(5)
    # Fix-bt-audio.sh n'existe plus : on force les profils ici (bloquant,
    # donc hors loop asyncio). Flip4 → HFP, autres cartes BT → A2DP.
    await asyncio.to_thread(setup_audio_route)
    source = JBL_SOURCE  # micro = source HFP de la Flip4
    # Reveille la source JBL (elle passe en SUSPENDED sans flux)
    if source:
        try:
            subprocess.run(["pactl", "set-source-state", source, "RUNNING"],
                           capture_output=True, timeout=3)
        except Exception:
            pass

    await asyncio.to_thread(dog_startup_pose)
    led("black", 0)

    print("=" * 58, flush=True)
    print("PiDog ASSISTANT STREAMING", flush=True)
    print(f"  LLM     : Groq / {LLM_MODEL}" + ("" if GROQ_KEY else "  (PAS DE CLÉ)"), flush=True)
    print(f"  STT     : Vosk streaming (wake word « PiDog »)", flush=True)
    print(f"  TTS     : Piper {os.path.basename(VOICE)}", flush=True)
    print(f"  Micro   : {source or 'défaut'}", flush=True)
    print("=" * 58, flush=True)

    last_activity = time.time()
    pipeline = MicPipeline(source)
    PIPELINE = pipeline
    try:
        await asyncio.gather(pipeline.run(), tts_worker(),
                             playback_worker())
    finally:
        PIPELINE = None
        led("black", 0)
        await asyncio.to_thread(dog_action, "lie")
        dog_head(0, 0)
        # Stabilisation anti-vibration avant arrêt agent
        try:
            await asyncio.to_thread(lambda: _lazy_d().stable() if _lazy_d() is not None else None)
        except Exception:
            pass
        print("Arrêt propre — PiDog couché, servos réinitialisés.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCtrl+C — au revoir.", flush=True)
