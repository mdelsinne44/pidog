#!/bin/bash
# PiDog Monitor — diagnostic santé + réparation graduée, idempotent et borné.
#
# Historique : l'ancienne version faisait un `sleep 120` dans ExecStart, était
# relancée 567 fois en 20 h (Restart=on-failure sur un Type=oneshot) et ses
# ExecStartPost étaient mal parenthésés (`a && echo X || echo Y | logger` ne
# logge jamais X). Résultat : du bruit, aucune alerte fiable.
#
# Ici :
#   * oneshot + timer, pas de Restart= ; jamais plus de quelques dizaines de s ;
#   * 3 modes : check (diagnostic + réparation), report, json ;
#   * rapport complet au boot (uptime < 10 min), sur changement d'état, après
#     une action, ou en mode report ; silencieux le reste du temps ;
#   * sortie 1 UNIQUEMENT sur problème critique → `systemctl --failed` redevient
#     utile.
#
# Réparation graduée (désactivable : MON_REPAIR=0) :
#   1. échelle déterministe par domaine (wifi, wan/dns, ssh, tailscale) ;
#   2. IA de dernier recours (monitor_ai.py) à whitelist fermée ;
#   3. reboot, en dernier ressort, avec budget et garde-fous (jamais en boucle).
#
# Le monitor ne démarre JAMAIS pidog.service ni pidog-agent-stream.service :
# il ne fait que constater leur état (`systemctl is-active`). La voix reste
# lancée à la main / par son propre service, c'est un choix explicite.
#
# Piège évité : les contrôles sont appelés DANS le shell principal. Un
# `while read < <(check_*)` les exécuterait dans un sous-shell et perdrait le
# compteur CRITICAL (donc jamais d'alerte).
#
# Usage :
#   monitor.sh [check]        # diagnostic + réparations (timer)
#   monitor.sh report         # diagnostic seul, rapport complet
#   monitor.sh json           # diagnostic seul, JSON sur stdout
#   monitor.sh repair <nom>   # une action de la whitelist (debug / manuel)

set -u

MODE="${1:-check}"
ACTION_ARG="${2:-}"

STATE_DIR="${MON_STATE_DIR:-/var/lib/pidog-monitor}"
BOOT_LOG="${MON_BOOT_LOG:-$HOME/mon-boot.log}"
UPTIME_S=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
BOOT_WINDOW="${MON_BOOT_WINDOW:-600}"

DRY_RUN="${MON_DRY_RUN:-0}"
REPAIR_ENABLED="${MON_REPAIR:-1}"
REBOOT_ENABLED="${MON_REBOOT:-1}"
STRICT="${MON_STRICT:-0}"

AI_ENABLED="${MON_AI:-1}"
AI_BIN="${MON_AI_BIN:-$(dirname "$(readlink -f "$0")")/monitor_ai.py}"
AI_MIN_CONF="${MON_AI_MIN_CONF:-0.7}"
AI_COOLDOWN="${MON_AI_COOLDOWN:-1800}"
AI_MAX_DAY="${MON_AI_MAX_DAY:-6}"
AI_ALLOW_REBOOT="${MON_AI_ALLOW_REBOOT:-0}"

MAX_ACTIONS_HOUR="${MON_MAX_ACTIONS_HOUR:-6}"
PER_ACTION_HOUR="${MON_PER_ACTION_HOUR:-2}"
REBOOT_MIN_INTERVAL="${MON_REBOOT_MIN_INTERVAL:-1800}"
REBOOT_MIN_UPTIME="${MON_REBOOT_MIN_UPTIME:-900}"
REBOOT_MAX_DAY="${MON_REBOOT_MAX_DAY:-3}"
ACTION_TIMEOUT="${MON_ACTION_TIMEOUT:-20}"

WIFI_DEV="${MON_WIFI_DEV:-wlan0}"
WIFI_PROFILE="${MON_WIFI_PROFILE:-}"
SSH_HOST="${MON_SSH_HOST:-127.0.0.1}"
SSH_PORT="${MON_SSH_PORT:-22}"
SSH_CHECK="${MON_SSH_CHECK:-banner}"
DNS_HOST="${MON_DNS_HOST:-github.com}"
PING_HOSTS="${MON_PING_HOSTS:-1.1.1.1 8.8.8.8}"
PING_TIMEOUT="${MON_PING_TIMEOUT:-2}"
ENV_FILE="${MON_ENV_FILE:-$HOME/pidog/extensions/pidog-agent/.env}"
LLM_LOCAL_DIR="${LLM_LOCAL_DIR:-$HOME/models/llm-local}"
TS_AUTHKEY_FILE="${MON_TAILSCALE_AUTHKEY_FILE:-$HOME/.config/pidog/tailscale-authkey}"

