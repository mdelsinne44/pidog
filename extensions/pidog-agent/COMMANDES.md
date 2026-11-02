# Commandes vocales PiDog — RetroPie & Kodi

## Service / CLI
- `pidog start` → lance l'unique daemon robot (socket `/tmp/pidog.sock`)
- `pidog status` → état du daemon / robot
- `pidog agent cli --debug` → CLI texte avec trace des étapes
- `pidog stop` → arrête tout (chien couché, GPIO libéré)

## Le mot « PiDog »
En CLI comme à la voix, dire **« PiDog, … »** annonce une commande destinée au
chien. Le LLM doit alors **déduire** l'action derrière la formulation, même
imagée, et l'exécuter dans un bloc `<<<CODE>>>` :

- « PiDog, vas-y allume tes guirlandes » → `dog.action("led", color="rainbow")`
- « PiDog, lève la papatte gauche » → `dog.action("lift_paw", leg="front_left")`
- « PiDog, remue la queue » → `dog.action("wag", times=4)`
- « PiDog, hoche la tête » → `dog.action("head_gesture", name="nod")`
- « PiDog, assis / debout / couché » → `dog.action("posture", name="sit"|"stand"|"lie")`

Sans le préfixe « PiDog », le message reste une discussion ; le LLM peut
néanmoins exécuter une action s'il la juge nécessaire.

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

## Chien + général
- "PiDog, assis / debout / couché" → dog.action("posture", …)
- "PiDog, météo / radio FIP" → get_weather / play_radio
- "PiDog, redémarre" / "reboot" → reboot (redémarre le système / le robot)

## STT (voix)
- La veille « PiDog » reste 100 % locale (Vosk).
- `STT_BACKEND=auto` (défaut) : si `GROQ_KEY` est présente, la phrase de
  commande est ré-transcrite par Groq Whisper (`WHISPER_MODEL`, défaut
  `whisper-large-v3-turbo`), avec repli automatique sur Vosk si le réseau
  échoue. `STT_BACKEND=vosk` force le 100 % local, `STT_BACKEND=groq` force
  Whisper.
