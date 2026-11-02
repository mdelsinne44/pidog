# PiDog Assistant
- Pi 4 2Go, Robot HAT, mic 360° (ALSA), BT/HDMI/Jack sortie (volume control)
- Un seul propriétaire du robot : le daemon `pidog start` (socket `/tmp/pidog.sock`)
- CLI : `pidog agent cli` ; trace détaillée : `pidog agent cli --debug`
- Stream vocal : `pidog agent stream` (ou `--voice-only` sans robot)
- État : `pidog status` ; arrêt : `pidog stop` (ou `pidog stop --stream`)
- Stt: Vosk local pour la veille ; commande via Groq Whisper si `STT_BACKEND=groq|auto`
  (repli automatique Vosk), ou 100 % Vosk avec `STT_BACKEND=vosk`
- LLM: Groq (FR, actions) — `LLM_MODEL` dans `.env`
- TTS: Piper local fr_FR
- Actions: RetroPie/EmuStation, radio (Kodi), meteo, chien (postures, tête,
  queue, LED, pattes, marche, presets SunFounder) via `dog.action(...)`
- Sécurité: palier `sit` obligatoire entre couché et debout, vitesse douce,
  refus des mouvements lourds si la tension batterie est trop basse
- Émotions: Groq écrit `[JOIE]`, `[TRISTE]`, `[COLERE]`, `[SURPRISE]`,
  `[CURIEUX]`, `[EXCITE]`, `[FATIGUE]`, `[CALME]`, `[NEUTRE]` dans sa
  réponse ; `pidog_stream.py` retire ces balises du texte parlé et les
  traduit en mouvements (tête, queue, posture, son, LED) + prosodie Piper.
- Watchdog 30s → reset PiDog + libère RAM/process ; réveil : "PiDog" ou 2 claps
- Discussion continue : après une réponse du LLM, le micro se rouvre tout seul
  (12 s) → on enchaîne sans redire « PiDog » ; silence ou « au revoir » → veille.
  Les commandes physiques restent one-shot (« PiDog, assis »).
- Réparer haut-parleur intégré → le mettre défaut ALSA; BT temporaire

Usage: copier .env (GROQ_KEY, GOOGLE_API_KEY), adapter input_device_index après arecord -l, pip install -r requirements.txt, python assistant_core.py
