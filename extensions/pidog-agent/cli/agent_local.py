#!/usr/bin/env python3
"""Agent local PiDog - reparation hors ligne, dernier recours.

Modele pre-telecharge : ~/models/llm-local (SmolLM2-135M-Instruct).
Charge UNIQUEMENT si pas de connexion (local_files_only=True).
Sinon : regles simples.
"""
import os
import subprocess
import logging

MODEL_DIR = os.environ.get('LLM_LOCAL_DIR', '/home/mdelsinne/models/llm-local')
logging.basicConfig(level=logging.INFO, format='[AGENT] %(message)s')
log = logging.info

RULES = [
    ('Bluetooth', 'sudo systemctl restart bluetooth'),
    ('PulseAudio', 'pulseaudio --start || true'),
    ('Wifi', 'sudo ip link set wlan0 up || true'),
    ('SSH', 'sudo systemctl restart ssh'),
    ('Assistant', 'sudo systemctl restart pidog-agent.service'),
]

def has_internet(wait=3):
    try:
        r = subprocess.run(['ping', '-c', '1', '-W', str(wait), '8.8.8.8'],
                           capture_output=True, timeout=wait + 3)
        return r.returncode == 0
    except Exception:
        return False


def load_model():
    if not os.path.isfile(os.path.join(MODEL_DIR, 'config.json')):
        log('Modele absent (%s) -> regles simples' % MODEL_DIR)
        return None
    if has_internet():
        log('Connexion presente -> pas de chargement du LLM (regles simples)')
        return None
    try:
        import transformers
        log('Chargement hors ligne de %s (RAM 1.8 Go, patience)...' % MODEL_DIR)
        return transformers.pipeline(
            'text-generation',
            model=MODEL_DIR, tokenizer=MODEL_DIR,
            local_files_only=True)
    except Exception as e:
        log('Chargement impossible (%s) -> regles simples' % e)
        return None


SAFE = ('systemctl', 'service', 'ip link', 'ip addr', 'pulseaudio',
        'rfkill', 'bluetoothctl', 'pactl')


def ask_llm(pipe):
    q = ('Repare ce Pi qui a un probleme reseau/audio/SSH. '
         'Reponds par UNE commande bash seule.')
    try:
        out = pipe(q, max_new_tokens=60, do_sample=False)
        txt = out[0]['generated_text']
        rep = txt[len(q):].strip().splitlines()
        rep = rep[0].strip() if rep else ''
        return rep
    except Exception as e:
        log('LLM en echec (%s)' % e)
        return ''


def safe(cmd):
    if not cmd or len(cmd) > 200:
        return False
    if any(x in cmd for x in ('rm ', 'dd ', 'mkfs', 'reboot', 'shutdown', '> /')):
        return False
    return any(cmd.startswith(s) or (' ' + s) in cmd for s in SAFE)

def repair():
    pipe = load_model()
    done = False
    if pipe is not None:
        cmd = ask_llm(pipe)
        if safe(cmd):
            log('LLM propose: %s' % cmd)
            try:
                subprocess.run(cmd, shell=True, timeout=20, capture_output=True)
                done = True
            except Exception as e:
                log('Echec commande LLM: %s' % e)
        else:
            log('Reponse LLM ignoree (non sure): %s' % cmd[:80])
        del pipe
        import gc
        gc.collect()
    if not done:
        for name, cmd in RULES:
            log('Regle %s -> %s' % (name, cmd))
            try:
                subprocess.run(cmd, shell=True, timeout=15, capture_output=True)
            except Exception as e:
                log('Echec %s: %s' % (name, e))
    log('Reparation terminee.')


if __name__ == '__main__':
    repair()
