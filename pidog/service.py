#!/usr/bin/env python3
"""PiDog Service — un seul daemon possède le robot (GPIO/I2C/robot_hat).

Architecture
------------
  * ``pidog start`` lance UN daemon détaché (ou systemd via pidog.service).
  * Le daemon initialise UNE seule fois ``Pidog()`` puis expose un socket Unix
    JSON sur ``/tmp/pidog.sock``.
  * Tous les clients (CLI, stream, vision, monitor) sont de simples clients
    socket : ils ne créent JAMAIS de ``Pidog()``, ne touchent JAMAIS au GPIO et
    ne démarrent JAMAIS un second serveur.

API publique
------------
  start()            lance le daemon et attend qu'il soit prêt
  stop()             arrête proprement le daemon (chien couché, GPIO rendu)
  status()           état lisible par un humain
  service_state()    état structuré (dict) pour les extensions
  is_ready()         True si le daemon répond et que le robot est initialisé
  my_dog()/get_pidog() proxy RPC vers le robot (toujours le daemon)
  release_pidog()    parque le robot (couché + tête centrée + LED off),
                     NE TUE PAS le daemon
  park()             idem release_pidog()
"""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Chemins ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = "/tmp/pidog.lock"
SOCKET_PATH = "/tmp/pidog.sock"
DAEMON_LOG = "/tmp/pidog-daemon.log"
DAEMON_SCRIPT = str(REPO_ROOT / "extensions" / "pidog-agent" / "pidog_service_daemon.py")

# ── Logging (stderr → journal systemd ou /tmp/pidog-daemon.log) ─────────────
import logging

log = logging.getLogger("pidog.service")
log.setLevel(logging.INFO)
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[pidog] %(levelname)s: %(message)s"))
    log.addHandler(_handler)
    log.propagate = False


