# PiDog Assistant

Extension agent (CLI + voix) du PiDog. Deux canaux, un seul chien.

## Architecture

```
pidog.service      daemon unique : GPIO, socket /tmp/pidog.sock, RPC
   ├── agent cli      terminal / SSH : agent à outils, réponses techniques
   └── agent stream   voix : Vosk (veille) → LLM → Piper → JBL
memory.py          mémoire locale SQLite + FTS5 (RAG lexical)
tools.py           18 outils partagés (function calling)
tests/             non-régression : directions de tête, shell, exécution de code
pidog_prompt.py    prompts partagés : voice_prompt() / cli_prompt()
pidog-monitor      health-check systemd (oneshot + timer 15 min)
```

- Un seul propriétaire du matériel : le daemon `pidog start`. Le CLI et le
  stream ne créent jamais de `Pidog()` local, ils passent par RPC.
- `pidog status` → état ; `pidog stop` → arrêt (chien couché, GPIO libéré).

## CLI — agent de dev

`pidog agent cli` (ou `python3 cli/agent_cli.py`).

Le CLI n'est plus un chat à protocole texte : il appelle de vrais **outils**
(function calling Groq) et boucle observer → agir → vérifier (6 tours max).

| Outil | Usage |
|---|---|
| `robot_action` | posture, tête, LED, queue, patte, marche, sons, tension |
| `robot_state` | daemon, tension, audio/réseau/Bluetooth |
| `robot_actions` | catalogue et paramètres |
| `memory_search` / `memory_add` / `memory_forget` | mémoire persistante |
| `sys_info` | uptime, charge, RAM, disque, température, throttling |
| `sys_services` / `sys_logs` | états et journaux systemd (whitelist) |
| `service_ctl` | start/stop/restart — **seulement sur demande explicite** |
| `shell` | commandes dans `~/pidog` (pipes/`&&` acceptés, destructif refusé) |
| `run_python` | écrit et exécute un script Python (timeout 30 s) |
| `write_file` | crée/remplace un fichier sous `~/pidog` |
| `web_search` | recherche DuckDuckGo (titres, URLs, extraits) |
| `fetch_url` | lit une page http(s) (Wikipédia via API REST) |
| `weather`, `media`, `self_info` | météo, Kodi/RetroPie/TV/radio, auto-description |

Commandes de session : `/state /actions /mem <q> /web <q> /run <code>
/remember k = v /forget <clé> /stats /model [nom] /reset /debug /quit`.

### Modes `shell` (`PIDOG_SHELL_MODE`)

| Mode | Comportement |
|---|---|
| `readonly` | liste blanche historique, pas de `|`, `>`, `$` |
| `workspace` *(défaut)* | `bash -lc` dans `~/pidog` : pipes, `&&`, redirections ; `sudo`, `reboot`, `mkfs`, `rm -rf /`, `systemctl stop`… refusés |
| `full` | aucune vérification (assumé) |
| `off` | aucun accès shell/code |

`run_python` et `write_file` respectent `PIDOG_WORKSPACE` (défaut `~/pidog`).

### Directions de tête

Convention unique, vérifiée et testée (`tests/test_head_directions.py`) :

- `turn_head_left` / `look_left` → **yaw positif** (gauche du chien)
- `turn_head_right` / `look_right` → **yaw négatif** (droite)
- `tilt_left` / `tilt_right` → roll négatif / positif
- `head_up` / `head_down` → pitch positif / négatif (jamais du roll)

Les rotations horizontales passent par `head_move_raw([yaw, 0, 0])` : lacet
pur, sans mélange roll/pitch, donc plus de tête qui « penche » en tournant.

Options :

```bash
pidog agent cli --debug            # trace LLM + outils (cli/debug_trace.log)
pidog agent cli --no-stream        # réponse en un bloc (diagnostic)
pidog agent cli --model openai/gpt-oss-20b
```

## Mémoire (RAG)

`~/pidog/data/memory.db` (local, jamais sur le mount rclone).

- Table `memory` + index FTS5 (`unicode61 remove_diacritics 2`), triggers de
  synchronisation.
- `memory_search()` = BM25 + correspondance exacte + récence + importance.
- `memory_context()` = bloc borné (1100 car.) injecté dans le prompt ; le
  reste est récupéré à la demande par l'outil `memory_search`.
- Les dates relatives sont figées à l'écriture : « demain » → « demain
  (2026-11-04) ». Un fait ne devient donc pas faux le lendemain.
- Ménage : `python3 memory.py cleanup` (bruit + quasi-doublons),
  `python3 memory.py prune` (entrées anciennes non utilisées).
- Export JSON plat vers `~/GoogleDrive/memory-export.json` (pas de SQLite sur
  le mount FUSE).

## Modèles et quotas

`.env` :

```
LLM_MODEL_CLI=openai/gpt-oss-120b     # profondeur (réponses techniques)
LLM_MODEL_VOICE=qwen/qwen3.8-27b      # latence (~0,4 s)
LLM_FALLBACKS=openai/gpt-oss-20b      # repli automatique
PIDOG_MAX_TOKENS=800
PIDOG_MAX_TOOL_CHARS=1200
PIDOG_REASONING_EFFORT=low            # gpt-oss : coupe le « reasoning »
```

Groq gratuit = 8 000 TPM **par modèle**. Le CLI attend le délai annoncé et
réessaie (2×), puis bascule sur le modèle de repli. `reasoning_effort=low` est
indispensable sur gpt-oss : sans lui, le budget de sortie part en réflexion et
la réponse revient vide.

## Voix

`pidog agent stream` (ou `--voice-only` sans robot).

- Veille « PiDog » 100 % locale (Vosk) ; 2 claps possibles.
- `STT_BACKEND=auto|groq|vosk` — Whisper Groq pour la phrase de commande,
  repli Vosk.
- TTS Piper local `fr_FR` ; lecture JBL Flip 4 (HFP).
- Les balises d'humeur (`[JOIE]`, `[CALME]`, …) pilotent tête, queue, posture,
  LED et prosodie ; elles ne sont jamais prononcées.
- Une action s'écrit dans un bloc `<<<CODE>>>`, retiré du texte parlé.
- Routage local strict : le volume exige « volume » **et** une direction ;
  la météo exige « météo » ou « quel temps » ; la conversation ne se ferme que
  sur une vraie formule de congé.

## Monitor

`extensions/pidog-monitor/` — oneshot + timer, voir son README.
`systemctl show pidog-monitor -p NRestarts` doit rester à **0**.

## Installation

```bash
cp .env.example .env      # ou créer .env : GROQ_KEY, VOICE, VOSK_MODEL_DIR, PIPER_BIN
pip install -r requirements.txt
python3 memory.py         # crée/migre la base
```

Hors repo : `~/robot_guardian.py` (optionnel, chargé si présent).
