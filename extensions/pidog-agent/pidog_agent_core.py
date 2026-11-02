import os
import threading
from pidog import Pidog
from time import sleep

dog_singleton = None
_singleton_lock = threading.Lock()
LOCK_FILE = "/tmp/pidog.lock"

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

def get_pidog():
    global dog_singleton
    with _singleton_lock:
        # Partage réel : si verrou pris par autre processus, pas dispo
        other_pid = _read_lock_pid()
        if other_pid is not None and other_pid != os.getpid():
            import subprocess, time
            # Vérifier que le PID est toujours vivant
            try:
                subprocess.run(["kill", "-0", str(other_pid)], capture_output=True, check=True)
                raise RuntimeError(f"Pidog déjà occupé par PID {other_pid} — pas disponible")
            except Exception:
                # PID mort → verrou obsolète, on le supprime
                _clear_lock()
        if dog_singleton is None:
            dog_singleton = Pidog()
            _write_lock_pid()
        return dog_singleton

def release_pidog():
    """Ferme le bus I2C/SMBus, libère le GPIO MCURST et met le singleton à None."""
    global dog_singleton
    with _singleton_lock:
        # Libérer le GPIO MCURST (robot_hat) — mais JAMAIS de deinit() :
        # deinit() ferme pin_factory (gpiozero) et cassait toute construction
        # Pin()/Pidog() ultérieure dans ce process (« GPIO busy » au boot,
        # ou crash NoneType & int dans l'IMU SH3001).
        try:
            from robot_hat import Pin
            mcu = Pin("MCURST")
            mcu.close()
        except Exception:
            pass
        if dog_singleton is not None:
            try:
                # Libération du bus SMBus utilisé par la LED (pidog rgb_strip)
                if hasattr(dog_singleton, 'rgb_strip'):
                    rgb = dog_singleton.rgb_strip
                    rgb.close()  # éteint la LED
                    if hasattr(rgb, 'bus') and rgb.bus is not None:
                        try:
                            rgb.bus.close()
                        except Exception:
                            pass
                # Certains modules internes utilisent SMBus directement
                if hasattr(dog_singleton, 'legs_thread') and dog_singleton.exit_flag is not False:
                    try:
                        dog_singleton.close_all_thread()
                    except Exception:
                        pass
            except Exception:
                pass
            dog_singleton = None

