#!/bin/bash
# Moniteur tension (diagnostic coupures / brown-outs) - lance par moniteur-pidog.service.
#
# Contexte : ce Pi a deja brown-outte (get_throttled=0x50000) quand les servos
# du robot-hat s'initialisent en I2C. Un brown-out coupe le courant au pire
# moment : le Pi redemarre mal et il faut eteindre physiquement. Ce moniteur
# rend la tension visible dans ~/mon-boot.log pour arreter de deviner.
#
# get_throttled (bits) :
#   0  = sous-tension en ce moment          (0x1)
#   1  = sous-tension passee                (0x2)
#   2  = frequence limitee passee           (0x4)
#   3  = limiteur thermique actif           (0x8)
#  16  = sous-tension forcee passee         (0x10000)
#  17  = frequence limiee en force passee   (0x20000)
#  18  = sous-tension + frequence en force  (0x40000)
# Ici on a vu 0x50000 = bit 16 + bit 18 (sous-tension forcee historiquee).

# Journal systemd (stdout) + historique ~/mon-boot.log (comme l'ancien
# moniteur-pidog). L'append n'est tenté que si le fichier est accessible :
# lancé à la main par mdelsinne il ne faut pas spammer « Permission denied ».
MON_BOOT_LOG="${MON_BOOT_LOG:-/home/mdelsinne/mon-boot.log}"
log() {
    local line
    line="$(date -Is) $*"
    echo "$line"
    if [ -w "$MON_BOOT_LOG" ] || { touch "$MON_BOOT_LOG" 2>/dev/null && [ -w "$MON_BOOT_LOG" ]; }; then
        echo "$line" >> "$MON_BOOT_LOG"
    fi
}

log "=== moniteur tension demarre (noyau $(uname -r), $(uptime -p 2>/dev/null)) ==="

# Adresse reseau au demarrage : utile pour savoir si le Wi-Fi etait deja mort
# avant la coupure.
for iface in $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}'); do
    ipaddr=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | head -1)
    log "iface $iface ${ipaddr:-sans-Adresse-IP}"
done

# Retracer les sous-tensions signalees par le noyau au dernier demarrage
# (dmesg du boot en cours = celui qui suit un eventuel brown-out).
dmesg 2>/dev/null | grep -iE 'under-?voltage|over-?temperature' | while read -r l; do
    log "kernel: $l"
done

# Health check : une ligne par service critique, pour qu'un boot foiré se
# voie d'un coup d'oeil dans mon-boot.log. Le 22/10, ssh et x11vnc sont morts
# au démarrage (limite de redémarrages atteinte) et rien ne le disait : la
# machine tournait sans GUI ni SSH pendant 12 h.
#
# ATTENTION (bug corrige le 22/10) : ce check doit partir EN ARRIERE-PLAN et
# apres un delai. A t=0 les services ne sont pas encore montes -> le check
# disait "INACTIF" a tous les coups, pour rien. Et il ne doit surtout pas
# retarder la boucle de surveillance tension, qui doit tourner des la
# seconde 1 (les servos s'initialisent en I2C au boot, c'est la ou ca
# brown-outte).
health_check() {
    # Laisser le temps aux services de se stabiliser + au Wi-Fi de prendre
    # une IP (mesure : ~80 s au boot du 22/10).
    sleep "${HEALTH_DELAY:-120}"

    local DOWN=""
    for svc in ssh lightdm x11vnc NetworkManager; do
        if systemctl -q is-active "$svc" 2>/dev/null; then
            log "sante $svc actif"
        else
            log "sante $svc INACTIF"
            DOWN="$DOWN $svc"
        fi
    done

    # Derniere synchro NTP : sans elle l'heure est fausse (le 22/10, un
    # decalage fake-hwclock de 39 min a rendu tous les timestamps trompeurs).
    local NTP
    NTP=$(journalctl -q -b -u systemd-timesyncd -g 'Initial clock synchronization' -o cat 2>/dev/null | tail -1)
    log "sante ntp ${NTP:-pas encore synchronise}"

    # Verdict en une ligne, c'est ce qu'on lira en cas de souci.
    if [ -n "$DOWN" ]; then
        log "=== BOOT FOIRE : services morts :$DOWN ==="
    else
        log "=== BOOT OK : tous les services critiques sont actifs ==="
    fi
}
health_check &

PREV=""
NET_PREV=""
while true; do
    # timeout 5 : si le firmware bloque (mail box GPU wedged), vcgencmd peut
    # rester bloqué indéfiniment -> le moniteur ne doit jamais perdre sa boucle.
    TH=$(timeout 5 vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    V=$(timeout 5 vcgencmd measure_volts 2>/dev/null | cut -d= -f2)
    if [ -z "$TH" ]; then
        log "ERREUR vcgencmd indisponible"
    elif [ "$TH" != "$PREV" ]; then
        # Changement d'etat = l'evenement interressant : bit pose apres un
        # brown-out, ou sous-tension en cours.
        log "ALERTE get_throttled ${PREV:-inconnu} -> $TH volts=$V"
        PREV="$TH"
    else
        log "ok throttled=$TH volts=$V"
    fi

    # Chien de garde réseau : le 22/10, wlan0 n'a eu aucune IP pendant 12 h
    # et rien ne le signalait. On ne logge que les CHANGEMENTS d'état, donc
    # zéro ligne en plus quand tout va bien.
    if ip -4 -o addr show dev wlan0 2>/dev/null | grep -q 'inet '; then
        NET="wlan0 up"
    elif ip -4 -o addr show dev eth0 2>/dev/null | grep -q 'inet '; then
        NET="eth0 up"
    else
        NET="sans-IP"
    fi
    if [ "$NET" != "$NET_PREV" ]; then
        log "ALERTE reseau ${NET_PREV:-inconnu} -> $NET"
        NET_PREV="$NET"
    fi

    sleep 60
done