# ---------------------------------------------------------------- état ------
CRITICAL=0
DEGRADED=0
REGRESSED=0
REPAIR_FAILED=0
ACTIONS_DONE=0
SIGNATURE=""
REPORT=()
CHECKS=()            # "nom<TAB>ok<TAB>sévérité<TAB>détail"
RUN_ACTIONS=()       # "action<TAB>ok<TAB>source<TAB>détail"
WIFI_OK=1
LAN_OK=1
WAN_OK=1
DNS_OK=1
SSH_OK=1
TS_OK=1
HW_BAD=0

# Le répertoire d'état doit survivre aux reboots (sinon aucun budget d'essais
# ne tient). /var/lib est le bon endroit ; repli /tmp si non inscriptible.
STATE_FALLBACK=0
if ! mkdir -p "$STATE_DIR" 2>/dev/null || [ ! -w "$STATE_DIR" ]; then
    STATE_DIR="${TMPDIR:-/tmp}/pidog-monitor"
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    STATE_FALLBACK=1
fi
STATE_FILE="$STATE_DIR/signature"
AUDIT="$STATE_DIR/audit.tsv"
CHECKS_FILE="$STATE_DIR/checks.tsv"
HW_FILE="$STATE_DIR/hardware.tsv"
AI_LOG="$STATE_DIR/ai.log"
WIFI_PROFILE_FILE="$STATE_DIR/wifi_profile"
touch "$AUDIT" 2>/dev/null || true

# On ne veut pas de sortie si le log n'est pas inscriptible : le journal
# systemd reste la source de vérité.
_LOGGABLE=0
if [ -w "$BOOT_LOG" ]; then
    _LOGGABLE=1
elif [ ! -e "$BOOT_LOG" ] && [ -w "$(dirname "$BOOT_LOG")" ]; then
    _LOGGABLE=1
fi

STAMP=$(date -Is)
emit() {
    local line="$STAMP $*"
    REPORT+=("$line")
    if [ "$_LOGGABLE" -eq 1 ]; then
        printf '%s\n' "$line" >> "$BOOT_LOG" 2>/dev/null || true
    fi
}

jesc() {                 # échappe une chaîne pour du JSON
    printf '%s' "$1" | tr '\n\t' '  ' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

is_active() { systemctl -q is-active "$1" 2>/dev/null; }

# ------------------------------------------------------ état / budgets ------
total_actions() {
    local since=$(( $(date +%s) - 3600 ))
    awk -F'\t' -v s="$since" '$1 >= s { c++ } END { print c + 0 }' "$AUDIT" 2>/dev/null
}

action_count() {         # $1 = action, $2 = fenêtre (s)
    local since=$(( $(date +%s) - $2 ))
    awk -F'\t' -v s="$since" -v a="$1" '$1 >= s && $2 == a { c++ } END { print c + 0 }' \
        "$AUDIT" 2>/dev/null
}

last_action_ts() {       # $1 = action → epoch de la dernière exécution (0 si jamais)
    awk -F'\t' -v a="$1" '$2 == a { t = $1 } END { print t + 0 }' "$AUDIT" 2>/dev/null
}

audit() {                # $1 action, $2 ok(0/1), $3 source, $4 détail
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$(date +%s)" "$1" "$2" "$3" \
        "$(printf '%s' "${4:-}" | tr '\t\n' '  ' | cut -c1-400)" \
        >> "$AUDIT" 2>/dev/null || true
}

# --------------------------------------------------------------- contrôles ---
record() {               # $1 nom, $2 ok(0/1), $3 sévérité, $4 détail, [$5 signature]
    local name="$1" ok="$2" sev="$3" detail="$4" sig="${5:-$2}"
    CHECKS+=("$name"$'\t'"$ok"$'\t'"$sev"$'\t'"$detail")
    SIGNATURE+="$name=$sig;"
    if [ "$ok" -eq 0 ]; then
        case "$sev" in
            critical)  CRITICAL=$((CRITICAL + 1)) ;;
            important) DEGRADED=$((DEGRADED + 1)) ;;
        esac
    fi
}

check_service() {        # $1 unité, $2 critical|optional|expected-off
    local unit="$1" kind="$2" state
    if is_active "$unit"; then
        state="actif"
    else
        state="INACTIF"
        case "$kind" in
            critical)     state="INACTIF (critique)"; REGRESSED=1 ;;
            expected-off) state="arrêté (attendu)" ;;
        esac
    fi
    record "service:$unit" "$([ "$state" = "actif" ] && echo 1 || echo 0)" \
           "$([ "$kind" = critical ] && echo critical || echo info)" "$state"
}

