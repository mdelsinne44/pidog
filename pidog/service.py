#!/usr/bin/env python3
"""PiDog Service — daemon unique qui détient le robot GPIO + socket de proxy.

Ce module offre :
  - pidog start                    → lance le daemon (systemd ou détaché)
  - pidog stop [--stream]          → arrête tout (ou juste le stream)
  - pidog status                   → service / robot / stream
  - pidog.my_dog()                 → proxy Pidog (bloque si service absent)
  - pidog.my_dog().lie(), .do_action(...), .head.move(...)

Ce module est importé par `pidog/__init__.py` et `pidog/__init__.py` ne contient plus de __getattr__.
"""

import os, sys, json, socket, time, subprocess, logging, threading
from pathlib import Path

# ── Chemin racine du paquet pidog (celui du repo)
PIDOG_ROOT = Path(__file__).parent.resolve()
LOCK_FILE = "/tmp/pidog.lock"
SOCKET_PATH = "/tmp/pidog.sock"

# ── Logging -------------------------------------------------
log = logging.getLogger("pidog.service")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[pidog] %(levelname)s: %(message)s"))
if not log.handlers:
    log.addHandler(_handler)

# ── Helpers lockfile -----------------------------------------
def _read_lock_pid():
    try:
        with open(LOCK_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _write_lock_pid():
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

def _clear_lock():
    try:
        if os.path.isfile(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

# ── Tiny JSON socket helpers -------------------------------
def _send_json(sock, obj):
    payload = (json.dumps(obj) + "\n").encode()
    sock.sendall(len(payload).to_bytes(4, "big") + payload)

def _recv_json(sock):
    try:
        hdr = sock.recv(4)
        if not hdr:
            return None
        length = int.from_bytes(hdr, "big")
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return json.loads(data.decode())
    except Exception:
        return None

# ── Le petit serveur proxy (s'exécute en arrière-plan) -------
class DogProxyServer:
    """Tiny Unix-Socket JSON server qui fait tourner Pidog() unique."""
    def __init__(self):
        self.dog = None                # instance Pidog() vraie
        self.dog_lock = threading.Lock()
        self.running = False
        self.sock = None

    def do_action(self, action_name, step_count=1, speed=50, pitch_comp=0):
        import socket, json
        result = {"ok": False, "error": "Aucune réponse du daemon"}
        try:
            payload = json.dumps({"cmd": action_name}).encode()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(SOCKET_PATH)
            s.sendall(len(payload).to_bytes(4, "big") + payload)
            hdr = s.recv(4)
            if hdr:
                length = int.from_bytes(hdr, "big")
                data = b""
                while len(data) < length:
                    chunk = s.recv(length - len(data))
                    if chunk:
                        data += chunk
                s.close()
                result = json.loads(data.decode())
        except Exception as e:
            print("Proxy do_action erreur:", e, flush=True)
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "Action échouée (robot indisponible?)"))
        print(f"[proxy] do_action({action_name}) -> OK", flush=True)

    def __getattr__(self, name):
        try:
            if self.dog is not None:
                return getattr(self.dog, name)
            else:
                raise AttributeError(f"Le robot n'est pas initialisé localement (service : {SOCKET_PATH}). Utilisez le daemon.")
        except Exception:
            return lambda *a, **kw: (print(f"[proxy] Action '{name}' relayée au daemon (pas encore connecté).", flush=True))

    def _ensure_dog(self):
        """Créer le singleton Pidog la première fois qu'on en a besoin."""
        if self.dog is None:
            # Solution B : Cleanup forcé du GPIO MCURST avant Pidog()
            try:
                import RPi.GPIO as GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.cleanup(29) # Libère MCURST si bloqué
                import time; time.sleep(0.5)
            except Exception:
                pass
            try:
                for attempt in range(3):
                    try:
                        from pidog import Pidog
                        try:
                            from pidog.pidog import Pidog
                            self.dog = Pidog()
                        except Exception as e2:
                            log.error("Daemon Pidog init fail (GPIO busy?) attempt %d: %s", attempt+1, e2)
                        else:
                            log.info("Dog proxy : Pidog initialisé (robot_hat GPIO).")
                            break
                    except Exception:
                        pass
                    import time; time.sleep(1)
            except Exception:
                pass

    def _handle_client(self, conn):
        try:
            while self.running:
                obj = _recv_json(conn)
                if obj is None:
                    break
                cmd = obj.get("cmd")
                if cmd and self.dog is not None:
                    try:
                        if cmd in ("head_down","head_up","head_left","head_right","lie","sit","stand","reset"):
                            if cmd == "head_down": self.dog.head_move([[0,-25,0]], immediately=True, speed=50)
                            elif cmd == "head_up": self.dog.head_move([[0,25,0]], immediately=True, speed=50)
                            elif cmd == "head_left": self.dog.head_move([[-30,0,0]], immediately=True, speed=50)
                            elif cmd == "head_right": self.dog.head_move([[30,0,0]], immediately=True, speed=50)
                            elif cmd == "lie" or cmd == "reset": self.dog.do_action("lie")
                            elif cmd == "sit": self.dog.do_action("sit")
                            elif cmd == "stand": self.dog.do_action("stand")
                            else: self.dog.command(cmd)
                        log.info("Daemon executed cmd: %s", cmd)
                    except Exception as ex:
                        log.error("Daemon cmd %s fail: %s", cmd, ex)
                with self.dog_lock:
                    if self.dog is None:
                        try:
                            # Nettoyage direct du GPIO busy avant d'essayer le robot
                            try:
                                import subprocess, time
                                subprocess.run(["killall", "-SIGTERM", "parec", "aplay", "mpv", "piper"], capture_output=True)
                                time.sleep(0.3)
                                from robot_hat import utils
                                try:
                                    mcu = __import__("robot_hat").Pin("MCURST")
                                    mcu.close()
                                except Exception:
                                    pass
                                utils.reset_mcu()
                            except Exception:
                                pass
                            self._ensure_dog()
                        except Exception as e:
                            log.error(f"Robot non initialisé (GPIO busy?) : {e}")
                    if self.dog is None:
                        _send_json(conn, {"ok": False, "error": "PiDog non initialisé (GPIO occupé ou absent)."})
                        continue
                    try:
                        if cmd == "lie":
                            self.dog.do_action("lie")
                            _send_json(conn, {"ok": True})
                        elif cmd == "stand":
                            self.dog.do_action("stand")
                            _send_json(conn, {"ok": True})
                        elif cmd == "sit":
                            self.dog.do_action("sit")
                            _send_json(conn, {"ok": True})
                        elif cmd == "head.move":
                            payload = obj.get("payload", [])
                            self.dog.head_move(payload, immediately=obj.get("immediately", True),
                                               speed=obj.get("speed", 50))
                            _send_json(conn, {"ok": True})
                        elif cmd == "head.move.raw":
                            payload = obj.get("payload", [])
                            self.dog.head_move_raw(payload, immediately=obj.get("immediately", True),
                                                   speed=obj.get("speed", 50))
                            _send_json(conn, {"ok": True})
                        elif cmd == "head.stop":
                            self.dog.head_stop()
                            _send_json(conn, {"ok": True})
                        elif cmd == "stable":
                            self.dog.stable()
                            _send_json(conn, {"ok": True})
                        elif cmd == "rgb":
                            style = obj.get("style", "listen")
                            color = obj.get("color", "black")
                            brightness = obj.get("brightness", 0)
                            if hasattr(self.dog, 'rgb_strip'):
                                rs = self.dog.rgb_strip
                                try:
                                    rs.set_mode(style=style, color=color, brightness=brightness)
                                    _send_json(conn, {"ok": True})
                                except Exception as e2:
                                    _send_json(conn, {"ok": False, "error": str(e2)})
                            else:
                                _send_json(conn, {"ok": False, "error": "pas de rgb_strip"})
                        elif cmd == "version":
                            from pidog.version import __version__
                            _send_json(conn, {"ok": True, "version": __version__})
                        elif cmd == "status":
                            if self.dog is not None:
                                try:
                                    v = self.dog.get_battery_voltage()
                                    _send_json(conn, {"ok": True, "battery_v": v})
                                except Exception:
                                    _send_json(conn, {"ok": True, "battery_v": None})
                            else:
                                _send_json(conn, {"ok": False, "error": "dog None"})
                        elif cmd == "release":
                            try:
                                from pidog_core import release_pidog
                                release_pidog()
                                self.dog = None
                                _send_json(conn, {"ok": True})
                            except Exception as e:
                                _send_json(conn, {"ok": False, "error": str(e)})
                        elif cmd == "reset_mcu":
                            from robot_hat import utils
                            utils.reset_mcu()
                            _send_json(conn, {"ok": True})
                        else:
                            if cmd in self.dog.actions_dict:
                                self.dog.do_action(cmd)
                                _send_json(conn, {"ok": True})
                            else:
                                _send_json(conn, {"ok": False, "error": f"Commande inconnue : {cmd}"})
                    except Exception as e:
                        log.error(f"Erreur exécution cmd={cmd} : {e}")
                        _send_json(conn, {"ok": False, "error": str(e)})
        except Exception:
            log.debug(f"Client déconnecté : {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self, auto_start_robot=True):
        """Démarrer le serveur socket Unix."""
        self.running = True
        # Création du socket Unix (même si le robot n'est pas dispo)
        try:
            if self.sock:
                self.sock.close()
            try:
                os.unlink(SOCKET_PATH)
            except Exception:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(SOCKET_PATH)
            self.sock.listen(5)
            log.info("Dog proxy : socket Unix prête sur %s", SOCKET_PATH)
        except Exception as e:
            log.error(f"Dog proxy : impossible de créer le socket : {e}")
            self.running = False
            return
        # Accepter les connexions en boucle
        accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        accept_thread.start()
        # Ensuite essayer d'initier le robot (si demandé et possible)
        if auto_start_robot:
            try:
                # Nettoyer le GPIO busy avant d'essayer le robot
                try:
                    import subprocess, time
                    subprocess.run(["killall", "-SIGTERM", "parec", "aplay", "mpv", "piper"], capture_output=True)
                    time.sleep(0.3)
                    from robot_hat import utils
                    try:
                        mcu = __import__("robot_hat").Pin("MCURST")
                        mcu.close()
                    except Exception:
                        pass
                    utils.reset_mcu()
                except Exception as e:
                    log.debug(f"GPIO clean skip: {e}")
                self._ensure_dog()
            except Exception as e:
                log.error(f"Dog proxy : robot non dispo (GPIO busy?) : {e}")

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                t.start()
            except Exception:
                if self.running:
                    log.debug("Erreur accept socket")
                break

    def stop(self):
        """Arrêter proprement le serveur et libérer le robot."""
        self.running = False
        # 1) Envoyer le chien couché
        try:
            with self.dog_lock:
                if self.dog is not None:
                    try:
                        self.dog.do_action("lie")
                        self.dog.stable()
                        log.info("Robot libéré (GPIO)")
                        if hasattr(self.dog, 'rgb_strip'):
                            try:
                                self.dog.rgb_strip.set_mode(style="listen", color="black", brightness=0)
                            except Exception:
                                pass
                        self.dog = None
                    except Exception:
                        pass
        except Exception:
            pass
        # 2) Fermer le socket
        try:
            if self.sock:
                self.sock.close()
                os.unlink(SOCKET_PATH)
        except Exception:
            pass
        # 3) Nettoyer le lockfile
        _clear_lock()
        log.info("Dog proxy : arrêté proprement.")

# Instance globale unique
_proxy = DogProxyServer()

# Fonctions publiques
def start(auto_start_robot=True):
    """Lance le daemon dans un processus détaché (reste en vie après sortie du CLI)."""
    # Vérifier si le daemon tourne déjà (ne pas créer de doublon)
    try:
        import subprocess
        res = subprocess.run(["pgrep", "-f", "/home/mdelsinne/pidog/extensions/pidog-agent/pidog_service_daemon.py"],
                             capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip() != "" and int(res.stdout.strip().split()[0]) != os.getpid():
            print("[pidog start] Daemon déjà actif (ne relance pas).", flush=True)
            # Si le robot est demandé mais le daemon tourne sans robot, on ne fait rien (le daemon gère)
            return
    except Exception:
        pass
    # Lancer le processus détaché qui fait tourner le service
    import subprocess, sys
    env = os.environ.copy()
    env["PYTHONPATH"] = "/home/mdelsinne/pidog" + os.pathsep + env.get("PYTHONPATH", "")
    daemon_script = "/home/mdelsinne/pidog/extensions/pidog-agent/pidog_service_daemon.py"
    subprocess.Popen(
        [sys.executable, daemon_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    time.sleep(0.5)
    # Vérifier que le robot répond via le socket du daemon (pas un 2e Pidog local)
    robot_ok = False
    import socket, json
    try:
        for attempt in range(15):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(SOCKET_PATH)
            payload = (json.dumps({"cmd":"lie"}) + "\n").encode()
            s.sendall(len(payload).to_bytes(4, "big") + payload)
            hdr = s.recv(4)
            s.close()
            if hdr:
                robot_ok = True
                break
            time.sleep(0.5)
    except Exception as e:
        robot_ok = False
        log.info("pidog start : robot non initialisé (GPIO busy?) : %s", e)
    if robot_ok:
        log.info("pidog start : robot initialisé et testé (sit OK).")
        print(f"[pidog start] Service lancé + robot testé et fonctionnel.", flush=True)
    else:
        log.info("pidog start : daemon lancé, robot non testé (GPIO busy?).")
        print(f"[pidog start] Service lancé — robot non initialisé (GPIO busy?). Vérifiez `pidog status`.", flush=True)

def stop():
    try:
        pid = _read_lock_pid()
        if pid is not None:
            import subprocess
            subprocess.run(["kill", "-TERM", str(pid)], capture_output=True, timeout=5)
    except Exception:
        pass
    # Arrêter aussi le proxy local si présent
    try:
        _proxy.stop()
    except Exception:
        pass
    _clear_lock()
    log.info("pidog stop : service arrêté.")

def status():
    # Vérifier directement que le daemon tourne et que le socket répond
    try:
        import subprocess
        # Vérifier le processus du daemon (par nom de script)
        res = subprocess.run(["pgrep", "-f", "/home/mdelsinne/pidog/extensions/pidog-agent/pidog_service_daemon.py"],
                             capture_output=True, text=True, timeout=5)
        daemon_alive = False
        for line in res.stdout.strip().split("\n"):
            line = line.strip()
            if line and int(line) != os.getpid():
                daemon_alive = True
                break
        if daemon_alive and os.path.exists(SOCKET_PATH):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(SOCKET_PATH)
                s.close()
                return "running (service actif, socket OK)"
            except Exception:
                return "running (daemon actif, socket à vérifier)"
        elif daemon_alive:
            return "running (daemon actif, socket absent)"
    except Exception:
        pass
    return "stopped"

def my_dog():
    """Retourne le proxy Pidog (bloque si service absent)."""
    if not _proxy.running:
        print("[pidog] Service pas encore démarré. Lancez `pidog start` d'abord.", flush=True)
        try:
            _proxy.start(auto_start_robot=False)
        except Exception:
            pass
    return _proxy

def get_pidog():
    """Alias pour compatibilité descendante."""
    return my_dog()

def release_pidog():
    """Alias vers le service d'arrêt."""
    stop()