# ── Helpers PID / lockfile ──────────────────────────────────────────────────
def _read_lock_pid():
    try:
        with open(LOCK_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def _pid_is_daemon(pid):
    """Évite de confondre un PID recyclé avec notre daemon."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        return "pidog_service_daemon.py" in cmdline
    except Exception:
        return False


def _daemon_pid():
    pid = _read_lock_pid()
    if pid and _pid_alive(pid) and _pid_is_daemon(pid):
        return pid
    return None


def _socket_exists():
    return os.path.exists(SOCKET_PATH)


def _remove_stale_files():
    """Supprime socket/lock uniquement si aucun daemon vivant ne les possède."""
    if _daemon_pid() is None:
        for path in (SOCKET_PATH, LOCK_FILE):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ── Protocole JSON (4 octets de longueur + payload) ─────────────────────────
def _send_json(sock, obj):
    payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def _recv_json(sock):
    try:
        hdr = sock.recv(4)
        if not hdr:
            return None
        length = int.from_bytes(hdr, "big")
        if length <= 0 or length > 1024 * 1024:
            return None
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _jsonable(value):
    """Convertit les types numpy / objets exotiques en JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    for attr in ("tolist", "item"):
        try:
            return _jsonable(getattr(value, attr)())
        except Exception:
            pass
    return str(value)


# ── Client RPC (utilisé par TOUS les processus clients) ─────────────────────
class PidogUnavailable(RuntimeError):
    """Le daemon n'est pas joignable ou n'est pas prêt."""


class _RemoteMethod:
    """Attribut distant : dog.head_move(...) ou dog.rgb_strip.set_mode(...).

    ``__getattr__`` étend le chemin (namespace), ``__call__`` exécute le RPC.
    """

    def __init__(self, client, path):
        self._client = client
        self._path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _RemoteMethod(self._client, f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        return self._client.call(self._path, args=args, kwargs=kwargs)

    def __repr__(self):
        return f"<PiDog remote method {self._path}>"


class DogClient:
    """Proxy léger vers le robot du daemon.

    Une connexion est ouverte par requête : si le daemon redémarre, le client
    suivant retrouve automatiquement le nouveau socket (pas d'état bloqué).
    """

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path

    def _request(self, obj, timeout=30.0):
        payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            try:
                sock.connect(self.socket_path)
            except FileNotFoundError:
                raise PidogUnavailable(
                    "daemon PiDog non démarré (socket absent) — lancez `pidog start`"
                )
            except ConnectionRefusedError:
                raise PidogUnavailable(
                    "daemon PiDog arrêté (socket orphelin) — lancez `pidog start`"
                )
            except (TimeoutError, socket.timeout):
                raise PidogUnavailable("daemon PiDog ne répond pas (timeout connexion)")
            sock.sendall(len(payload).to_bytes(4, "big") + payload)
            reply = _recv_json(sock)
            if reply is None:
                raise PidogUnavailable("daemon PiDog a fermé la connexion sans réponse")
            return reply
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def ping(self, timeout=0.8):
        return self._request({"cmd": "ping"}, timeout=timeout)

    def status(self, timeout=1.0):
        return self._request({"cmd": "status"}, timeout=timeout)

    def park(self, timeout=15.0):
        return self._request({"cmd": "park"}, timeout=timeout)

    def call(self, path, args=(), kwargs=None, timeout=30.0):
        reply = self._request(
            {
                "cmd": "call",
                "path": path,
                "args": _jsonable(list(args)),
                "kwargs": _jsonable(kwargs or {}),
            },
            timeout=timeout,
        )
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or f"appel {path} refusé par le daemon")
        return reply.get("result")

    # Méthodes explicites (les plus utilisées)
    def do_action(self, action_name, step_count=1, speed=50, pitch_comp=0):
        return self.call(
            "do_action",
            args=(action_name,),
            kwargs={"step_count": step_count, "speed": speed, "pitch_comp": pitch_comp},
        )

    def head_move(self, target_yrps, roll_comp=0, pitch_comp=0, immediately=True, speed=50):
        return self.call(
            "head_move",
            args=(target_yrps,),
            kwargs={
                "roll_comp": roll_comp,
                "pitch_comp": pitch_comp,
                "immediately": immediately,
                "speed": speed,
            },
        )

    def head_move_raw(self, target_angles, immediately=True, speed=50):
        return self.call(
            "head_move_raw",
            args=(target_angles,),
            kwargs={"immediately": immediately, "speed": speed},
        )

    def action(self, action_name, timeout=90.0, **params):
        """Exécute une action de haut niveau et SÛRE côté daemon.

        Exemples : ``dog.action("posture", name="stand")``,
        ``dog.action("led", color="rainbow")``,
        ``dog.action("lift_paw", leg="front_left")``.
        """
        return self.call("action", args=(action_name,), kwargs=params,
                         timeout=timeout)

    def head_stop(self):
        return self.call("head_stop")

    def stable(self):
        return self.call("stable")

    def wait_all_done(self):
        return self.call("wait_all_done", timeout=60.0)

    def get_battery_voltage(self):
        return self.call("get_battery_voltage")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # Attributs/méthodes : renvoie un objet appelable distant.
        # Permet aussi dog.rgb_strip.set_mode(...) → path "rgb_strip.set_mode".
        return _RemoteMethod(self, name)

    def __repr__(self):
        return f"<DogClient {self.socket_path}>"


_client = DogClient()


def my_dog():
    """Proxy RPC vers le robot du daemon (ne démarre rien)."""
    return _client


def get_pidog():
    """Alias historique de my_dog()."""
    return _client


def is_ready():
    """True si le daemon répond ET que le robot est initialisé."""
    try:
        reply = _client.ping(timeout=0.8)
        return bool(reply.get("ready"))
    except Exception:
        return False


def service_state(timeout=1.0):
    """État structuré, sans jamais lever d'exception."""
    try:
        reply = _client.status(timeout=timeout)
        reply.setdefault("state", "unknown")
        reply.setdefault("ready", False)
        reply.setdefault("ok", True)
        return reply
    except PidogUnavailable as exc:
        return {"ok": False, "state": "stopped", "ready": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "state": "error", "ready": False, "error": str(exc)}


def park():
    """Chien couché, tête centrée, LED éteinte — sans arrêter le daemon."""
    try:
        return _client.park(timeout=15.0).get("ok", False)
    except Exception:
        return False


def release_pidog():
    """Compatibilité : parque le robot. Ne tue JAMAIS le daemon partagé."""
    return park()


# ── Serveur daemon (un seul par machine) ────────────────────────────────────
# Chemins de méthodes autorisés. Toute autre méthode est refusée.
_ALLOWED_CALLS = {
    "action",
    "do_action",
    "head_move",
    "head_move_raw",
    "head_stop",
    "stable",
    "tail_move",
    "legs_move",
    "legs_simple_move",
    "body_stop",
    "stop_and_lie",
    "wait_all_done",
    "wait_head_done",
    "wait_legs_done",
    "wait_tail_done",
    "get_battery_voltage",
    "read_distance",
    "speak",
    "close_all_thread",
    "rgb_strip.set_mode",
    "rgb_strip.close",
    "music.sound_play",
    "ears.isdetected",
    "ears.read",
}

_HEAD_SHORTCUTS = {
    "head_down": [[0, -25, 0]],
    "head_up": [[0, 25, 0]],
    "head_left": [[-30, 0, 0]],
    "head_right": [[30, 0, 0]],
}


class DogService:
    """Daemon : possède le socket, le lock et l'unique instance Pidog."""

    def __init__(self):
        self.dog = None
        self.actions = None              # RobotActions (couche sûre, voir _init_dog)
        self.state = "stopped"           # stopped | starting | ready | error
        self.last_error = None
        self.started_at = None
        self.running = False
        self.sock = None
        self.stop_event = threading.Event()
        self.init_lock = threading.Lock()
        self.dog_lock = threading.RLock()
        self._lock_fd = None
        self._init_thread = None

    # ----------------------------- démarrage -----------------------------
    def run(self, auto_start_robot=True):
        """Point d'entrée du processus daemon. Retourne un code de sortie."""
        # 1) Un autre daemon vivant ? On refuse de créer un second Pidog().
        other = _daemon_pid()
        if other and other != os.getpid():
            log.error("Un daemon PiDog est déjà actif (PID %s) — abandon.", other)
            return 2

        # 2) Verrou exclusif (flock) : empêche deux daemons même en course.
        try:
            self._lock_fd = open(LOCK_FILE, "a+")
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.seek(0)
            self._lock_fd.truncate()
            self._lock_fd.write(str(os.getpid()))
            self._lock_fd.flush()
        except OSError:
            log.error("Verrou %s déjà détenu — un daemon tourne déjà.", LOCK_FILE)
            return 2
        except Exception as exc:
            log.error("Impossible d'écrire le lock %s : %s", LOCK_FILE, exc)

        # 3) Signaux : arrêt propre (chien couché, child ultrasonic, socket).
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        self.running = True
        self.started_at = time.time()
        self.state = "starting"

        # 4) Socket AVANT l'init robot : les clients peuvent interroger l'état
        #    ("starting") au lieu de tomber sur un socket absent.
        try:
            try:
                if os.path.exists(SOCKET_PATH):
                    os.remove(SOCKET_PATH)
            except Exception:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(SOCKET_PATH)
            os.chmod(SOCKET_PATH, 0o660)
            self.sock.listen(8)
            self.sock.settimeout(1.0)
            log.info("socket prêt sur %s", SOCKET_PATH)
        except Exception as exc:
            log.error("impossible de créer le socket %s : %s", SOCKET_PATH, exc)
            self._cleanup_files()
            return 1

        threading.Thread(target=self._accept_loop, name="pidog-accept", daemon=True).start()

        # 5) Init robot dans UN thread unique (jamais depuis un client).
        if auto_start_robot:
            self._start_init_thread()
        else:
            self.state = "ready"

        log.info("daemon démarré (PID %s), état=%s", os.getpid(), self.state)
        while not self.stop_event.wait(0.5):
            pass

        self._shutdown()
        return 0

    def _on_signal(self, signum, frame):
        log.info("signal %s reçu — arrêt demandé", signum)
        self.stop_event.set()

    def request_shutdown(self):
        self.stop_event.set()

    # ----------------------------- init robot ----------------------------
    def _start_init_thread(self, name="pidog-init"):
        self._init_thread = threading.Thread(target=self._init_dog, name=name, daemon=True)
        self._init_thread.start()

    def _init_dog(self):
        with self.init_lock:
            if self.dog is not None:
                self.state = "ready"
                return
            self.state = "starting"
            self.last_error = None
            from pidog import Pidog  # import local : coûteux (numpy/robot_hat)

            for attempt in range(1, 4):
                if self.stop_event.is_set():
                    return
                try:
                    log.info("initialisation Pidog (tentative %d/3)...", attempt)
                    dog = Pidog()
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.error("échec init PiDog tentative %d/3 : %s", attempt, exc)
                    log.debug("traceback init", exc_info=True)
                    if attempt < 3:
                        time.sleep(1.0)
                    continue

                if self.stop_event.is_set():
                    # Arrêt demandé pendant l'init : on rend immédiatement le
                    # matériel proprement (threads + process ultrason).
                    self.dog = dog
                    self._shutdown_dog(dog)
                    self.dog = None
                    return

                self.dog = dog
                try:
                    from pidog.robot_actions import RobotActions
                    self.actions = RobotActions(dog)
                except Exception as exc:
                    # Le robot reste utilisable en bas niveau, mais les
                    # actions de haut niveau seront indisponibles.
                    self.actions = None
                    log.error("couche d'actions indisponible : %s", exc)
                self.state = "ready"
                log.info("robot PiDog initialisé et prêt")
                return

            self.state = "error"
            log.error("robot indisponible après 3 tentatives : %s", self.last_error)

    # ----------------------------- socket loop ---------------------------
    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except Exception:
                if not self.stop_event.is_set():
                    log.debug("erreur accept", exc_info=True)
                break
            threading.Thread(
                target=self._handle_client, args=(conn,), name="pidog-client", daemon=True
            ).start()

    def _handle_client(self, conn):
        try:
            while not self.stop_event.is_set():
                obj = _recv_json(conn)
                if obj is None:
                    break
                reply = self._dispatch(obj)
                _send_json(conn, reply)
                if obj.get("cmd") == "shutdown" and reply.get("ok"):
                    self.stop_event.set()
                    break
        except Exception:
            log.debug("client déconnecté", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, obj):
        cmd = obj.get("cmd")
        if cmd == "ping":
            return {
                "ok": True,
                "state": self.state,
                "ready": self.state == "ready" and self.dog is not None,
                "pid": os.getpid(),
            }
        if cmd == "status":
            return {
                "ok": True,
                "state": self.state,
                "ready": self.state == "ready" and self.dog is not None,
                "pid": os.getpid(),
                "robot": self.dog is not None,
                "error": self.last_error,
                "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
            }
        if cmd == "version":
            try:
                from pidog.version import __version__
                return {"ok": True, "version": __version__}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        if cmd == "shutdown":
            return {"ok": True, "state": self.state}
        if cmd == "park":
            return self._park_locked()
        if cmd == "retry_init":
            if self.dog is not None:
                return {"ok": True, "state": "ready"}
            if self.state == "starting":
                return {"ok": True, "state": "starting"}
            self.state = "starting"
            self.last_error = None
            self._start_init_thread(name="pidog-init-retry")
            return {"ok": True, "state": "starting"}
        if cmd == "release":  # ancien protocole : parquer, pas arrêter
            return self._park_locked()
        if cmd == "call":
            return self._handle_call(obj)

        # Ancien protocole : {"cmd": "lie"}, {"cmd": "head.move", ...}
        return self._handle_legacy(cmd, obj)

    def _not_ready(self):
        return {
            "ok": False,
            "state": self.state,
            "error": self.last_error or "robot non initialisé",
            "hint": "daemon actif mais robot indisponible ; consultez /tmp/pidog-daemon.log",
        }

    def _has_action(self, name):
        """ActionDict n'implémente pas __contains__ : tester via __getitem__."""
        try:
            self.dog.actions_dict[name]
            return True
        except Exception:
            return False

    def _handle_call(self, obj):
        if self.dog is None or self.state != "ready":
            return self._not_ready()
        path = obj.get("path") or ""
        args = obj.get("args") or []
        kwargs = obj.get("kwargs") or {}

        # Alias historiques : dog.command('stand') et do_action('head_left').
        if path == "command":
            if not args:
                return {"ok": False, "error": "command() sans nom d'action"}
            return self._handle_legacy(str(args[0]), {})

        # ── Couche d'actions de haut niveau : dog.action("led", color=...) ──
        if path == "action":
            if not args:
                return {"ok": False, "error": "action() sans nom d'action"}
            if self.actions is None:
                return {"ok": False, "error": "couche d'actions indisponible"}
            name = args[0]
            params = dict(kwargs or {})
            if isinstance(name, dict):
                payload = dict(name)
                name = payload.pop("name", payload.pop("action", ""))
                params.update(payload)
            try:
                with self.dog_lock:
                    result = self.actions.dispatch(str(name), **params)
                return {"ok": True, "result": _jsonable(result)}
            except Exception as exc:
                log.error("échec action %s : %s", name, exc)
                log.debug("traceback action", exc_info=True)
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # ── Accès direct aux méthodes de la couche sûre : actions.posture() ──
        if path.startswith("actions."):
            method = path.split(".", 1)[1]
            if not method or method.startswith("_") or self.actions is None:
                return {"ok": False, "error": f"méthode non autorisée : {path}"}
            target = getattr(self.actions, method, None)
            if target is None or not callable(target):
                return {"ok": False, "error": f"méthode non autorisée : {path}"}
            try:
                with self.dog_lock:
                    result = target(*args, **kwargs)
                return {"ok": True, "result": _jsonable(result)}
            except Exception as exc:
                log.error("échec appel %s : %s", path, exc)
                log.debug("traceback appel", exc_info=True)
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if path == "do_action" and args and args[0] in _HEAD_SHORTCUTS:
            return self._handle_legacy(str(args[0]), {})
        if path == "do_action":
            if not args:
                return {"ok": False, "error": "do_action() sans nom d'action"}
            name = str(args[0])
            if self.actions is not None:
                # Toute posture/preset passe par la couche sûre : palier assis
                # automatique et garde-fou de tension.
                try:
                    with self.dog_lock:
                        result = self.actions.do_action(name, **kwargs)
                    return {"ok": True, "result": _jsonable(result)}
                except Exception as exc:
                    log.error("échec do_action %s : %s", name, exc)
                    log.debug("traceback do_action", exc_info=True)
                    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not self._has_action(name):
                return {"ok": False, "error": f"action inconnue : {name}"}

        if path not in _ALLOWED_CALLS:
            return {"ok": False, "error": f"méthode non autorisée : {path}"}
        try:
            target = self.dog
            for part in path.split("."):
                target = getattr(target, part)
            with self.dog_lock:
                result = target(*args, **kwargs)
            return {"ok": True, "result": _jsonable(result)}
        except Exception as exc:
            log.error("échec appel %s : %s", path, exc)
            log.debug("traceback appel", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _handle_legacy(self, cmd, obj):
        """Ancien format : une seule exécution, jamais deux."""
        if self.dog is None or self.state != "ready":
            return self._not_ready()
        try:
            with self.dog_lock:
                if cmd in _HEAD_SHORTCUTS:
                    self.dog.head_move(_HEAD_SHORTCUTS[cmd], immediately=True, speed=50)
                elif cmd == "head.move":
                    payload = obj.get("payload", [])
                    self.dog.head_move(
                        payload,
                        immediately=obj.get("immediately", True),
                        speed=obj.get("speed", 50),
                    )
                elif cmd == "head.move.raw":
                    payload = obj.get("payload", [])
                    self.dog.head_move_raw(
                        payload,
                        immediately=obj.get("immediately", True),
                        speed=obj.get("speed", 50),
                    )
                elif cmd == "head.stop":
                    self.dog.head_stop()
                elif cmd == "stable":
                    self.dog.stable()
                elif cmd == "rgb":
                    self.dog.rgb_strip.set_mode(
                        style=obj.get("style", "listen"),
                        color=obj.get("color", "black"),
                        brightness=obj.get("brightness", 0),
                    )
                elif cmd == "reset_mcu":
                    from robot_hat import utils
                    utils.reset_mcu()
                elif cmd == "version":
                    from pidog.version import __version__
                    return {"ok": True, "version": __version__}
                elif self._has_action(cmd):
                    if self.actions is not None:
                        self.actions.do_action(cmd)
                    else:
                        self.dog.do_action(cmd)
                else:
                    return {"ok": False, "error": f"commande inconnue : {cmd}"}
            log.info("commande exécutée : %s", cmd)
            return {"ok": True}
        except Exception as exc:
            log.error("échec commande %s : %s", cmd, exc)
            log.debug("traceback commande", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _park_locked(self):
        if self.dog is None:
            return {"ok": False, "error": "robot non initialisé"}
        try:
            with self.dog_lock:
                try:
                    if self.actions is not None:
                        self.actions.posture("lie", speed=40)
                    else:
                        self.dog.do_action("lie", speed=40)
                        self.dog.wait_all_done()
                except Exception:
                    pass
                try:
                    self.dog.head_move([[0, 0, 0]], immediately=True, speed=30)
                except Exception:
                    pass
                try:
                    self.dog.stable()
                except Exception:
                    pass
                try:
                    self.dog.rgb_strip.set_mode(style="listen", color="black", brightness=0)
                except Exception:
                    pass
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----------------------------- arrêt --------------------------------
    def _shutdown(self):
        if not self.running:
            return
        log.info("arrêt du daemon : robot couché, threads et GPIO libérés...")
        self.running = False
        self.state = "stopped"

        # Si l'init est encore en cours, la laisser finir/se terminer avant de
        # libérer le matériel (sinon Pidog() peut continuer dans un thread).
        init_thread = self._init_thread
        if init_thread is not None and init_thread.is_alive():
            init_thread.join(timeout=12.0)

        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass

        dog = self.dog
        self.dog = None
        self.actions = None
        if dog is not None:
            self._shutdown_dog(dog)

        self._cleanup_files()
        log.info("daemon arrêté proprement")

    def _shutdown_dog(self, dog):
        try:
            with self.dog_lock:
                try:
                    dog.do_action("lie", speed=40)
                    dog.wait_all_done()
                except Exception:
                    pass
                try:
                    dog.head_move([[0, 0, 0]], immediately=True, speed=30)
                    dog.stable()
                except Exception:
                    pass
                try:
                    dog.rgb_strip.set_mode(style="listen", color="black", brightness=0)
                except Exception:
                    pass
                try:
                    dog.close_all_thread()
                except Exception:
                    pass

            for name in ("legs_thread", "head_thread", "tail_thread", "imu_thread"):
                thread = getattr(dog, name, None)
                if thread is not None:
                    try:
                        thread.join(timeout=2.0)
                    except Exception:
                        pass
            rgb_thread = getattr(dog, "rgb_strip_thread", None)
            if rgb_thread is not None:
                try:
                    dog.rgb_thread_run = False
                    rgb_thread.join(timeout=2.0)
                except Exception:
                    pass
            # Le process multiprocessing "sensory_process" (ultrason) doit être
            # tué explicitement, sinon il survit au daemon et garde le GPIO.
            proc = getattr(dog, "sensory_process", None)
            if proc is not None:
                try:
                    proc.terminate()
                    proc.join(timeout=3.0)
                    if proc.is_alive():
                        proc.kill()
                except Exception:
                    pass
            try:
                dog.rgb_strip.close()
            except Exception:
                pass
        except Exception:
            log.debug("nettoyage robot incomplet", exc_info=True)

    def _cleanup_files(self):
        try:
            if os.path.exists(SOCKET_PATH):
                os.remove(SOCKET_PATH)
        except Exception:
            pass
        try:
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self._lock_fd.close()
                self._lock_fd = None
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass


# Instance utilisée par le processus daemon. Les clients n'y touchent pas.
_server = DogService()


def run_daemon(auto_start_robot=True):
    """Entrée du script pidog_service_daemon.py."""
    return _server.run(auto_start_robot=auto_start_robot)


# ── Commandes publiques ─────────────────────────────────────────────────────
def _wait_until_ready(timeout=30.0):
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        state = service_state(timeout=1.0)
        if state.get("state") != last_state:
            last_state = state.get("state")
            log.debug("état daemon : %s", last_state)
        if state.get("ready"):
            return True, state
        if state.get("state") == "error":
            return False, state
        time.sleep(0.25)
    return False, service_state(timeout=1.0)


def start(auto_start_robot=True):
    """Lance le daemon si nécessaire et attend que le robot soit prêt.

    Retourne True si le robot est utilisable, False sinon.
    """
    state = service_state(timeout=0.8)
    if state.get("ready"):
        print("[pidog start] Daemon déjà actif et robot prêt.", flush=True)
        return True

    pid = _daemon_pid()
    if pid and state.get("state") in ("starting", "ready"):
        print(f"[pidog start] Daemon déjà actif (PID {pid}) — attente du robot...", flush=True)
        ok, state = _wait_until_ready()
        if ok:
            print("[pidog start] Daemon prêt (robot initialisé).", flush=True)
            return True
        print(
            f"[pidog start] Robot indisponible : {state.get('error') or state.get('state')}",
            flush=True,
        )
        return False

    if pid and state.get("state") == "error":
        print(
            f"[pidog start] Daemon en erreur ({state.get('error')}) — redémarrage.",
            flush=True,
        )
        stop()
    elif pid and state.get("state") == "stopped":
        # Socket muet mais process vivant : on le termine avant de relancer.
        print(f"[pidog start] Daemon PID {pid} ne répond pas — redémarrage.", flush=True)
        stop()

    _remove_stale_files()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    try:
        log_file = open(DAEMON_LOG, "ab")
    except Exception:
        log_file = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, DAEMON_SCRIPT],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        if hasattr(log_file, "close"):
            try:
                log_file.close()
            except Exception:
                pass

    print(f"[pidog start] Daemon lancé (journal : {DAEMON_LOG}).", flush=True)
    ok, state = _wait_until_ready()
    if ok:
        print("[pidog start] Daemon prêt (robot initialisé).", flush=True)
        return True

    print(
        f"[pidog start] Échec : {state.get('error') or state.get('state')}. "
        f"Voir {DAEMON_LOG}.",
        flush=True,
    )
    return False


def stop():
    """Arrête le daemon partagé et libère proprement le robot/GPIO."""
    state = service_state(timeout=0.8)
    pid = _daemon_pid()

    # 1) Arrêt propre via le socket (le daemon couche le chien et termine le
    #    process ultrason avant de rendre le GPIO).
    if state.get("ok") and state.get("state") != "stopped":
        try:
            _client._request({"cmd": "shutdown"}, timeout=5.0)
        except Exception:
            pass

    # 2) Fallback signal si le socket ne répond pas (jamais soi-même).
    if pid and pid != os.getpid() and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        for _ in range(40):
            if not _pid_alive(pid):
                break
            time.sleep(0.25)
        if _pid_alive(pid):
            log.warning("le daemon PID %s ne s'arrête pas — SIGKILL", pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    # 3) Si le daemon est géré par systemd, on le stoppe aussi.
    try:
        subprocess.run(
            ["systemctl", "stop", "pidog.service"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass

    _remove_stale_files()
    if in_process_server_running():
        _server.request_shutdown()
    log.info("pidog stop : service arrêté.")
    return True


def in_process_server_running():
    return bool(_server.running)


def status():
    """État lisible : distingue daemon absent / init / prêt / erreur."""
    pid = _daemon_pid()
    state = service_state(timeout=1.0)
    name = state.get("state")

    if name == "ready":
        return f"running (daemon prêt, robot initialisé, PID {state.get('pid', pid)})"
    if name == "starting":
        return f"starting (daemon PID {state.get('pid', pid)}, initialisation du robot en cours)"
    if name == "error":
        return f"error (daemon PID {state.get('pid', pid)}, robot indisponible : {state.get('error')})"
    if pid:
        return f"running (daemon PID {pid}, socket ne répond pas)"
    if _socket_exists():
        return "stopped (socket orphelin — lancez `pidog start`)"
    return "stopped"