check_hardware() {
    # Sous-tension / throttling : cause n°1 des coupures du 22/10.
    # Bits 0-3 = **maintenant** ; bits 16-19 = « est arrivé depuis le boot »
    # (historique, pas une alerte).
    local throttled="" now=0 past=0
    if command -v vcgencmd >/dev/null 2>&1; then
        throttled=$(vcgencmd get_throttled 2>/dev/null || true)
        now=$(( ${throttled#throttled=} ))
        [ -z "$throttled" ] && now=0
        past=$(( (now >> 16) & 0xF ))
        now=$(( now & 0xF ))
    fi
    if [ "$now" -ne 0 ]; then
        HW_BAD=1
        record "alim" 0 critical "** sous-tension/throttling actif ** ($throttled)"
    elif [ "$past" -ne 0 ]; then
        record "alim" 1 critical "ok maintenant (throttling vu depuis le boot : 0x$(printf %x "$past"))"
    else
        record "alim" 1 critical "ok (${throttled:-vcgencmd absent})"
    fi

    local temp
    temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
    temp=$((temp / 1000))
    if [ "$temp" -ge 80 ]; then
        HW_BAD=1
        record "temp" 0 critical "${temp}°C (seuil 80)"
    else
        record "temp" 1 critical "${temp}°C"
    fi

    local disk
    disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    disk=${disk:-0}
    if [ "$disk" -ge 92 ]; then
        record "disque" 0 critical "${disk}% utilisé (seuil 92%)"
    else
        record "disque" 1 critical "${disk}% utilisé (seuil 92%)"
    fi

    local load mem
    load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || true)
    mem=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "$mem" -lt 80 ]; then
        record "ram" 0 critical "${mem} Mo dispo (seuil 80) — charge ${load:-?}"
    else
        record "ram" 1 critical "${mem} Mo dispo — charge ${load:-?}"
    fi

    # NTP : sans synchro, logs et timers mentent (incident du 22/10).
    local ntp
    ntp=$(journalctl -q -b -u systemd-timesyncd \
             -g 'Initial clock synchronization' -o cat 2>/dev/null | tail -1)
    record "ntp" "$([ -n "$ntp" ] && echo 1 || echo 0)" info \
           "${ntp:-pas encore synchronisé}"

    {
        printf 'uptime\t%s\n' "$UPTIME_S"
        printf 'throttled\t%s\n' "${throttled:-?}"
        printf 'temp_c\t%s\n' "$temp"
        printf 'disk_pct\t%s\n' "$disk"
        printf 'ram_mb\t%s\n' "$mem"
        printf 'ntp\t%s\n' "${ntp:-ko}"
    } > "$HW_FILE" 2>/dev/null || true
}

# --------------------------------------------------------------- réseau ------
ping_host() {
    timeout $(( PING_TIMEOUT + 2 )) ping -c1 -W "$PING_TIMEOUT" "$1" >/dev/null 2>&1
}

ping_any() {
    local h
    for h in $PING_HOSTS; do
        ping_host "$h" && return 0
    done
    return 1
}

wifi_state() {
    nmcli -t -f DEVICE,STATE dev status 2>/dev/null \
        | awk -F: -v d="$WIFI_DEV" '$1 == d { print $2; exit }'
}

wifi_profile_current() {
    nmcli -t -f GENERAL.CONNECTION dev show "$WIFI_DEV" 2>/dev/null \
        | sed 's/^GENERAL.CONNECTION://'
}

ts_state() {
    tailscale status --json 2>/dev/null \
        | grep -m1 '"BackendState"' \
        | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/'
}

ssh_banner() {
    if [ -n "${MON_SSH_CMD:-}" ]; then
        $MON_SSH_CMD 2>/dev/null
        return
    fi
    timeout 3 bash -c \
        'exec 3<>/dev/tcp/'"$SSH_HOST"'/'"$SSH_PORT"'; head -c 40 <&3' 2>/dev/null
}

rfkill_soft_blocked() {
    command -v rfkill >/dev/null 2>&1 || return 1
    rfkill list wifi 2>/dev/null | grep -qi 'Soft blocked: yes'
}

verify_wifi() {
    command -v nmcli >/dev/null 2>&1 || return 0
    [ "$(wifi_state)" = "connected" ]
}

verify_wan() { ping_any; }

verify_dns() { getent hosts "$DNS_HOST" >/dev/null 2>&1; }

verify_ssh() {
    [ "$SSH_CHECK" = "off" ] && return 0
    if [ "$SSH_CHECK" = "port" ]; then
        timeout 2 bash -c 'exec 3<>/dev/tcp/'"$SSH_HOST"'/'"$SSH_PORT"'' 2>/dev/null
        return
    fi
    case "$(ssh_banner)" in
        SSH-*) return 0 ;;
        *)     return 1 ;;
    esac
}

