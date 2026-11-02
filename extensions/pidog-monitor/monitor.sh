#!/bin/bash
# Moniteur tension (diagnostic coupures / brown-outs) - exécuté une fois par systemd (oneshot)

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

# Adresse réseau au démarrage : utile pour savoir si le Wi-Fi etait deja mort
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
# après un delai. A t=0 les services ne sont pas encore montés -> le check
# disait "INACTIF" à tous les coups, pour rien. Et il ne doit surtout pas
# retarder la boucle de surveillance tension, qui doit tourner dès la
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

# Lancer la vérification une seule fois (pas de boucle)
health_check