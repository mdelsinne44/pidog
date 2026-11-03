#!/bin/bash
# PiDog Monitor — diagnostic santé, idempotent et non bloquant.
#
# Historique : l'ancienne version faisait un `sleep 120` dans ExecStart, était
# relancée 567 fois en 20 h (Restart=on-failure sur un Type=oneshot) et ses
# ExecStartPost étaient mal parenthésés (`a && echo X || echo Y | logger` ne
# logge jamais X). Résultat : du bruit, aucune alerte fiable.
#
# Ici :
#   * aucune boucle de redémarrage : oneshot + timer, pas de Restart= ;
#   * le script ne bloque jamais plus de quelques secondes ;
#   * rapport complet au boot (uptime < 10 min), sur changement d'état, ou en
#     mode `report` ; silencieux le reste du temps ;
#   * sortie 1 UNIQUEMENT sur problème critique → `systemctl --failed` redevient
#     utile.
#
# Piège évité : les contrôles sont appelés DANS le shell principal. Un
# `while read < <(check_*)` les exécuterait dans un sous-shell et perdrait le
# compteur CRITICAL_FAILURES (donc jamais d'alerte).
#
# Usage : monitor.sh [check|report]

set -u

MODE="${1:-check}"
BOOT_LOG="${MON_BOOT_LOG:-/home/mdelsinne/mon-boot.log}"
STATE_FILE="${MON_STATE_FILE:-/tmp/pidog-monitor.state}"
UPTIME_S=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
BOOT_WINDOW=600   # 10 min

CRITICAL=0
SIGNATURE=""
REPORT=()
STAMP=$(date -Is)

# emit <texte> : ajoute au rapport (journal) et au fichier de log.
_LOGGABLE=0
if [ -w "$BOOT_LOG" ]; then
    _LOGGABLE=1
elif [ ! -e "$BOOT_LOG" ] && [ -w "$(dirname "$BOOT_LOG")" ]; then
    _LOGGABLE=1
fi

emit() {
    local line="$STAMP $*"
    REPORT+=("$line")
    if [ "$_LOGGABLE" -eq 1 ]; then
        printf '%s\n' "$line" >> "$BOOT_LOG" 2>/dev/null || true
    fi
}

is_active() { systemctl -q is-active "$1" 2>/dev/null; }

# ---------------------------------------------------------------- services --
check_service() {          # $1 = unité, $2 = critical|optional|expected-off
    local unit="$1" kind="$2" state
    if is_active "$unit"; then
        state="actif"
    else
        state="INACTIF"
        case "$kind" in
            critical)      CRITICAL=$((CRITICAL + 1)); state="INACTIF (critique)" ;;
            expected-off)  state="arrêté (attendu)" ;;
        esac
    fi
    SIGNATURE+="$unit=$state;"
    emit "$(printf 'service %-24s %s' "$unit" "$state")"
}

# ----------------------------------------------------------------- système --
check_system() {
    # Sous-tension / throttling : cause n°1 des coupures du 22/10.
    if command -v vcgencmd >/dev/null 2>&1; then
        local throttled
        throttled=$(vcgencmd get_throttled 2>/dev/null || true)
        SIGNATURE+="thr=$throttled;"
        emit "alim $throttled"
        case "$throttled" in
            ""|*throttled=0x0*) ;;
            *) CRITICAL=$((CRITICAL + 1))
               emit "ALIM ** sous-tension/throttling actif **" ;;
        esac
    fi

    local temp
    temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
    temp=$((temp / 1000))
    SIGNATURE+="temp=$temp;"
    if [ "$temp" -ge 80 ]; then
        CRITICAL=$((CRITICAL + 1))
        emit "temp ${temp}°C (critique)"
    else
        emit "temp ${temp}°C"
    fi

    local disk
    disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    disk=${disk:-0}
    SIGNATURE+="disk=$disk;"
    emit "disque ${disk}% utilisé (seuil 92%)"
    [ "$disk" -ge 92 ] && CRITICAL=$((CRITICAL + 1))

    local load mem
    load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || true)
    mem=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
    SIGNATURE+="mem=$mem;"
    emit "charge ${load:-?} — RAM dispo ${mem} Mo (seuil 80)"
    [ "$mem" -lt 80 ] && CRITICAL=$((CRITICAL + 1))

    # NTP : sans synchro, logs et timers mentent (incident du 22/10).
    local ntp
    ntp=$(journalctl -q -b -u systemd-timesyncd \
             -g 'Initial clock synchronization' -o cat 2>/dev/null | tail -1)
    SIGNATURE+="ntp=${ntp:+ok};"
    emit "ntp ${ntp:-pas encore synchronisé}"

    # Réseau : au moins une interface avec une IPv4.
    local ips
    ips=$(ip -4 -o addr show 2>/dev/null | awk '!/ lo /{print $2"="$4}' | paste -sd, -)
    SIGNATURE+="net=$ips;"
    emit "réseau ${ips:-aucune IPv4}"
    [ -z "$ips" ] && CRITICAL=$((CRITICAL + 1))

    # Daemon robot : état informatif (lancé à la main par choix).
    if [ -S /tmp/pidog.sock ] && is_active pidog.service; then
        emit "robot daemon actif (socket /tmp/pidog.sock)"
    elif [ -S /tmp/pidog.sock ]; then
        emit "robot socket présent, daemon inactif (lancement manuel ?)"
    else
        emit "robot daemon arrêté"
    fi
}

# --------------------------------------------------------------------- main --
if [ "$UPTIME_S" -lt "$BOOT_WINDOW" ]; then
    MODE="report"
    emit "=== démarrage (noyau $(uname -r), up ${UPTIME_S}s) ==="
    if command -v dmesg >/dev/null 2>&1; then
        while IFS= read -r line; do
            [ -n "$line" ] && emit "kernel: $line"
        done < <(dmesg 2>/dev/null | grep -iE 'under-?voltage|over-?temperature' | tail -5)
    fi
fi

check_service ssh.service critical
check_service lightdm.service critical
check_service NetworkManager.service critical
check_service x11vnc.service optional
check_service tailscaled.service optional
check_service pidog-agent-stream.service optional
check_service pidog.service expected-off
check_system

if [ "$CRITICAL" -gt 0 ]; then
    emit "=== ALERTE : $CRITICAL problème(s) critique(s) ==="
else
    emit "=== OK ==="
fi

# Écriture conditionnelle : au boot, sur changement d'état, ou en mode report.
PREV=$(cat "$STATE_FILE" 2>/dev/null || true)
if [ "$MODE" = "report" ] || [ "$PREV" != "$SIGNATURE" ]; then
    printf '%s\n' "${REPORT[@]}"
    printf '%s' "$SIGNATURE" > "$STATE_FILE" 2>/dev/null || true
fi

if [ "$CRITICAL" -gt 0 ]; then
    exit 1
fi
exit 0
