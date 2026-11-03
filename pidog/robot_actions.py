#!/usr/bin/env python3
"""Actions de haut niveau et SÉCURISÉES pour le PiDog.

Ce module est le seul endroit où l'on traduit une intention ("lève la patte",
"allume tes guirlandes", "assis", "remue la queue") en mouvements réels. Il est
utilisé par le daemon (`pidog.service`) : les clients CLI / voix passent donc
toujours par lui, même s'ils écrivent un bloc Python généré par le LLM.

Règles de sécurité importantes :

* **Transitions de posture** : on ne passe jamais directement de ``lie`` à
  ``stand`` (ou l'inverse) : on marque un palier ``sit``. Les servomoteurs
  consomment énormément pendant ce genre de mouvement et la batterie 2×18650
  peut provoquer un brown-out.
* **Tension batterie** : avant un mouvement lourd (posture, marche, patte),
  on lit la tension. En dessous du seuil critique, l'action est refusée avec
  une erreur explicite au lieu de risquer une coupure.
* **Vitesses douces** : les transitions utilisent une vitesse basse et on
  attend la fin de chaque étape avant d'envoyer la suivante.
* **Aucun accès GPIO ici** : la classe prend un objet ``Pidog`` déjà construit
  par le daemon. Les tests peuvent injecter un faux chien.

API destinée au LLM (via ``dog.action(...)``) :

    dog.action("posture", name="stand")
    dog.action("head_gesture", name="nod")
    dog.action("wag", times=4)
    dog.action("led", color="rainbow", style="breath")
    dog.action("lift_paw", leg="front_left")
    dog.action("walk", direction="forward", steps=1)

Le catalogue lisible par le LLM est exposé dans ``ACTION_CATALOG``.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "ACTION_CATALOG",
    "ACTION_ALIASES",
    "RobotActions",
    "ActionError",
    "LowBatteryError",
]

# ── Seuils de tension (batterie 2×18650 Li-ion, ~8.4 V pleine) ────────────────
# 6.9 V = prévenir / ralentir. 6.45 V = refuser les mouvements lourds.
VOLTAGE_WARN = float(os.getenv("PIDOG_VOLTAGE_WARN", "6.90"))
VOLTAGE_MIN = float(os.getenv("PIDOG_VOLTAGE_MIN", "6.45"))

# ── Vitesse de locomotion ────────────────────────────────────────────────────
# Attention : la vitesse n'est PAS linéaire. servo_move() (robot_hat) calcule
#     total_time = -9.9 * speed + 1000   (ms par image de marche)
# donc 80 = 208 ms/image, 90 = 109 ms, 98 = 58 ms. Un pas fait 49 images :
# à 80 ça donne ~10 s par pas, à 98 ~1,7 s. D'où le défaut à 98.
# Réglable sans toucher au code : PIDOG_WALK_SPEED dans l'environnement.
WALK_SPEED = max(0, min(100, int(os.getenv("PIDOG_WALK_SPEED", "98"))))

# ── Tête : convention de sens (UNE seule source de vérité) ───────────────────
# Vérifié sur le matériel et recoupé avec examples/7_face_track.py :
#   yaw > 0  → la tête tourne à GAUCHE (du point de vue du chien)
#   yaw < 0  → la tête tourne à DROITE
#   roll > 0 → penche à DROITE ; roll < 0 → penche à GAUCHE
#   pitch > 0 → lève le museau  ; pitch < 0 → baisse le museau
# Ne jamais inverser ces signes ailleurs : service.py, local_actions.py et
# stream/agent_stream.py s'appuient sur cette convention.
_HEAD_YAW = max(10, min(80, int(os.getenv("HEAD_LEFT_YAW", "30"))))
# Filet de sécurité si un lot de servo était monté à l'envers : -1 inverse
# gauche/droite *partout* sans toucher au code.
HEAD_YAW_SIGN = -1 if os.getenv("HEAD_YAW_SIGN", "1").strip().startswith("-") else 1
HEAD_LEFT_YAW = _HEAD_YAW * HEAD_YAW_SIGN
HEAD_RIGHT_YAW = -HEAD_LEFT_YAW
HEAD_TILT_ROLL = max(10, min(55, int(os.getenv("HEAD_TILT_ROLL", "25"))))
HEAD_TURN_SPEED = max(10, min(45, int(os.getenv("HEAD_TURN_SPEED", "30"))))

# ── Postures connues et actions motrices ─────────────────────────────────────
POSTURES = ("stand", "sit", "lie", "half_sit", "lie_with_hands_out")
POSTURE_FR = {
    "debout": "stand",
    "assis": "sit",
    "assise": "sit",
    "couche": "lie",
    "couché": "lie",
    "coucher": "lie",
    "dormir": "lie",
    "mi_assise": "half_sit",
    "mi_assise_pattes": "lie_with_hands_out",
}

GAITS = ("forward", "backward", "turn_left", "turn_right", "trot")

# Anciens raccourcis : dog.command("head_left") / do_action("head_left").
# Mêmes signes que HEAD_LEFT_YAW / HEAD_RIGHT_YAW ci-dessus.
HEAD_SHORTCUTS = {
    "head_left": (HEAD_LEFT_YAW, 0, 0),
    "head_right": (HEAD_RIGHT_YAW, 0, 0),
    "head_up": (0, 0, 22),
    "head_down": (0, 0, -25),
}

# ── Couleurs / styles LED ────────────────────────────────────────────────────
LED_COLORS = {
    "white", "black", "red", "yellow", "green", "blue", "cyan",
    "magenta", "pink", "orange", "purple",
}
LED_STYLES = {"breath", "boom", "bark", "speak", "listen"}
RAINBOW = ("red", "orange", "yellow", "green", "cyan", "blue", "magenta", "purple", "pink")

# ── Sons disponibles (fichiers du dossier sounds/) ───────────────────────────
SOUND_ALIASES = {
    "bark": "single_bark_1",
    "bark2": "single_bark_2",
    "aboiement": "single_bark_1",
    "growl": "growl_1",
    "grognement": "growl_1",
    "happy": "r2d2_happy_1",
    "joie": "r2d2_happy_1",
    "sad": "r2d2_sad_1",
    "triste": "r2d2_sad_1",
    "confused": "r2d2_confused_2",
    "confus": "r2d2_confused_2",
    "question": "r2d2_question_1",
    "howl": "howling",
    "hurlement": "howling",
    "pant": "pant",
    "haletant": "pant",
    "snore": "snoring",
    "ronflement": "snoring",
    "woohoo": "woohoo",
}

# ── Alias d'actions (français / formulations naturelles) ─────────────────────
ACTION_ALIASES = {
    # postures
    "assis": "posture", "assise": "posture", "sit": "posture",
    "debout": "posture", "stand": "posture", "up": "posture",
    "couche": "posture", "couché": "posture", "lie": "posture",
    "leve_toi": "posture", "lève_toi": "posture",
    "couche_toi": "posture", "couche-toi": "posture",
    "assieds_toi": "posture", "assieds-toi": "posture",
    "half_sit": "posture",
    # tête
    "tete": "head", "tête": "head", "head": "head",
    "regarde": "head_gesture", "look": "head_gesture",
    "hoche": "head_gesture", "hoche_la_tete": "head_gesture",
    "secoue": "head_gesture", "secoue_la_tete": "head_gesture",
    "penche": "head_gesture", "penche_la_tete": "head_gesture",
    "nod": "head_gesture", "shake": "head_gesture",
    "tilt": "head_gesture", "tilt_left": "head_gesture",
    "tilt_right": "head_gesture",
    # tête : contrôle du sens (cf. HEAD_LEFT_YAW / HEAD_RIGHT_YAW)
    "tourne_tete_gauche": "turn_head_left", "turn_head_left": "turn_head_left",
    "tourne_tete_droite": "turn_head_right", "turn_head_right": "turn_head_right",
    "tourne_la_tete_a_gauche": "turn_head_left",
    "tourne_la_tete_a_droite": "turn_head_right",
    "tete_a_gauche": "turn_head_left", "tete_a_droite": "turn_head_right",
    "regarde_gauche": "turn_head_left", "regarde_a_gauche": "turn_head_left",
    "regarde_droite": "turn_head_right", "regarde_a_droite": "turn_head_right",
    "look_left": "turn_head_left", "look_right": "turn_head_right",
    "center": "head_center", "centre": "head_center",
    "axe_centre": "head_center", "milieu": "head_center",
    # queue
    "queue": "wag", "tail": "wag", "remue": "wag",
    "remue_la_queue": "wag", "wag": "wag", "wag_tail": "wag",
    # LED
    "led": "led", "leds": "led", "lumiere": "led", "lumière": "led",
    "lumieres": "led", "lumières": "led", "guirlande": "led",
    "guirlandes": "led", "lights": "led", "light": "led",
    "eclaire": "led", "éclaire": "led", "allume": "led",
    # sons
    "son": "sound", "sound": "sound", "aboie": "bark", "aboyer": "bark",
    "bark": "bark", "hurle": "howl", "howl": "howl",
    # pattes
    "patte": "lift_paw", "papatte": "lift_paw", "paw": "lift_paw",
    "leve_la_patte": "lift_paw", "lève_la_patte": "lift_paw",
    "serre_la_main": "handshake", "handshake": "handshake",
    "tape_la_main": "high_five", "high_five": "high_five", "high5": "high_five",
    "gratte": "scratch", "scratch": "scratch",
    # locomotion
    "avance": "walk", "walk": "walk", "marche": "walk",
    "recule": "walk", "backward": "walk",
    "tourne": "turn", "turn": "turn", "tourne_a_droite": "turn",
    "tourne_a_gauche": "turn",
    "trotte": "trot", "trot": "trot",
    "etire": "stretch", "étire": "stretch", "stretch": "stretch",
    "pompe": "push_up", "push_up": "push_up", "pushup": "push_up",
    "celebre": "celebrate", "célèbre": "celebrate", "celebrate": "celebrate",
    "stop": "stop", "arrete": "stop", "arrête": "stop",
    "batterie": "battery", "battery": "battery", "tension": "battery",
    "repos": "rest", "rest": "rest", "dors": "rest",
}

# ── Catalogue pour le prompt du LLM (et pour l'humain) ───────────────────────
ACTION_CATALOG = """\
ACTIONS ROBOT (bloc <<<CODE>>>, via dog.action(...)) :
- Posture : dog.action("posture", name="stand"|"sit"|"lie"|"half_sit")
  (le daemon insère automatiquement un palier "sit" entre couché et debout ;
  ne cherche jamais à enchaîner couch\u00e9->debout ou debout->couch\u00e9 toi-m\u00eame)
