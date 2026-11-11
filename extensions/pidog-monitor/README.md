# PiDog Monitor

Diagnostic de santé de la Raspberry, avec **réparation graduée**. Ce n'est pas
un superviseur de processus : il constate, il alerte, et il ne répare que dans
un périmètre fermé, budgété et vérifié.

## Ce qu'il ne fait pas (par choix)

* il **ne démarre ni `pidog.service` ni `pidog-stream.service`** — il ne lit que
  leur état (`systemctl is-active`). La voix reste lancée à la main ou par son
  propre service ;
* il **ne modifie aucun fichier de configuration** (`.env`, Netplan, SSH…) ;
* il **n'exécute jamais de shell libre** : les actions sont une whitelist de
  verbes fixes, sans paramètre arbitraire (pas d'injection via un SSID ou un log) ;
* il **ne redémarre pas en boucle** : pas de `Restart=` sur le oneshot, budget
  de reboots, anti-boucle par uptime.

## Déclenchement

| Quand | Détail |
|---|---|
| Au boot | `pidog-monitor.timer`, `OnBootSec=2min` |
| Périodique | `OnUnitActiveSec=15min` (±30 s, jitter 15 s) |
| À la demande | `systemctl start pidog-monitor.service`, `monitor.sh report` |

Le service n'a **pas** de `[Install] WantedBy=multi-user.target` : seule l'unité
`.timer` s'installe. L'activer aussi au boot provoquait deux exécutions.

Modes :

```bash
monitor.sh check          # diagnostic + réparations (ce que fait le timer)
monitor.sh report         # diagnostic seul, rapport complet
monitor.sh json           # diagnostic seul, JSON sur stdout (watchdog externe)
monitor.sh repair reboot  # une action de la whitelist (debug)
```

`MON_DRY_RUN=1` journalise les actions sans les exécuter.

## Contrôles

| Contrôle | Comment | Sévérité |
|---|---|---|
| `ssh`, `NetworkManager` | `systemctl is-active` | critique |
| `tailscaled`, `pidog-stream` (vidéo) | `systemctl is-active` | informatif |
| `pidog.service` | état | informatif (« arrêté attendu ») |
| `vcgencmd get_throttled` | bits 0-3 = maintenant (critique), bits 16-19 = historique | critique / info |
| Température CPU | `/sys/class/thermal` | critique ≥ 80 °C |
| Disque / RAM | `df`, `/proc/meminfo` | critique ≥ 92 % / < 80 Mo |
| NTP | `journalctl -u systemd-timesyncd` | informatif |
| IPv4 | `ip -4 -o addr` (hors `lo`) | critique |
| **Wi-Fi** | `nmcli -t -f DEVICE,STATE` → `wlan0 connected` | critique |
| **Passerelle** | `ping` la route par défaut | informatif |
| **WAN** | `ping` 1.1.1.1 / 8.8.8.8 | critique |
| **DNS** | `getent hosts github.com` | critique |
| **SSH** | bannière réelle sur `127.0.0.1:22` | critique |
| **Tailscale** | `tailscale status --json` → `BackendState=Running` | important |
| Clés LLM | présence dans `pidog-agent/.env` (hors ligne, 0 requête) | critique si aucune |
| Secours local | présence de `$LLM_LOCAL_DIR` (jamais chargé ici) | informatif |
| Socket robot | `/tmp/pidog.sock` | informatif |

Un service « actif » ne prouve rien : c'est pour ça que le Wi-Fi, le DNS, SSH et
Tailscale sont testés par leur **effet** (association, résolution, bannière,
état du backend), pas par `is-active`.

## Réparation graduée

Désactivable avec `MON_REPAIR=0`. Chaque action est suivie d'une **vérification**
(le domaine repasse-t-il à OK ?) et écrite dans `audit.tsv`.

1. **Déterministe, par domaine** :

   | Domaine KO | Échelle |
   |---|---|
   | Wi-Fi | `rfkill_unblock` (si soft-bloqué) → `wifi_reapply` → `wifi_up` (profil mémorisé) → `wifi_provision` (si `MON_WIFI_SSID`/`MON_WIFI_PSK` renseignés) → `nm_restart` |
   | WAN / DNS | `nm_restart` → `dns_restart` (si `systemd-resolved` existe) |
   | SSH | `ssh_restart` |
   | Tailscale | `tailscale_restart` → `tailscale_up` |

2. **IA de dernier recours** (`monitor_ai.py`), seulement si un domaine est
   encore KO après l'échelle :
   * réseau disponible → **OpenRouter → Gemini → Groq** (clés du `.env`) ;
   * plus de réseau → **modèle local** : `llama-cli` + un `*.gguf` dans
     `$LLM_LOCAL_DIR` (voir plus bas), sinon `transformers` + `config.json` ;
     absent = on le dit, l'échelle déterministe reste seule, et on s'arrête ;
   * le modèle choisit **une** action de la whitelist ci-dessus, avec une
     confiance ; `monitor.sh` revalide, applique le budget, et refuse
     `confidence < MON_AI_MIN_CONF` (0.7) ;
   * **`reboot` est interdit à l'IA** par défaut (`MON_AI_ALLOW_REBOOT=1` pour
     l'autoriser) ;
   * quota : 1 appel / 30 min, 6 / 24 h.

3. **Reboot en dernier ressort**, uniquement si la panne est **locale**
   (Wi-Fi, passerelle ou SSH KO) **et déjà constatée au passage précédent**
   (une première détection peut être transitoire : la réparation déterministe
   + l'IA ont un cycle complet avant d'envisager un redémarrage). Un WAN/DNS
   injoignable alors que la passerelle répond est une panne FAI : on ne
   redémarre pas la Pi pour ça, et un Tailscale seul ne suffit pas non plus.
   Conditions :
   * pas de sous-tension/throttling actif, pas de surchauffe ;
   * uptime ≥ 15 min (anti-boucle au démarrage) ;
   * ≥ 30 min depuis le dernier reboot, max 3 / 24 h.

**Tailscale — réauthentification** : si `BackendState=NeedsLogin`, `tailscale up`
seul ne peut pas aboutir (il faudrait coller une URL). Dépose une clé d'auth
**réutilisable** dans `~/.config/pidog/tailscale-authkey` (`chmod 600`) ; elle
est passée via `--auth-key=file:…`, donc jamais visible dans `ps`. Sans ce
fichier, le monitor signale l'action manuelle au lieu de boucler.

Budgets globaux : max 6 actions/heure, max 2 tentatives/heure par action
(reboot excepté). L'état et l'historique sont dans `/var/lib/pidog-monitor/`,
donc ils **survivent aux reboots** (contrairement à l'ancien `/tmp`).

## Secours LLM hors ligne

Installation (une fois) : `./install-local-llm.sh` — construit `llama-cli`,
télécharge le modèle, vérifie son SHA256 et fait un test à froid.

| Élément | Choix |
|---|---|
| Modèle | **Qwen2.5‑0.5B‑Instruct, GGUF Q4_K_M** (469 Mo) |
| Pourquoi lui | assez gros pour suivre un schéma JSON, assez petit pour tenir à côté du reste sur 2 Go |
| Runtime | `llama-cli` (llama.cpp), libs copiées dans `~/.local/lib/pidog-llama` |
| Emplacement | `~/models/llm-local/qwen2.5-0.5b-instruct-q4_k_m.gguf` |
| Intégrité | SHA256 `74a4da8c…d7a9db` vérifié à l'installation |
| Sortie | **grammaire GBNF** : JSON valide garanti, `action` limitée à la whitelist |
| Mémoire | `RLIMIT_AS` à 1300 Mo sur le process enfant ; ~600–800 Mo de RSS en pratique |
| Vitesse mesurée | ~40 s par décision (~7 tok/s en génération, ~12 tok/s en préremplissage) |
| Chargement | **paresseux** : seulement quand `online=0`, dans un process jetable, jamais résident |
| Absent | le monitor le signale (`llm:local absent`) et continue sans IA |

Pourquoi pas ONNX : le runtime est bien présent sur la machine, mais générer
avec un décodeur ONNX oblige à gérer le KV cache à la main, sans sampler ni
grammaire, et reste plus lent sur ARM64. llama.cpp apporte le mmap, le KV
cache, la grammaire et les optimisations NEON dans un seul binaire.

Budget : au pire 6 décisions locales par 24 h (`MON_AI_MAX_DAY`), donc le
modèle n'est chargé que quelques fois par jour, et seulement en panne réseau.

## Journalisation

* rapport complet dans le journal systemd **et** `~/mon-boot.log` ;
* bavard au boot (uptime < 10 min), sur changement de signature, après une
  action, ou en mode `report` ; silencieux sinon ;
* `audit.tsv` : `epoch <TAB> action <TAB> ok <TAB> source <TAB> détail` ;
* `ai.log` : erreurs/diagnostics du helper IA.

Codes de sortie : `0` OK, `1` critique (visible dans `systemctl --failed`),
`2` dégradé **seulement** si `MON_STRICT=1`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `monitor.sh` | contrôles, budget, échelle de réparation, rapport |
| `monitor_ai.py` | dernier recours IA (cloud/local), sortie JSON ou `--machine` |
| `pidog-monitor.service` | oneshot, `TimeoutStartSec=300`, pas de `Restart=` |
| `pidog-monitor.timer` | boot + 2 min, puis toutes les 15 min |
| `install.sh` | installation idempotente (+ nettoyage du symlink mort) |
| `install-local-llm.sh` | construit `llama-cli` et télécharge le modèle GGUF |
| `tests/test_monitor.sh` | 26 assertions hors ligne (faux PATH, dry-run) |

## Installation

```bash
sudo ./install.sh
```

Le script crée `/var/lib/pidog-monitor`, copie les unités, supprime le symlink
mort `pidog-agent.service` s'il existe, désactive l'exécution du service au
boot (le timer suffit) et lance un premier diagnostic.

## Vérifier

```bash
systemctl status pidog-monitor.timer
systemctl show pidog-monitor -p NRestarts       # doit rester à 0
systemctl --failed
tail -20 ~/mon-boot.log
./tests/test_monitor.sh                          # 26 assertions (monitor)
./tests/test_local_ai.sh                         # 22 assertions (IA locale)
./monitor.sh json | python3 -m json.tool         # état machine
```

## Réglages (variables d'environnement)

| Variable | Défaut | Effet |
|---|---|---|
| `MON_STATE_DIR` | `/var/lib/pidog-monitor` | état, audit, budget |
| `MON_BOOT_LOG` | `~/mon-boot.log` | journal lisible |
| `MON_DRY_RUN` | `0` | simule les actions |
| `MON_REPAIR` | `1` | active l'échelle déterministe |
| `MON_REBOOT` | `1` | autorise le reboot de dernier recours |
| `MON_STRICT` | `0` | sortie 2 si dégradé |
| `MON_WIFI_DEV` / `MON_WIFI_PROFILE` | `wlan0` / mémorisé | cible Wi-Fi |
| `MON_WIFI_SSID` / `MON_WIFI_PSK` | vide | repli `wifi_provision` (recrée le profil) — dans `~/.config/pidog/monitor.env` (`chmod 600`) |
| `MON_PING_HOSTS` | `1.1.1.1 8.8.8.8` | test WAN |
| `MON_DNS_HOST` | `github.com` | test DNS |
| `MON_SSH_CHECK` | `banner` | `banner`, `port` ou `off` |
| `MON_AI` | `1` | active le dernier recours IA |
| `MON_AI_PROVIDERS` | `openrouter,gemini,groq` | ordre cloud |
| `MON_AI_MIN_CONF` | `0.7` | seuil de confiance |
| `MON_AI_COOLDOWN` / `MON_AI_MAX_DAY` | `1800` / `6` | quotas IA |
| `MON_AI_ALLOW_REBOOT` | `0` | reboot autorisé à l'IA |
| `MON_TAILSCALE_AUTHKEY_FILE` | `~/.config/pidog/tailscale-authkey` | clé d'auth Tailscale réutilisable (`chmod 600`) |
| `MON_MAX_ACTIONS_HOUR` / `MON_PER_ACTION_HOUR` | `6` / `2` | budgets |
| `MON_REBOOT_MIN_INTERVAL` / `MON_REBOOT_MIN_UPTIME` / `MON_REBOOT_MAX_DAY` | `1800` / `900` / `3` | garde-fous reboot |
| `LLM_LOCAL_DIR` | `~/models/llm-local` | modèle de secours hors ligne |
| `MON_LOCAL_MEM_MB` | `1300` | plafond d'adressage de `llama-cli` (anti-OOM) |
| `MON_LOCAL_THREADS` / `MON_LOCAL_CTX` / `MON_LOCAL_N` | `3` / `2048` / `200` | réglages llama.cpp |
| `MON_LOCAL_TIMEOUT` | `120` | durée max d'une décision locale (s) |

## Alerte quand il n'y a plus de réseau

Aucun cloud ne peut prévenir quand la connexion est coupée : le monitor écrit
donc localement (journal, `mon-boot.log`, `audit.tsv`) et sort en `1` si un
domaine critique reste KO. Pour une alerte **externe**, faire pinger l'IP
Tailscale (ou le port SSH) par une autre machine : l'absence de réponse est le
signal, pas un log. `monitor.sh json` est prévu pour ça.