verify_tailscale() {
    command -v tailscale >/dev/null 2>&1 || return 0
    [ "$(ts_state)" = "Running" ]
}

verify_all() {
    verify_wifi && verify_wan && verify_dns && verify_ssh && verify_tailscale
}

wait_ok() {              # $1 fonction de vérification, $2 secondes
    local fn="$1" limit="$2" i=0
    while [ "$i" -lt "$limit" ]; do
        "$fn" && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

check_network() {
    local ips gw wan dns ws cur
    ips=$(ip -4 -o addr show 2>/dev/null | awk '!/ lo /{print $2"="$4}' | paste -sd, -)
    record "reseau:ipv4" "$([ -n "$ips" ] && echo 1 || echo 0)" critical \
           "${ips:-aucune IPv4}"

    # rfkill : un Wi-Fi soft-bloqué ne remontera jamais tout seul.
    if command -v rfkill >/dev/null 2>&1; then
        if rfkill_soft_blocked; then
            record "reseau:rfkill" 0 important "Wi-Fi soft-bloqué (rfkill)"
        else
            record "reseau:rfkill" 1 info "Wi-Fi non bloqué"
        fi
    fi

    # Wi-Fi : association réelle, pas seulement NetworkManager actif.
    if command -v nmcli >/dev/null 2>&1; then
        ws=$(wifi_state)
        cur=$(wifi_profile_current)
        case "$ws" in
            connected)
                WIFI_OK=1
                SIGNATURE+="wifi=$cur;"
                record "reseau:wifi" 1 critical "$WIFI_DEV connecté ($cur)"
                [ -n "$cur" ] && printf '%s' "$cur" > "$WIFI_PROFILE_FILE" 2>/dev/null
                ;;
            disconnected|"")
                WIFI_OK=0
                record "reseau:wifi" 0 critical "$WIFI_DEV déconnecté"
                ;;
            *)
                WIFI_OK=0
                record "reseau:wifi" 0 critical "$WIFI_DEV état: $ws"
                ;;
        esac
    else
        SIGNATURE+="wifi=absent;"
        record "reseau:wifi" 1 info "nmcli absent (non vérifiable)"
    fi

    # LAN : passerelle joignable. Informatif mais discriminant.
    gw=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
    if [ -n "$gw" ] && ping_host "$gw"; then
        LAN_OK=1
        record "reseau:lan" 1 info "passerelle $gw joignable"
    else
        LAN_OK=0
        record "reseau:lan" 0 info "${gw:-pas de passerelle} injoignable"
    fi

    # WAN : ping d'au moins un résolveur public.
    if ping_any; then
        WAN_OK=1
        wan="ok ($PING_HOSTS)"
    else
        WAN_OK=0
        wan="** injoignable ($PING_HOSTS) **"
    fi
    record "reseau:wan" "$WAN_OK" critical "$wan"

    # DNS : une résolution qui aboutit, ou rien ne marche vraiment.
    if verify_dns; then
        DNS_OK=1
        dns="ok ($DNS_HOST)"
    else
        DNS_OK=0
        dns="** échec ($DNS_HOST) **"
    fi
    record "reseau:dns" "$DNS_OK" critical "$dns"

    # SSH : bannière réelle (le démon peut être actif et bloqué).
    if verify_ssh; then
        SSH_OK=1
        record "reseau:ssh" 1 critical "bannière OK ($SSH_HOST:$SSH_PORT)"
    else
        SSH_OK=0
        record "reseau:ssh" 0 critical "** pas de bannière ($SSH_HOST:$SSH_PORT) **"
    fi

    # Tailscale : BackendState, pas juste le démon.
    if command -v tailscale >/dev/null 2>&1; then
        cur=$(ts_state)
        if [ "$cur" = "Running" ]; then
            TS_OK=1
            record "reseau:tailscale" 1 important "Running"
        else
            TS_OK=0
            record "reseau:tailscale" 0 important "BackendState=${cur:-inconnu}"
        fi
    else
        record "reseau:tailscale" 1 important "tailscale absent (non vérifiable)"
    fi
}