- T\u00eate : dog.action("head", yaw=0, roll=0, pitch=0, speed=30)
  dog.action("head_gesture", name="nod"|"shake"|"tilt_left"|"tilt_right"|
             "look_left"|"look_right"|"look_up"|"look_down"|"center")
- Queue : dog.action("tail", angle=0) ou dog.action("wag", times=4, amplitude=30)
- LED / guirlandes : dog.action("led", color="red"|"green"|"blue"|"yellow"|
             "cyan"|"magenta"|"pink"|"white"|"rainbow", style="breath"|"boom"|
             "bark"|"speak"|"listen", brightness=0.6, bps=1)
  dog.action("led", off=True) \u00e9teint les LED.
- Sons : dog.action("sound", name="bark"|"growl"|"happy"|"sad"|"confused"|
             "question"|"howl"|"pant"|"snore"|"woohoo")
  dog.action("bark", times=1) aboie en remuant la t\u00eate.
- Pattes : dog.action("lift_paw", leg="front_left"|"front_right")
  dog.action("wave_paw", leg="front_left"|"front_right")
  dog.action("handshake"), dog.action("high_five"), dog.action("scratch")
- Locomotion : dog.action("walk", direction="forward"|"backward", steps=1)
  dog.action("turn", direction="left"|"right", steps=1)
  dog.action("trot", steps=1)
