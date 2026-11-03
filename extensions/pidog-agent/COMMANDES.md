# Commandes PiDog — CLI agent, voix, RetroPie & Kodi

## Service / CLI

- `pidog start` → lance l'unique daemon robot (socket `/tmp/pidog.sock`)
- `pidog status` → état du daemon / robot
- `pidog agent cli` → agent de dev à outils (voir plus bas)
- `pidog stop` → arrête tout (chien couché, GPIO libéré)

## Agent CLI — outils (function calling)

Le CLI ne devine pas : il **lit** puis répond. Chaque affirmation sur l'état
matériel doit venir d'un outil exécuté dans le tour.

```
vous › donne-moi l'état du robot, des services et la température
  ⤷ robot_state({})            ✓ ok (380 ms)
  ⤷ sys_info({})               ✓ 44.8 (22 ms)
  ⤷ sys_services({})           ✓ ok (106 ms)
État du robot : daemon arrêté… | Température : 44,8 °C (pas de throttling)
```

Règles appliquées par les outils (pas par le prompt) :

- `service_ctl` refuse `start/stop/restart` sur `pidog` et
  `pidog-agent-stream` sans `explicit=true` — donc pas de démarrage surprise.
- `shell` est en **lecture seule** : liste blanche de binaires et de
  sous-commandes (`systemctl status`, `journalctl`, `git log`,
  `pactl`, `vcgencmd`, …), pas de `sudo`, pas de `|`, `>` ni `$`.
  Passer `PIDOG_SHELL_MODE=full` pour tout autoriser (assumé).
- `robot_action` refuse `reboot`/`shutdown` : demande explicite requise.

Commandes de session :

| Commande | Effet |
|---|---|
| `/state` | état robot + services (2 outils) |
| `/actions` | catalogue d'actions physiques |
| `/mem <texte>` | recherche mémoire (BM25 + récence) |
| `/remember clé = valeur` | ajoute un fait durable |
| `/forget <clé>` | désactive une entrée |
| `/stats` | stats mémoire + modèle + taille d'historique |
| `/model [nom]` | affiche/change le modèle Groq |
| `/reset` | vide l'historique de conversation |
| `/debug` | bascule la trace détaillée |
| `/quit` | quitter |

## Le mot « PiDog » à la voix

À la voix, « PiDog, … » annonce une commande destinée au chien. Le LLM doit
**déduire** l'action derrière la formulation, même imagée, et l'écrire dans un
bloc `<<<CODE>>>` :

- « PiDog, vas-y allume tes guirlandes » → `dog.action("led", color="rainbow")`
- « PiDog, lève la papatte gauche » → `dog.action("lift_paw", leg="front_left")`
- « PiDog, remue la queue » → `dog.action("wag", times=4)`
- « PiDog, hoche la tête » → `dog.action("head_gesture", name="nod")`
- « PiDog, assis / debout / couché » → `dog.action("posture", name="sit"|"stand"|"lie")`

Sans le préfixe, le message reste une discussion (le LLM peut néanmoins agir
s'il le juge utile).

## Catalogue d'actions (bloc `<<<CODE>>>`)

Tout passe par `dog.action(...)`, une couche **sûre** côté daemon :

| Domaine | Exemples |
|---|---|
| Posture | `dog.action("posture", name="stand"|"sit"|"lie"|"half_sit")` |
| Tête | `dog.action("head", yaw=20, pitch=10, speed=30)` |
| Gestes tête | `nod`, `shake`, `tilt_left`, `tilt_right`, `look_left`, `look_right`, `look_up`, `look_down`, `center` |
| Queue | `dog.action("tail", angle=25)`, `dog.action("wag", times=4, amplitude=30)` |
| LED / guirlandes | `dog.action("led", color="rainbow", style="breath", brightness=0.7)` |
| Sons | `dog.action("sound", name="bark"|"growl"|"happy"|"sad"|"howl"|"pant")` |
| Pattes | `dog.action("lift_paw", leg="front_left"|"front_right")`, `wave_paw`, `handshake`, `high_five`, `scratch` |
| Locomotion | `dog.action("walk", direction="forward", steps=1)`, `turn`, `trot` |
| Autres | `dog.action("stretch")`, `push_up`, `howl`, `celebrate`, `stop`, `battery`, `rest` |

Les presets SunFounder restent disponibles en bas niveau :
`dog.do_action("stand"|"sit"|"lie"|"stretch"|"push_up"|"wag_tail"|"shake_head"|…)`.
Ils sont routés par la même couche de sécurité.

## Sécurité batterie / servomoteurs

- **Transitions de posture** : `lie → stand` et `stand → lie` passent
  TOUJOURS par `sit`, avec une vitesse douce et une pause. Le daemon applique
  ce palier même si le LLM appelle `dog.do_action("stand")` directement.
- **Tension** : avant un mouvement lourd, la tension est lue. En dessous de
  `PIDOG_VOLTAGE_MIN` (défaut 6,45 V), l'action est refusée avec un message
  clair au lieu de risquer un brown-out. `PIDOG_VOLTAGE_WARN` (6,90 V)
  déclenche un ralentissement.
- **Mouvements** : vitesses plafonnées pour la tête (qui vibre), pas
  d'enchaînement de positions contradictoires.

## Routage des commandes locales (voix)

Ces commandes ne passent pas par le LLM :

| Intention | Déclencheur (regex, frontières de mots) |
|---|---|
| Stop musique | `arrête`, `stop`, `coupe`, `silence` |
| Volume bas | `volume|son|audio` **et** `bas|baisse|moins|réduis|diminue|descend` |
| Volume haut | `volume|son|audio` **et** `haut|augmente|monte|fort|max` |
| Météo | `météo`, `weather`, `quel temps` |
| Média | Kodi / RetroPie / TV / radio (FIP, NOVA) |
| Fin de discussion | `au revoir`, `à plus`, `bonne nuit`, `bye`, `c'est tout`, `terminé` |

« un peu » ou « en même temps » ne déclenchent plus rien : c'était la cause des
baisses de volume et des météos intempestives.

## RetroPie (console rétro)

- "PiDog, lance la console" → retropie_start
- "PiDog, arrête la console" → retropie_stop
- "PiDog, lance le jeu Super Mario" → retropie_game_cmd(game="super_mario")
- "PiDog, liste mes jeux" → retropie_list_cmd
- "PiDog, quitte RetroPie" → retropie_stop

## Kodi (média / TV)

- "PiDog, démarre Kodi" → kodi_start
- "PiDog, quitte Kodi" → kodi_stop
- "PiDog, lance la TV" → kodi_tv_on + kodi_start
- "PiDog, éteins la TV" → kodi_tv_off
- "PiDog, mets la lecture" → kodi_video_cmd / kodi_pause
- "PiDog, pause" → kodi_pause
- "PiDog, allume le salon" → kodi_start + kodi_video_cmd

## STT (voix)

- La veille « PiDog » reste 100 % locale (Vosk).
- `STT_BACKEND=auto` (défaut) : si `GROQ_KEY` est présente, la phrase de
  commande est ré-transcrite par Groq Whisper (`WHISPER_MODEL`, défaut
  `whisper-large-v3-turbo`), avec repli automatique sur Vosk si le réseau
  échoue. `STT_BACKEND=vosk` force le 100 % local, `STT_BACKEND=groq` force
  Whisper.

## Quota Groq (429)

Le tier gratuit plafonne à 8 000 tokens/minute **par modèle**. Le CLI comme la
voix gèrent le 429 : attente du délai annoncé, une ou deux reprises, puis
bascule sur `LLM_FALLBACKS`. Message explicite si le quota reste bloqué.