check_llm() {
    local configured=() name label
    if [ ! -r "$ENV_FILE" ]; then
        record "llm:cles" 0 critical ".env illisible ($ENV_FILE)"
        return
    fi
    for name in GROQ_KEY GEMINI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY; do
        if grep -qE "^${name}=.+" "$ENV_FILE" 2>/dev/null; then
            label=${name%_API_KEY}
            label=${label%_KEY}
            configured+=("$label")
        fi
    done
    if [ "${#configured[@]}" -eq 0 ]; then
        record "llm:cles" 0 critical "aucune clé : le robot ne peut pas répondre"
    else
        record "llm:cles" 1 info "clés ${configured[*]}"
    fi

    # Secours hors ligne : informatif, ce n'est pas critique tant qu'une clé
    # réseau existe. On ne charge JAMAIS le modèle ici (coût RAM/temps).
    if [ -f "$LLM_LOCAL_DIR/config.json" ] || ls "$LLM_LOCAL_DIR"/*.gguf >/dev/null 2>&1; then
        record "llm:local" 1 info "présent ($LLM_LOCAL_DIR)"
    else
        record "llm:local" 1 info "absent ($LLM_LOCAL_DIR) — pas de repli hors ligne"
    fi
}

check_robot() {
    if [ -S /tmp/pidog.sock ] && is_active pidog.service; then
        record "robot:daemon" 1 info "actif (socket /tmp/pidog.sock)"
    elif [ -S /tmp/pidog.sock ]; then
        record "robot:daemon" 1 info "socket présent, daemon inactif (lancement manuel ?)"
    else
        record "robot:daemon" 1 info "arrêté (lancé à la main par choix)"
    fi
}

# ------------------------------------------------------------ réparations ----
# Whitelist FERMÉE : c'est le seul endroit qui exécute quelque chose, et l'IA
# ne peut choisir que parmi ces noms. Aucun shell libre, aucun paramètre
# arbitraire (pas d'injection possible via un log ou un SSID).
run_action() {           # $1 action, $2 source, $3 raison
    local action="$1" source="${2:-auto}" why="${3:-}" rc=0 out=""
    local -a cmd=()

    case "$action" in
        rfkill_unblock|wifi_reapply|wifi_up|nm_restart|dns_restart|tailscale_restart|\
        tailscale_up|ssh_restart|bluetooth_restart|audio_restart|reboot|none) ;;
        *)
            emit "action REFUSÉE (hors whitelist) : $action"
            audit "$action" 0 "$source" "hors whitelist"
            return 1
            ;;
    esac

    if [ "$action" = "none" ]; then
        emit "ia: aucune action retenue — $why"
        return 0
    fi

    if [ "$REPAIR_ENABLED" != 1 ]; then
        emit "réparation désactivée (MON_REPAIR=0) : $action"
        return 1
    fi

    # ---------------------------------------------------------- budgets --
    if [ "$action" = "reboot" ]; then
        local last now
        now=$(date +%s)
        last=$(last_action_ts reboot)
        [ "$HW_BAD" -eq 0 ] || {
            emit "reboot refusé : matériel en alerte (alim/temp)"; return 1; }
        [ "$UPTIME_S" -ge "$REBOOT_MIN_UPTIME" ] || {
            emit "reboot refusé : uptime ${UPTIME_S}s < ${REBOOT_MIN_UPTIME}s (anti-boucle)"
            return 1; }
        [ $(( now - ${last:-0} )) -ge "$REBOOT_MIN_INTERVAL" ] || {
            emit "reboot refusé : moins de ${REBOOT_MIN_INTERVAL}s depuis le dernier"
            return 1; }
        [ "$(action_count reboot 86400)" -lt "$REBOOT_MAX_DAY" ] || {
            emit "reboot refusé : quota ${REBOOT_MAX_DAY}/24 h atteint"; return 1; }
    else
        [ "$(total_actions)" -lt "$MAX_ACTIONS_HOUR" ] || {
            emit "action $action refusée : budget ${MAX_ACTIONS_HOUR}/h atteint"
            audit "$action" 0 "$source" "budget horaire"
            return 1; }
        [ "$(action_count "$action" 3600)" -lt "$PER_ACTION_HOUR" ] || {
            emit "action $action refusée : ${PER_ACTION_HOUR}/h déjà tentées"
            audit "$action" 0 "$source" "budget par action"
            return 1; }
    fi

    case "$action" in
        rfkill_unblock)    cmd=(sudo -n rfkill unblock wifi) ;;
        wifi_reapply)      cmd=(nmcli dev reapply "$WIFI_DEV") ;;
        wifi_up)           cmd=(nmcli -w 20 con up id "$WIFI_PROFILE") ;;
        nm_restart)        cmd=(sudo -n systemctl restart NetworkManager) ;;
        dns_restart)       cmd=(sudo -n systemctl restart systemd-resolved) ;;
        tailscale_restart) cmd=(sudo -n systemctl restart tailscaled) ;;
        tailscale_up)
            cmd=(sudo -n tailscale up --timeout=15s)
            if [ -r "$TS_AUTHKEY_FILE" ]; then
                cmd=(sudo -n tailscale up --timeout=15s \
                     --auth-key="file:$TS_AUTHKEY_FILE")
            fi
            ;;
        ssh_restart)       cmd=(sudo -n systemctl restart ssh) ;;
        bluetooth_restart) cmd=(sudo -n systemctl restart bluetooth) ;;
        audio_restart)     cmd=(pulseaudio --start) ;;
        reboot)            cmd=(sudo -n reboot) ;;
    esac

    if [ "$DRY_RUN" = 1 ]; then
        emit "[dry-run] $action ($source) — ${why:-sans raison}"
        audit "$action" 1 "$source" "dry-run | $why"
        RUN_ACTIONS+=("$action"$'\t'"1"$'\t'"$source"$'\t'"dry-run | $why")
        ACTIONS_DONE=$((ACTIONS_DONE + 1))
        return 0
    fi

    out=$(timeout "$ACTION_TIMEOUT" "${cmd[@]}" 2>&1)
    rc=$?
    emit "réparation $action ($source) → rc=$rc${out:+ | $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160)}"
    audit "$action" "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "$source" \
          "${why}${out:+ | $out}"
    RUN_ACTIONS+=("$action"$'\t'"$([ "$rc" -eq 0 ] && echo 1 || echo 0)"$'\t'"$source"$'\t'"${why}")
    ACTIONS_DONE=$((ACTIONS_DONE + 1))
    return "$rc"
}

repair_wifi() {
    verify_wifi && return 0
    # Profil : explicite, sinon dernier SSID vu connecté (état persistant).
    if [ -z "$WIFI_PROFILE" ] && [ -r "$WIFI_PROFILE_FILE" ]; then
        WIFI_PROFILE=$(cat "$WIFI_PROFILE_FILE" 2>/dev/null || true)
    fi
    if rfkill_soft_blocked; then
        run_action rfkill_unblock auto "wifi soft-bloqué (rfkill)"
        sleep 2
    fi
    run_action wifi_reapply auto "wifi déconnecté ($WIFI_DEV)"
    wait_ok verify_wifi 12 && return 0
    if [ -n "$WIFI_PROFILE" ]; then
        run_action wifi_up auto "profil $WIFI_PROFILE"
        wait_ok verify_wifi 20 && return 0
    fi
    run_action nm_restart auto "wifi toujours KO"
    wait_ok verify_wifi 25 && return 0
    return 1
}

repair_net() {
    run_action nm_restart auto "wan/dns KO"
    wait_ok verify_wan 20 && verify_dns && return 0
    if systemctl list-unit-files systemd-resolved.service 2>/dev/null \
       | grep -q '^systemd-resolved\.service'; then
        run_action dns_restart auto "dns KO"
        wait_ok verify_dns 15 && return 0
    fi
    return 1
}

repair_ssh() {
    verify_ssh && return 0
    run_action ssh_restart auto "pas de bannière SSH"
    wait_ok verify_ssh 12
}

repair_tailscale() {
    verify_tailscale && return 0
    local state
    state=$(ts_state)
    if [ "$state" = "NeedsLogin" ] && [ ! -r "$TS_AUTHKEY_FILE" ]; then
        emit "tailscale: NeedsLogin sans clé ($TS_AUTHKEY_FILE) — action manuelle requise"
        return 1
    fi
    run_action tailscale_restart auto "tailscale $state"
    wait_ok verify_tailscale 20 && return 0
    run_action tailscale_up auto "tailscale up"
    wait_ok verify_tailscale 25 && return 0
    return 1
}

# --------------------------------------------------------------- IA ----------
ai_needed() {
    [ "$WIFI_OK" -eq 0 ] || [ "$WAN_OK" -eq 0 ] || [ "$DNS_OK" -eq 0 ] \
        || [ "$SSH_OK" -eq 0 ] || [ "$TS_OK" -eq 0 ]
}

ask_ai() {
    local now last online=0 action="" conf="0" source="" reason=""
    now=$(date +%s)

    [ "$(action_count ai_call 86400)" -lt "$AI_MAX_DAY" ] || {
        emit "ia: quota journalier atteint ($AI_MAX_DAY)"; return 3; }
    last=$(last_action_ts ai_call)
    [ $(( now - ${last:-0} )) -ge "$AI_COOLDOWN" ] || {
        emit "ia: cooldown ($(( AI_COOLDOWN - (now - last) ))s restants)"; return 3; }
    [ -x "$AI_BIN" ] || { emit "ia: helper absent ($AI_BIN)"; return 3; }

    [ "$WAN_OK" -eq 1 ] && online=1
    audit ai_call 0 ia "demande (online=$online)"
    emit "ia: consultation (online=$online, $( [ "$WAN_OK" -eq 1 ] && echo cloud || echo modèle local ))"

    IFS=$'\t' read -r action conf source reason < <(
        timeout 150 "$AI_BIN" --machine \
            --checks "$CHECKS_FILE" \
            --audit "$AUDIT" \
            --hardware "$HW_FILE" \
            --online "$online" \
            --env "$ENV_FILE" 2>>"$AI_LOG"
    )

    action=${action:-}
    conf=${conf:-0}
    [ -n "$action" ] || { emit "ia: pas de décision exploitable (voir $AI_LOG)"; return 3; }
    [ -n "$source" ] || source="ia"

    # « none » est une décision valide (et le cas d'une IA indisponible) :
    # inutile d'y appliquer le seuil de confiance.
    if [ "$action" = "none" ]; then
        emit "ia: aucune action — $reason"
        audit none 0 ia "aucune action | $reason"
        return 0
    fi

    if ! awk -v c="$conf" -v m="$AI_MIN_CONF" 'BEGIN { exit !(c + 0 >= m + 0) }'; then
        emit "ia: confiance insuffisante ($conf < $AI_MIN_CONF) — action $action ignorée"
        audit "$action" 0 ia "confiance $conf < $AI_MIN_CONF | $reason"
        return 3
    fi
    if [ "$action" = "reboot" ] && [ "$AI_ALLOW_REBOOT" != 1 ]; then
        emit "ia: reboot proposé mais interdit à l'IA (MON_AI_ALLOW_REBOOT=0)"
        audit "$action" 0 ia "reboot interdit à l'IA | $reason"
        return 3
    fi

    emit "ia: $source propose $action (confiance $conf) — $reason"
    run_action "$action" ia "$reason"
    wait_ok verify_all 20 || true
}

# -------------------------------------------------------------- rendu --------
render_checks() {
    local line name ok sev detail
    for line in "${CHECKS[@]}"; do
        IFS=$'\t' read -r name ok sev detail <<< "$line"
        emit "$(printf '%-26s %s' "$name" "$detail")"
    done
}

render_json() {
    local line name ok sev detail sep="" actions="" a
    printf '{'
    printf '"generated":"%s",' "$(jesc "$STAMP")"
    printf '"uptime_s":%s,' "$UPTIME_S"
    printf '"ok":%s,' "$([ "$CRITICAL" -eq 0 ] && echo true || echo false)"
    printf '"critical":%s,' "$CRITICAL"
    printf '"degraded":%s,' "$DEGRADED"
    printf '"dry_run":%s,' "$([ "$DRY_RUN" = 1 ] && echo true || echo false)"
    printf '"checks":['
    for line in "${CHECKS[@]}"; do
        IFS=$'\t' read -r name ok sev detail <<< "$line"
        printf '%s{"name":"%s","ok":%s,"severity":"%s","detail":"%s"}' \
            "$sep" "$(jesc "$name")" "$([ "$ok" -eq 1 ] && echo true || echo false)" \
            "$(jesc "$sev")" "$(jesc "$detail")"
        sep=","
    done
    printf '],"actions":['
    sep=""
    for a in ${RUN_ACTIONS[@]+"${RUN_ACTIONS[@]}"}; do
        IFS=$'\t' read -r name ok sev detail <<< "$a"
        printf '%s{"action":"%s","ok":%s,"source":"%s","reason":"%s"}' \
            "$sep" "$(jesc "$name")" "$([ "$ok" -eq 1 ] && echo true || echo false)" \
            "$(jesc "$sev")" "$(jesc "$detail")"
        sep=","
    done
    printf ']}\n'
}

# ---------------------------------------------------------------- main ------
run_checks() {
    CRITICAL=0
    DEGRADED=0
    REGRESSED=0
    SIGNATURE=""
    CHECKS=()
    WIFI_OK=1
    LAN_OK=1
    WAN_OK=1
    DNS_OK=1
    SSH_OK=1
    TS_OK=1
    HW_BAD=0

    check_service ssh.service critical
    check_service lightdm.service critical
    check_service NetworkManager.service critical
    check_service x11vnc.service optional
    check_service tailscaled.service optional
    check_service pidog-agent-stream.service optional
    check_service pidog.service expected-off
    check_hardware
    check_network
    check_llm
    check_robot
    record "etat:repertoire" "$([ "$STATE_FALLBACK" = 1 ] && echo 0 || echo 1)" info \
           "$STATE_DIR$([ "$STATE_FALLBACK" = 1 ] && echo ' (repli : budgets perdus au reboot)')"

    # Fichier de contrôles pour le helper IA (et pour --json externe).
    : > "$CHECKS_FILE" 2>/dev/null || true
    local line
    for line in "${CHECKS[@]}"; do
        printf '%s\n' "$line" >> "$CHECKS_FILE" 2>/dev/null || true
    done
}

repair_pass() {
    [ "$REPAIR_ENABLED" = 1 ] || return 0

    if [ "$WIFI_OK" -eq 0 ] && command -v nmcli >/dev/null 2>&1; then
        if repair_wifi; then
            WIFI_OK=1          # sinon repair_net serait sauté à ce passage
        else
            REPAIR_FAILED=1
        fi
    fi
    if { [ "$WAN_OK" -eq 0 ] || [ "$DNS_OK" -eq 0 ]; } && [ "$WIFI_OK" -eq 1 ]; then
        repair_net || REPAIR_FAILED=1
    fi
    if [ "$SSH_OK" -eq 0 ]; then
        repair_ssh || REPAIR_FAILED=1
    fi
    if [ "$TS_OK" -eq 0 ]; then
        repair_tailscale || REPAIR_FAILED=1
    fi
    [ "$ACTIONS_DONE" -gt 0 ] && run_checks
    return 0
}

main() {
    # Mode manuel : une seule action, puis on rend la main.
    if [ "$MODE" = "repair" ]; then
        RUN_ACTIONS=()
        if run_action "$ACTION_ARG" manuel "demande manuelle"; then
            render_json
            exit 0
        fi
        exit 1
    fi

    if [ "$UPTIME_S" -lt "$BOOT_WINDOW" ] && [ "$MODE" = "check" ]; then
        emit "=== démarrage (noyau $(uname -r), up ${UPTIME_S}s) ==="
        if command -v dmesg >/dev/null 2>&1; then
            while IFS= read -r line; do
                [ -n "$line" ] && emit "kernel: $line"
            done < <(dmesg 2>/dev/null | grep -iE 'under-?voltage|over-?temperature' | tail -5)
        fi
    fi

    run_checks

    if [ "$MODE" = "check" ]; then
        repair_pass
        if ai_needed && [ "$AI_ENABLED" = 1 ] \
           && { [ "$ACTIONS_DONE" -gt 0 ] || [ "$REPAIR_FAILED" -eq 1 ]; }; then
            ask_ai || true
            run_checks
        fi
        # Reboot en dernier ressort : seulement si la panne est LOCALE (wifi,
        # passerelle ou SSH KO). Un WAN/DNS injoignable alors que le LAN répond
        # est une panne FAI : redémarrer la Pi n'y changerait rien.
        if ai_needed && [ "$REBOOT_ENABLED" = 1 ] && [ "$REPAIR_ENABLED" = 1 ] \
           && { [ "$WIFI_OK" -eq 0 ] || [ "$LAN_OK" -eq 0 ] \
                || [ "$SSH_OK" -eq 0 ]; }; then
            run_action reboot auto "domaines critiques KO après réparations + IA" || true
        fi
    fi

    if [ "$CRITICAL" -gt 0 ]; then
        SUMMARY="=== ALERTE : $CRITICAL problème(s) critique(s) ==="
    elif [ "$DEGRADED" -gt 0 ]; then
        SUMMARY="=== DÉGRADÉ : $DEGRADED problème(s) non critique(s) ==="
    else
        SUMMARY="=== OK ==="
    fi

    if [ "$MODE" = "json" ]; then
        render_json
    else
        PREV=$(cat "$STATE_FILE" 2>/dev/null || true)
        if [ "$MODE" = "report" ] || [ "$UPTIME_S" -lt "$BOOT_WINDOW" ] \
           || [ "$PREV" != "$SIGNATURE" ] || [ "$ACTIONS_DONE" -gt 0 ]; then
            render_checks
            emit "$SUMMARY"
            printf '%s\n' "${REPORT[@]}"
        fi
        printf '%s' "$SIGNATURE" > "$STATE_FILE" 2>/dev/null || true
    fi

    if [ "$CRITICAL" -gt 0 ]; then
        exit 1
    fi
    if [ "$STRICT" = 1 ] && [ "$DEGRADED" -gt 0 ]; then
        exit 2
    fi
    exit 0
}

main