def stop_service():
    """Arrêt complet du service PiDog : audio coupé, GPIO libéré, RAM libérée,
    zombies GPIO tués, robot couché, healthcheck tué (interne, pas voice/cli).
    Crée un flag STOPPED pour que le healthcheck ne relance pas l'agent après."""
    import subprocess, time, gc, sys, os, signal
    print("[STOP SERVICE] Arrêt du service PiDog...", flush=True)
    # Créer le flag STOPPED (le healthcheck le relit au prochain cycle)
    STOPPED_FLAG = "/home/mdelsinne/pidog/extensions/pidog-agent/.agent_stopped"
    try:
        Path(STOPPED_FLAG).touch()
    except Exception:
        pass
    # 1) Audio
    for proc in ("parec", "aplay", "mpv", "piper", "python3"):
        subprocess.run(["killall", "-SIGTERM", proc], capture_output=True)
    # Healthcheck interne : purgé le 01/11/2026 (pidog-health / moniteur-pidog
    # supprimés) — plus rien à arrêter ici. Le moniteur tension
    # (pidog-monitor.service) reste actif par construction.
    time.sleep(0.5)
    # 2) Zombies / concurrents pidog_stream + GPIO zombies
    try:
        my_pid = os.getpid()
        # Assistant stream duplicates
        result = subprocess.run(["pgrep", "-f", "pidog_stream.py"],
                                capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and int(line) != my_pid:
                try:
                    subprocess.run(["kill", "-SIGTERM", line], capture_output=True, timeout=2)
                    # Force kill if not terminated after a short wait
                    subprocess.run(["kill", "-9", line], capture_output=True, timeout=2)
                except Exception:
                    pass
        # PIDOG related processes (sound, sounddirection, gpiozero, robot_hat)
        zresult = subprocess.run(["pgrep", "-f", "pidog|soundsign|gpiozero|robot_hat"],
                                 capture_output=True, text=True)
        for line in zresult.stdout.strip().split("\n"):
            line = line.strip()
            if line and int(line) != my_pid and line:
                try:
                    subprocess.run(["killall", "-SIGKILL", line], capture_output=True, timeout=2)
                except Exception:
                    pass
        # Additional: find any stray python3 processes that are hanging (ghost zombies)
        # Use ps to find processes running python with our script names, not our current PID
        stray_res = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True)
        for psline in stray_res.stdout.splitlines()[1:]:
            parts = psline.strip().split(None, 1)
            if len(parts) == 2:
                pid_str, args = parts
                try:
                    pid = int(pid_str)
                    if pid != my_pid and ("python3" in args or "python" in args):
                        # Check if it's a PID that's already dead but still in ps (zombie)
                        # but we can't reliably know if it's a zombie here, so we just kill 
                        # suspect processes that have "pidog_stream" or "pidog" in args
                        if any(keyword in args for keyword in ("pidog_stream", "pidog", "soundsign", "memory", "env_vision")):
                            subprocess.run(["kill", "-9", pid_str], capture_output=True, timeout=2)
                except Exception:
                    pass
    except Exception:
        pass
    # 3) Services principaux (l'agent vocal s'appelle pidog-agent.service
    #    depuis la migration du 01/11/2026 ; l'ancien pidog-stream est purgé)
    try:
        subprocess.run(["systemctl", "stop", "pidog-agent"], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(["systemctl", "stop", "pidog"], capture_output=True, timeout=5)
    except Exception:
        pass
    # 4) Libérer GPIO / SMBus
    release_pidog()
    # 5) RAM / GC
    try:
        gc.collect()
    except Exception:
        pass
    # 6) Posture finale couché + LED off
    try:
        dog = get_pidog()
        if dog is not None:
            dog.do_action("lie")
            dog.head_move([[0, 0, 0]], immediately=True, speed=3)
            dog.stable()
            dog.rgb_strip.set_mode(style="listen", color="black", brightness=0)
            if hasattr(dog.rgb_strip, 'close'):
                dog.rgb_strip.close()
    except Exception:
        pass
    # 7) Nettoyer singleton
    try:
        release_pidog()
    except Exception:
        pass
    print("[STOP SERVICE] PiDog arrêté proprement. RAM/ GPIO libérés. Zombie GPIO tué si présent. Healthcheck tué.", flush=True)

def reboot():
    """Arrêt complet du service puis reboot système (libération GPIO, etc.)."""
    stop_service()
    import subprocess, time
    subprocess.run(["sudo", "reboot"], capture_output=True, timeout=10)

def shutdown():
    """Arrêt complet du service puis shutdown du système (libération GPIO, etc.)."""
    stop_service()
    import subprocess, time
    subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True, timeout=10)

def head_toward_sound():
    d = get_pidog()
    detected = False
    direction = 0
    for _ in range(30):
        if d.ears.isdetected():
            direction = d.ears.read()
            print(f"DEBUG sound_direction: direction={direction}")
            detected = True
            break
        sleep(0.1)
    if detected and direction >= 0:
        yaw = (direction - 160) % 360
        if yaw > 90: yaw = 90
        elif yaw < -90: yaw = -90
        d.head_move([[yaw, 0, 0]], immediately=True, speed=50)
        d.wait_all_done()
        d.stable()
        d.rgb_strip.set_mode(style='listen', color='yellow', brightness=1)
        print("DEBUG head_toward_sound: head moved toward sound")
        sleep(1.5)  # stabilisation de la tete (le verrou reel est robot_active)
    else:
        d.head_move([[10, 0, 0]], immediately=True, speed=50)
        d.wait_all_done()
        d.stable()
        d.rgb_strip.set_mode(style='listen', color='black')
        sleep(1.5)  # stabilisation