- Autres : dog.action("stretch"), dog.action("push_up", times=1),
  dog.action("howl"), dog.action("celebrate"), dog.action("stop"),
  dog.action("battery"), dog.action("rest")
- Pr\u00e9-r\u00e9glages SunFounder : dog.do_action("stand"|"sit"|"lie"|"stretch"|
  "push_up"|"wag_tail"|"shake_head"|"tilting_head_left"|"tilting_head_right"|
  "head_bark"|"head_up_down"|"nod_lethargy"|"forward"|"backward"|"turn_left"|
  "turn_right"|"trot"|"half_sit"|"lie_with_hands_out")
"""


class ActionError(RuntimeError):
    """Action inconnue, paramètre invalide ou mouvement impossible."""


class LowBatteryError(ActionError):
    """Tension trop basse : mouvement lourd refusé pour éviter un brown-out."""


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def normalize(name: str) -> str:
    """Normalise un nom d'action : accents, espaces, tirets → underscores."""
    text = _strip_accents(str(name or "")).strip().lower()
    for sep in (" ", "-", ".", ",", "'", '"'):
        text = text.replace(sep, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


class RobotActions:
    """Traduit des intentions de haut niveau en mouvements PiDog sûrs.

    ``dog`` doit être l'instance ``Pidog`` réelle (côté daemon) ou un faux
    objet de test exposant les mêmes méthodes.
    """

    # Posture assise de référence (SunFounder).
    SIT_ANGLES = [30, 60, -30, -60, 80, -45, -80, 45]

    # Pose "patte avant levée". Les pattes avant sont les servos [0,1] (gauche)
    # et [2,3] (droite) ; les valeurs sont les poses SunFounder hand_shake
    # (droite) et leur miroir exact (gauche).
    LIFT_POSES = {
        "front_right": [30, 60, -20, 65, 80, -45, -80, 38],
        "front_left": [20, -65, -30, -60, 80, -38, -80, 45],
    }
    WAVE_VARIANTS = {
        "front_right": [
            [30, 60, -20, 65, 80, -45, -80, 38],
            [30, 60, -35, 45, 80, -45, -80, 38],
            [30, 60, -15, 60, 80, -45, -80, 38],
        ],
        "front_left": [
            [20, -65, -30, -60, 80, -38, -80, 45],
            [35, -45, -30, -60, 80, -38, -80, 45],
            [15, -60, -30, -60, 80, -38, -80, 45],
        ],
    }

    def __init__(self, dog, voltage_warn: float = VOLTAGE_WARN,
                 voltage_min: float = VOLTAGE_MIN):
        self.dog = dog
        self.voltage_warn = float(voltage_warn)
        self.voltage_min = float(voltage_min)
        self.posture_state: Optional[str] = None
        self._lock = threading.RLock()
        self._voltage_at = 0.0
        self._voltage_value: Optional[float] = None

    # ------------------------------------------------------------- utilitaires
    def _wait(self, timeout: Optional[float] = None):
        try:
            self.dog.wait_all_done()
        except Exception:
            pass

    def _sleep(self, seconds: float):
        if seconds > 0:
            time.sleep(seconds)

    def _battery(self, fresh: bool = False) -> Optional[float]:
        now = time.time()
        if not fresh and self._voltage_value is not None and now - self._voltage_at < 2.0:
            return self._voltage_value
        try:
            value = self.dog.get_battery_voltage()
            self._voltage_value = float(value)
            self._voltage_at = now
        except Exception:
            self._voltage_value = None
        return self._voltage_value

    def _guard_power(self, action: str = "mouvement"):
        """Refuse un mouvement lourd si la batterie est trop basse."""
        voltage = self._battery()
        if voltage is None:
            return None
        if voltage < self.voltage_min:
            raise LowBatteryError(
                f"batterie trop faible pour {action} ({voltage:.2f} V < "
                f"{self.voltage_min:.2f} V) — recharge le PiDog avant de bouger"
            )
        if voltage < self.voltage_warn:
            # Petite pause : laisse la batterie remonter après une pointe.
            self._sleep(0.4)
        return voltage

    def _raw_action(self, name: str, step_count: int = 1, speed: int = 45,
                    pitch_comp: int = 0):
        self.dog.do_action(
            name,
            step_count=max(1, int(step_count)),
            speed=max(0, min(100, int(speed))),
            pitch_comp=pitch_comp,
        )
        self._wait()

    def _legs(self, angles: Sequence[Sequence[float]], speed: int = 45,
              immediately: bool = False):
        self.dog.legs_move(
            [list(a) for a in angles],
            immediately=immediately,
            speed=max(0, min(100, int(speed))),
        )
        self._wait()

    def _head(self, angles: Sequence[Sequence[float]], speed: int = 30,
              immediately: bool = True):
        self.dog.head_move(
            [list(a) for a in angles],
            immediately=immediately,
            speed=max(0, min(100, int(speed))),
        )
        self._wait()

    def _head_raw(self, angles: Sequence[Sequence[float]], speed: int = 30,
                  immediately: bool = True):
        self.dog.head_move_raw(
            [list(a) for a in angles],
            immediately=immediately,
            speed=max(0, min(100, int(speed))),
        )
        self._wait()

    def _tail(self, angles: Sequence[Sequence[float]], speed: int = 30,
              immediately: bool = True):
        self.dog.tail_move(
            [list(a) for a in angles],
            immediately=immediately,
            speed=max(0, min(100, int(speed))),
        )
        self._wait()

    # --------------------------------------------------------------- postures
    def _posture_path(self, target: str) -> List[str]:
        current = self.posture_state
        if current == target:
            return []
        if current is None:
            # Posture inconnue (démarrage) : on commence toujours par le
            # palier assis, sauf si on veut déjà s'asseoir.
            return ["sit"] if target == "sit" else ["sit", target]
        if target == "sit":
            return ["sit"]
        # Transitions "hautes" : jamais directes entre couché et debout.
        if {current, target} == {"lie", "stand"}:
            return ["sit", target]
        if current in ("lie", "half_sit", "lie_with_hands_out") and target == "stand":
            return ["sit", "stand"]
        if current == "stand" and target in ("lie", "half_sit", "lie_with_hands_out"):
            return ["sit", target]
        if current in ("lie", "lie_with_hands_out") and target == "half_sit":
            return ["sit", "half_sit"]
        if current == "half_sit" and target == "lie":
            return ["sit", "lie"]
        return [target]

    def posture(self, name: str = "sit", speed: Optional[int] = None) -> str:
        """Change la posture en respectant les paliers de sécurité."""
        target = POSTURE_FR.get(normalize(name), normalize(name))
        if target not in POSTURES:
            raise ActionError(
                f"posture inconnue : {name!r} (attendu : {', '.join(POSTURES)})"
            )
        with self._lock:
            path = self._posture_path(target)
            if not path:
                return target
            self._guard_power(f"posture {target}")
            step_speed = int(speed) if speed is not None else 40
            step_speed = max(20, min(60, step_speed))
            for index, step in enumerate(path):
                # Le palier intermédiaire est toujours plus lent.
                sp = step_speed if index == len(path) - 1 else min(step_speed, 35)
                self._raw_action(step, speed=sp)
                self.posture_state = step
                if index < len(path) - 1:
                    self._sleep(0.45)
            return target

    def rest(self) -> str:
        return self.posture("lie")

    # ------------------------------------------------------------------- tête
    def head(self, yaw: float = 0, roll: float = 0, pitch: float = 0,
             speed: int = 30) -> Dict[str, Any]:
        yaw = max(-80.0, min(80.0, float(yaw)))
        roll = max(-55.0, min(55.0, float(roll)))
        pitch = max(-40.0, min(25.0, float(pitch)))
        with self._lock:
            # La tête est lourde et vibre : on plafonne la vitesse.
            self._head([[yaw, roll, pitch]], speed=min(45, max(10, int(speed))))
        return {"yaw": yaw, "roll": roll, "pitch": pitch}

    def head_gesture(self, name: str = "nod", speed: int = 30) -> str:
        key = normalize(name)
        # Rotations horizontales : lacet pur (aucun roll/pitch → pas de
        # « tête penchée »), même chemin que turn_head_left/right.
        pure_yaw = {
            "look_left": HEAD_LEFT_YAW, "regarde_gauche": HEAD_LEFT_YAW,
            "look_right": HEAD_RIGHT_YAW, "regarde_droite": HEAD_RIGHT_YAW,
            "center": 0.0, "centre": 0.0,
        }
        if key in pure_yaw:
            self._look_yaw(pure_yaw[key], speed)
            return key
        gestures = {
            "nod": [[0, 0, 12], [0, 0, -12], [0, 0, 10], [0, 0, 0]],
            "hoche": [[0, 0, 12], [0, 0, -12], [0, 0, 10], [0, 0, 0]],
            "shake": [[35, 0, 0], [-35, 0, 0], [30, 0, 0], [-30, 0, 0], [0, 0, 0]],
            "secoue": [[35, 0, 0], [-35, 0, 0], [30, 0, 0], [-30, 0, 0], [0, 0, 0]],
            "tilt_left": [[0, -HEAD_TILT_ROLL, 15]],
            "tilt_right": [[0, HEAD_TILT_ROLL, 20]],
            "penche_gauche": [[0, -HEAD_TILT_ROLL, 15]],
            "penche_droite": [[0, HEAD_TILT_ROLL, 20]],
            "look_left": [[HEAD_LEFT_YAW, 0, 0]],
            "regarde_gauche": [[HEAD_LEFT_YAW, 0, 0]],
            "look_right": [[HEAD_RIGHT_YAW, 0, 0]],
            "regarde_droite": [[HEAD_RIGHT_YAW, 0, 0]],
            "look_up": [[0, 0, 22]],
            "regarde_en_haut": [[0, 0, 22]],
            "look_down": [[0, 0, -25]],
            "regarde_en_bas": [[0, 0, -25]],
            "center": [[0, 0, 0]],
            "centre": [[0, 0, 0]],
        }
        if key in gestures:
            with self._lock:
                self._head(gestures[key], speed=min(40, max(10, int(speed))),
                           immediately=True)
            return key

        presets = {
            "head_bark", "head_up_down", "nod_lethargy", "shake_head",
            "tilting_head_left", "tilting_head_right", "tilting_head",
        }
        raw = normalize(name)
        if raw in presets:
            with self._lock:
                self._raw_action(raw, speed=min(40, max(10, int(speed))))
            return raw
        raise ActionError(
            f"geste de tête inconnu : {name!r} (nod, shake, tilt_left, "
            "tilt_right, look_left, look_right, look_up, look_down, center)"
        )

    # ------------------------------------------------------------------ queue
    def tail(self, angle: float = 0, speed: int = 30) -> float:
        angle = max(-60.0, min(60.0, float(angle)))
        with self._lock:
            self._tail([[angle]], speed=min(60, max(10, int(speed))))
        return angle

    def wag(self, times: int = 4, amplitude: float = 30, speed: int = 45) -> Dict[str, Any]:
        times = max(1, min(12, int(times)))
        amplitude = max(10.0, min(55.0, float(amplitude)))
        sequence: List[List[float]] = []
        for _ in range(times):
            sequence.append([amplitude])
            sequence.append([-amplitude])
        sequence.append([0])
        with self._lock:
            self._tail(sequence, speed=min(70, max(20, int(speed))),
                       immediately=True)
        return {"times": times, "amplitude": amplitude}

    # -------------------------------------------------------------------- LED
    def led(self, color: str = "rainbow", style: str = "breath",
            brightness: float = 0.6, bps: float = 1.0,
            off: bool = False) -> Dict[str, Any]:
        """Allume la barre LED (les « guirlandes » du chien)."""
        style_key = normalize(style)
        if style_key not in LED_STYLES:
            style_key = "breath"
        if off:
            color_key = "black"
            brightness = 0.0
        else:
            color_key = normalize(color)
            if color_key in ("guirlandes", "guirlande", "rainbow", "arc_en_ciel",
                             "noel", "fete", "fête", "multicolore"):
                color_key = "rainbow"
        brightness = max(0.0, min(1.0, float(brightness)))
        bps = max(0.1, min(10.0, float(bps)))

        with self._lock:
            if color_key == "rainbow":
                for item in RAINBOW:
                    self.dog.rgb_strip.set_mode(
                        style=style_key, color=item, brightness=brightness, bps=bps
                    )
                    self._sleep(0.28)
                color_key = RAINBOW[-1]
            else:
                if color_key not in LED_COLORS and color_key != "black":
                    # Couleur hexadécimale acceptée (#a10a0a) ou nom inconnu.
                    if not color_key.startswith("#"):
                        color_key = "cyan"
                self.dog.rgb_strip.set_mode(
                    style=style_key, color=color_key, brightness=brightness, bps=bps
                )
        return {"color": color_key, "style": style_key,
                "brightness": brightness}

    def led_off(self) -> Dict[str, Any]:
        return self.led(off=True)

    # ------------------------------------------------------------------- sons
    def sound(self, name: str = "bark") -> str:
        key = normalize(name)
        sound_name = SOUND_ALIASES.get(key, key)
        with self._lock:
            try:
                self.dog.music.sound_play(sound_name)
            except Exception:
                # Certaines versions exposent speak() plutôt que music.
                self.dog.speak(sound_name)
        return sound_name

    def bark(self, times: int = 1, volume: int = 100) -> Dict[str, Any]:
        times = max(1, min(5, int(times)))
        for _ in range(times):
            with self._lock:
                self._head([[0, 0, 22]], speed=45)
                try:
                    self.dog.music.sound_play("single_bark_1")
                except Exception:
                    pass
                self._head([[0, 0, 0]], speed=35)
            self._sleep(0.15)
        return {"times": times}

    def howl(self) -> str:
        with self._lock:
            self.posture("sit")
            self._head([[0, 0, -25]], speed=35)
            try:
                self.dog.music.sound_play("howling")
            except Exception:
                self.dog.speak("howling")
            self._sleep(2.2)
            self._head([[0, 0, 0]], speed=30)
        return "howl"

    # ----------------------------------------------------------------- pattes
    def lift_paw(self, leg: str = "front_left", hold: float = 1.2,
                 speed: int = 55) -> Dict[str, Any]:
        key = normalize(leg)
        key = {
            "left": "front_left", "gauche": "front_left",
            "front_left": "front_left", "avant_gauche": "front_left",
            "patte_gauche": "front_left",
            "right": "front_right", "droite": "front_right",
            "front_right": "front_right", "avant_droite": "front_right",
            "patte_droite": "front_right",
        }.get(key, key)
        if key not in self.LIFT_POSES:
            raise ActionError(
                "patte non supportée : utilise front_left (patte avant gauche) "
                "ou front_right (patte avant droite)"
            )
        self._guard_power("lever la patte")
        with self._lock:
            self.posture("sit")
            self._legs([self.LIFT_POSES[key]], speed=speed, immediately=True)
            self._sleep(max(0.2, min(3.0, float(hold))))
            self._legs([self.SIT_ANGLES], speed=speed, immediately=True)
        return {"leg": key}

    def wave_paw(self, leg: str = "front_right", times: int = 3,
                 speed: int = 50) -> Dict[str, Any]:
        key = normalize(leg)
        key = {
            "left": "front_left", "gauche": "front_left",
            "avant_gauche": "front_left", "patte_gauche": "front_left",
            "right": "front_right", "droite": "front_right",
            "avant_droite": "front_right", "patte_droite": "front_right",
        }.get(key, key)
        if key not in self.WAVE_VARIANTS:
            raise ActionError("patte non supportée : front_left ou front_right")
        times = max(1, min(8, int(times)))
        self._guard_power("saluer avec la patte")
        variants = self.WAVE_VARIANTS[key]
        with self._lock:
            self.posture("sit")
            sequence = [self.LIFT_POSES[key]]
            for _ in range(times):
                sequence.extend([variants[1], variants[2]])
            sequence.append(self.SIT_ANGLES)
            self._legs(sequence, speed=speed, immediately=True)
        return {"leg": key, "times": times}

    def handshake(self) -> str:
        return str(self.wave_paw("front_right", times=4, speed=48)["leg"])

    def high_five(self) -> str:
        with self._lock:
            self.posture("sit")
            self._legs([self.LIFT_POSES["front_right"]], speed=60,
                       immediately=True)
            self._sleep(1.2)
            self._legs([self.SIT_ANGLES], speed=50, immediately=True)
        return "high_five"

    def scratch(self) -> str:
        """Gratte l'oreille avec la patte avant droite (pose SunFounder)."""
        f_up = [[30, 60, 50, 50, 80, -45, -80, 38]]
        f_scratch = [[30, 60, 40, 40, 80, -45, -80, 38]]
        self._guard_power("gratouiller")
        with self._lock:
            self.posture("sit")
            self._head([[30, 70, -10]], speed=35)
            self._legs(f_up, speed=45)
            for _ in range(8):
                self._legs(f_scratch, speed=65)
            self._head([[0, 0, -40]], speed=35)
        return "scratch"

    # ------------------------------------------------------------- locomotion
    def walk(self, direction: str = "forward", steps: int = 1,
             speed: Optional[int] = None) -> Dict[str, Any]:
        key = normalize(direction)
        if key in ("arriere", "arrière", "back", "backward", "recule"):
            key = "backward"
        elif key in ("avant", "forward", "avance", "marche"):
            key = "forward"
        else:
            raise ActionError("direction de marche : forward ou backward")
        steps = max(1, min(8, int(steps)))
        speed = WALK_SPEED if speed is None else speed
        self._guard_power("marcher")
        with self._lock:
            self.posture("stand")
            self._raw_action(key, step_count=steps, speed=speed)
            self._sleep(0.25)
        return {"direction": key, "steps": steps}

    def turn(self, direction: str = "left", steps: int = 1,
             speed: Optional[int] = None) -> Dict[str, Any]:
        key = normalize(direction)
        if key in ("gauche", "left", "turn_left"):
            key = "turn_left"
        elif key in ("droite", "right", "turn_right"):
            key = "turn_right"
        else:
            raise ActionError("direction de rotation : left ou right")
        steps = max(1, min(8, int(steps)))
        speed = WALK_SPEED if speed is None else speed
        self._guard_power("tourner")
        with self._lock:
            self.posture("stand")
            self._raw_action(key, step_count=steps, speed=speed)
            self._sleep(0.25)
        return {"direction": key, "steps": steps}

    def trot(self, steps: int = 1, speed: Optional[int] = None) -> Dict[str, Any]:
        steps = max(1, min(6, int(steps)))
        speed = WALK_SPEED if speed is None else speed
        self._guard_power("trotter")
        with self._lock:
            self.posture("stand")
            self._raw_action("trot", step_count=steps, speed=speed)
        return {"steps": steps}

    def stretch(self) -> str:
        head_angs = [[0, 0, 25]]
        leg_angs = [
            [-80, 70, 80, -70, -20, 64, 20, -64],
            [-65, 70, 65, -70, -20, 64, 20, -64],
            [-80, 70, 80, -70, -20, 64, 20, -64],
        ]
        self._guard_power("s'étirer")
        with self._lock:
            self.posture("sit")
            self._head(head_angs, speed=35)
            self._legs(leg_angs, speed=45)
            self._sleep(0.3)
            self._legs([[40, 35, -40, -35, 60, 5, -60, -5],
                        self.SIT_ANGLES], speed=55)
            self.posture_state = "sit"
        return "stretch"

    def push_up(self, times: int = 1, speed: int = 45) -> str:
        times = max(1, min(5, int(times)))
        self._guard_power("faire des pompes")
        with self._lock:
            self.posture("stand")
            self._raw_action("push_up", step_count=times, speed=speed)
        return "push_up"

    def bow(self, times: int = 1, speed: int = 30) -> str:
        """Incline la tête sans changer de posture (révérence).
        
        :param times: nombre de révérences (1-3)
        :param speed: vitesse de déplacement de la tête (10-45)
        """
        times = max(1, min(3, int(times)))
        speed = max(10, min(45, int(speed)))
        self._guard_power("faire une révérence")
        with self._lock:
            # La tête baisse de ~30° (pitch négatif), puis remonte à chaque tour.
            for _ in range(times):
                self._head([[0, 0, -30]], speed=speed)
                self._head([[0, 0, 0]], speed=speed)
        return "bow"

    def _look_yaw(self, yaw: float, speed: int = HEAD_TURN_SPEED) -> str:
        """Rotation de tête PURE en lacet (aucun roll/pitch parasite).

        On passe par ``head_move_raw`` pour ne pas dépendre de
        ``head_rpy_to_angle()`` : c'est ce mélange roll/pitch qui donnait
        l'impression que la tête « penchait » quand on tournait.
        """
        speed = max(10, min(45, int(speed)))
        yaw = max(-80.0, min(80.0, float(yaw)))
        self._guard_power("tourner la tête")
        with self._lock:
            self._head_raw([[yaw, 0, 0]], speed=speed)
        return yaw

    def look(self, direction: str = "left", speed: int = HEAD_TURN_SPEED) -> str:
        """Tourne la tête dans une direction horizontale (gauche/droite)."""
        key = normalize(direction)
        if key in ("gauche", "left", "turn_left"):
            self._look_yaw(HEAD_LEFT_YAW, speed)
            return "left"
        if key in ("droite", "right", "turn_right"):
            self._look_yaw(HEAD_RIGHT_YAW, speed)
            return "right"
        if key in ("centre", "center", "face", "devant"):
            self._look_yaw(0, speed)
            return "center"
        raise ActionError("direction de tête : left, right ou center")

    def turn_head_left(self, steps: int = 1, speed: int = HEAD_TURN_SPEED) -> str:
        """Tourne la tête à GAUCHE (yaw positif, convention PiDog).

        :param steps: conservé pour compatibilité (ignoré)
        :param speed: vitesse de déplacement (10-45)
        """
        self._look_yaw(HEAD_LEFT_YAW, speed)
        return "turn_head_left"

    def turn_head_right(self, steps: int = 1, speed: int = HEAD_TURN_SPEED) -> str:
        """Tourne la tête à DROITE (yaw négatif, convention PiDog).

        :param steps: conservé pour compatibilité (ignoré)
        :param speed: vitesse de déplacement (10-45)
        """
        self._look_yaw(HEAD_RIGHT_YAW, speed)
        return "turn_head_right"

    def head_center(self, speed: int = HEAD_TURN_SPEED) -> str:
        """Remet la tête bien droite dans l'axe."""
        self._look_yaw(0, speed)
        return "center"

    def groove(self, times: int = 1) -> str:
        """Mouvement de danse léger : petite inclinaison + wag de queue.
        
        :param times: nombre de cycles (1-3)
        """
        times = max(1, min(3, int(times)))
        self._guard_power("dancer")
        with self._lock:
            self.posture("stand")
            for _ in range(times):
                # Incline la tête
                self._head([[0, 0, -20]], speed=35)
                # Wag la queue doucement
                self._tail([[30]], speed=45)
                self._tail([[-30]], speed=45)
                # Reviens au centre
                self._tail([[0]], speed=50)
        return "groove"

    def celebrate(self) -> str:
        with self._lock:
            self.posture("stand")
            self.led("rainbow", style="boom", brightness=0.8, bps=2)
            self.wag(times=5, amplitude=40, speed=60)
            self.head_gesture("nod", speed=40)
            self.sound("happy")
        return "celebrate"

    def stop(self) -> str:
        with self._lock:
            try:
                self.dog.body_stop()
            except Exception:
                pass
            # La posture n'est plus garantie après un arrêt.
            self.posture_state = None
        return "stop"

    def battery(self) -> Dict[str, Any]:
        voltage = self._battery(fresh=True)
        return {"voltage": voltage, "warn": self.voltage_warn,
                "min": self.voltage_min}

    # ------------------------------------------------------------- dispatcher
    def do_action(self, name: str, step_count: int = 1, speed: int = 45,
                  pitch_comp: int = 0) -> Any:
        """Point d'entrée bas niveau (compatible Pidog.do_action)."""
        key = normalize(name)
        if key in HEAD_SHORTCUTS:
            yaw, roll, pitch = HEAD_SHORTCUTS[key]
            return self.head(yaw=yaw, roll=roll, pitch=pitch, speed=speed)
        if key in POSTURES:
            return self.posture(key, speed=speed)
        if key in GAITS:
            self._guard_power(key)
            with self._lock:
                if key in ("forward", "backward", "turn_left", "turn_right", "trot"):
                    self.posture("stand")
                self._raw_action(key, step_count=step_count, speed=speed)
            return key
        # Preset SunFounder quelconque (tête, queue, stretch...).
        try:
            part = self.dog.actions_dict[key][1]
        except Exception:
            raise ActionError(f"action inconnue : {name!r}")
        with self._lock:
            self._raw_action(key, step_count=step_count, speed=speed,
                             pitch_comp=pitch_comp)
            if part == "legs":
                self.posture_state = None
        return key

    def dispatch(self, action_name: str, **params: Any) -> Any:
        """Exécute une action nommée : ``dog.action("led", color="rainbow")``."""
        raw = normalize(action_name)
        key = ACTION_ALIASES.get(raw, raw)

        # Formulations libres autour des LED / guirlandes.
        if any(w in raw for w in ("guirlande", "lumier", "lumiere", "led",
                                  "light", "neon", "néon")):
            key = "led"
            if any(w in raw for w in ("etein", "étein", "off", "coupe")):
                params.setdefault("off", True)

        # Formulations libres autour des pattes :
        # « lève la papatte gauche », « patte avant droite »...
        if ("patte" in raw or "paw" in raw) and key not in (
                "lift_paw", "wave_paw", "handshake", "high_five"):
            if "arriere" in raw or "back" in raw:
                raise ActionError(
                    "les pattes arrière portent le chien : utilise une patte "
                    "avant (front_left / front_right)"
                )
            key = "lift_paw"
            if "gauche" in raw or "left" in raw:
                params.setdefault("leg", "front_left")
            elif "droite" in raw or "right" in raw:
                params.setdefault("leg", "front_right")
        if key == "lift_paw" and "leg" not in params:
            if "gauche" in raw or "left" in raw:
                params["leg"] = "front_left"
            elif "droite" in raw or "right" in raw:
                params["leg"] = "front_right"

        # Alias "posture" : si name="assis", on route vers posture(name=...).
        if key == "posture" and "name" not in params and raw in POSTURE_FR:
            params["name"] = POSTURE_FR[raw]
        if key == "posture" and "name" not in params and raw in POSTURES:
            params["name"] = raw
        if key == "lift_paw" and "leg" not in params and raw.startswith("patte_"):
            params["leg"] = raw.replace("patte_", "front_", 1)
        if key == "turn" and "direction" not in params:
            if raw in ("tourne_a_gauche", "gauche"):
                params["direction"] = "left"
            elif raw in ("tourne_a_droite", "droite"):
                params["direction"] = "right"
        if key == "walk" and "direction" not in params:
            params["direction"] = "backward" if raw == "recule" else "forward"
        if key == "sound" and "name" not in params and raw not in ("sound", "son"):
            params["name"] = raw

        if key in HEAD_SHORTCUTS:
            yaw, roll, pitch = HEAD_SHORTCUTS[key]
            return self.head(yaw=yaw, roll=roll, pitch=pitch)

        methods = {
            "posture": self.posture,
            "rest": self.rest,
            "head": self.head,
            "head_gesture": self.head_gesture,
            "head_center": self.head_center,
            "look": self.look,
            "turn_head_left": self.turn_head_left,
            "turn_head_right": self.turn_head_right,
            "bow": self.bow,
            "groove": self.groove,
            "tail": self.tail,
            "wag": self.wag,
            "led": self.led,
            "led_off": self.led_off,
            "sound": self.sound,
            "bark": self.bark,
            "howl": self.howl,
            "lift_paw": self.lift_paw,
            "wave_paw": self.wave_paw,
            "handshake": self.handshake,
            "high_five": self.high_five,
            "scratch": self.scratch,
            "walk": self.walk,
            "turn": self.turn,
            "trot": self.trot,
            "stretch": self.stretch,
            "push_up": self.push_up,
            "celebrate": self.celebrate,
            "stop": self.stop,
            "battery": self.battery,
        }
        if key in methods:
            return methods[key](**params)

        # Sinon : preset SunFounder direct (stand, sit, wag_tail, ...).
        try:
            known = self.dog.actions_dict[key]
            _ = known
        except Exception:
            raise ActionError(
                f"action inconnue : {action_name!r}. Actions : "
                + ", ".join(sorted(methods))
            )
        return self.do_action(key, **params)

    def available(self) -> List[str]:
        return sorted(set(ACTION_ALIASES) | set(POSTURES) | set(GAITS)
                      | {
                          "posture", "rest", "head", "head_gesture", "tail",
                          "wag", "led", "led_off", "sound", "bark", "howl",
                          "lift_paw", "wave_paw", "handshake", "high_five",
                          "scratch", "walk", "turn", "trot", "stretch",
                          "push_up", "celebrate", "stop", "battery",
                      })
