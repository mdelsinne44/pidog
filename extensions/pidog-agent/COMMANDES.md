# Commandes vocales PiDog — RetroPie & Kodi

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
- "PiDog, assis / debout / couché" → dog_action
- "PiDog, météo / radio FIP" → get_weather / play_radio
- "PiDog, redémarre" / "reboot" → reboot (redémarre le système / le robot)
