#!/bin/bash
# Tests hors-ligne du PiDog Monitor.
#
# Tout est simulé via un faux PATH (systemctl, nmcli, ping, tailscale, ip…)
# et MON_DRY_RUN=1 : aucun service n'est touché, aucun reboot, aucun réseau.
#
#   ./tests/test_monitor.sh          # tous les tests
#   ./tests/test_monitor.sh -v       # garde la sortie du moniteur

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
MON="$HERE/../monitor.sh"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
ko()   { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; [ -n "${LAST_OUT:-}" ] && sed 's/^/       | /' "$LAST_OUT" | head -15; }
check(){ if eval "$2"; then ok "$1"; else ko "$1"; fi; }

# --------------------------------------------------------------- fixtures ---
setup() {
    T=$(mktemp -d "${TMPDIR:-/tmp}/pidog-monitor-test.XXXXXX")
    mkdir -p "$T/bin" "$T/state" "$T/fake/services"
    export FAKE_STATE="$T/fake"
    export CALLS="$T/calls"
    : > "$CALLS"

    export PATH="$T/bin:$PATH"
    export MON_STATE_DIR="$T/state"
    export MON_BOOT_LOG="$T/boot.log"
    export MON_ENV_FILE="$T/env"
    export LLM_LOCAL_DIR="$T/no-model"
    export MON_DRY_RUN=1
    export MON_SSH_CMD="ssh-stub"
    export MON_AI_BIN="$T/ai-stub"
    printf 'GROQ_KEY=x\nGEMINI_API_KEY=y\nOPENROUTER_API_KEY=z\n' > "$T/env"

    # Wi-Fi / réseau / tailscale « tout va bien » par défaut.
    echo connected > "$FAKE_STATE/wifi"
    echo Running   > "$FAKE_STATE/tailscale"
    echo ok        > "$FAKE_STATE/ping"
    echo ok        > "$FAKE_STATE/dns"
    for unit in ssh NetworkManager tailscaled; do
        echo active > "$FAKE_STATE/services/$unit.service"
    done
    echo inactive > "$FAKE_STATE/services/pidog.service"
    echo inactive > "$FAKE_STATE/services/pidog-agent-stream.service"
    echo active   > "$FAKE_STATE/services/pidog-stream.service"

    write_fakes
}

write_fakes() {
    cat > "$T/bin/systemctl" <<'EOS'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"
case "$1" in
  -q)
    if [ "$2" = "is-active" ]; then
      f="$FAKE_STATE/services/$3"
      [ -f "$f" ] && [ "$(cat "$f")" = active ] && exit 0
      exit 3
    fi
    exit 0 ;;
  is-active)
    f="$FAKE_STATE/services/$2"
    [ -f "$f" ] && [ "$(cat "$f")" = active ] && exit 0
    exit 3 ;;
  list-unit-files)
    [ -f "$FAKE_STATE/resolved" ] && echo "systemd-resolved.service enabled enabled"
    exit 0 ;;
  *) exit 0 ;;
esac
EOS

    cat > "$T/bin/nmcli" <<'EOS'
#!/bin/bash
echo "nmcli $*" >> "$CALLS"
if [ "$1" = "-t" ] && [ "$2" = "-f" ] && [ "$3" = "DEVICE,STATE" ]; then
  echo "wlan0:$(cat "$FAKE_STATE/wifi" 2>/dev/null || echo connected)"
  echo "tailscale0:connected (externally)"
  exit 0
fi
if [ "$1" = "-t" ] && [ "$2" = "-f" ] && [ "$3" = "GENERAL.CONNECTION" ]; then
  echo "GENERAL.CONNECTION:Livebox-Test"
  exit 0
fi
if [ "$1" = "dev" ] && [ "$2" = "reapply" ]; then
  echo connected > "$FAKE_STATE/wifi"   # simule un reapply qui rétablit le lien
  exit 0
fi
exit 0
EOS

    cat > "$T/bin/tailscale" <<'EOS'
#!/bin/bash
echo "tailscale $*" >> "$CALLS"
printf '{\n  "BackendState": "%s"\n}\n' "$(cat "$FAKE_STATE/tailscale" 2>/dev/null || echo Running)"
EOS

    cat > "$T/bin/ping" <<'EOS'
#!/bin/bash
echo "ping $*" >> "$CALLS"
[ "$(cat "$FAKE_STATE/ping" 2>/dev/null || echo ok)" = ok ] && exit 0
exit 1
EOS

    cat > "$T/bin/getent" <<'EOS'
#!/bin/bash
echo "getent $*" >> "$CALLS"
[ "$(cat "$FAKE_STATE/dns" 2>/dev/null || echo ok)" = ok ] && { echo "1.2.3.4 $2"; exit 0; }
exit 2
EOS

    cat > "$T/bin/ip" <<'EOS'
#!/bin/bash
echo "ip $*" >> "$CALLS"
case "$*" in
  *route*) echo "default via 192.168.1.1 dev wlan0 proto dhcp" ;;
  *addr*)  echo "2: wlan0    inet 192.168.1.22/24 brd 192.168.1.255 scope global wlan0" ;;
esac
exit 0
EOS

    cat > "$T/bin/rfkill" <<'EOS'
#!/bin/bash
echo "rfkill $*" >> "$CALLS"
echo "1: phy0: Wireless LAN"
if [ -f "$FAKE_STATE/rfkill_blocked" ]; then
  echo "	Soft blocked: yes"
else
  echo "	Soft blocked: no"
fi
echo "	Hard blocked: no"
EOS

    cat > "$T/bin/ssh-stub" <<'EOS'
#!/bin/bash
[ "$(cat "$FAKE_STATE/ssh" 2>/dev/null || echo ok)" = ok ] || exit 1
printf 'SSH-2.0-OpenSSH_9.2p1 test\r\n'
EOS

    cat > "$T/bin/sudo" <<'EOS'
#!/bin/bash
echo "sudo $*" >> "$CALLS"
[ "${1:-}" = "-n" ] && shift
exec "$@"
EOS

    cat > "$T/bin/reboot" <<'EOS'
#!/bin/bash
echo "reboot $*" >> "$CALLS"
exit 0
EOS

    cat > "$T/bin/vcgencmd" <<'EOS'
#!/bin/bash
echo "throttled=0x0"
EOS

    cat > "$T/bin/journalctl" <<'EOS'
#!/bin/bash
echo "Initial clock synchronization"
EOS

    cat > "$T/bin/dmesg" <<'EOS'
#!/bin/bash
exit 0
EOS

    cat > "$T/bin/sleep" <<'EOS'
#!/bin/bash
exit 0
EOS

    chmod +x "$T/bin/"*
}

teardown() { [ -n "${T:-}" ] && rm -rf "$T"; }

# Lance le moniteur, garde stdout dans $T/out et stderr dans $T/err.
run_mon() {
    LAST_OUT="$T/out"
    bash "$MON" "$@" > "$T/out" 2> "$T/err"
    RC=$?
    [ "$VERBOSE" = 1 ] && { echo "--- monitor.sh $* (rc=$RC) ---"; cat "$T/out"; cat "$T/err"; }
    return 0
}

audit_has() { grep -q "$1" "$MON_STATE_DIR/audit.tsv" 2>/dev/null; }
audit_action() {        # $1 action, [$2 source]
    awk -F'\t' -v a="$1" -v s="${2:-}" \
        '($2 == a) && (s == "" || $4 == s) { found = 1 } END { exit !found }' \
        "$MON_STATE_DIR/audit.tsv" 2>/dev/null
}
audit_count() {         # $1 action
    awk -F'\t' -v a="$1" '$2 == a { c++ } END { print c + 0 }' \
        "$MON_STATE_DIR/audit.tsv" 2>/dev/null
}

# ============================================================== test 1 ======
echo "1. tout va bien → JSON valide, aucune action"
setup
run_mon json
check "code retour 0" "[ $RC -eq 0 ]"
check "JSON parseable" "python3 -m json.tool '$T/out' >/dev/null 2>&1"
check "\"ok\":true" "grep -q '\"ok\":true' '$T/out'"
check "aucune action" "grep -q '\"actions\":\[\]' '$T/out'"
check "aucun audit d'action" "[ ! -s '$MON_STATE_DIR/audit.tsv' ]"
teardown

# ============================================================== test 2 ======
echo "2. wifi + ssh + tailscale KO → réparations dans l'ordre, sans jamais démarrer pidog/stream"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko           > "$FAKE_STATE/ping"
echo ko           > "$FAKE_STATE/dns"
echo Stopped      > "$FAKE_STATE/tailscale"
echo ko           > "$FAKE_STATE/ssh"
run_mon check
check "code retour 1 (critique)" "[ $RC -eq 1 ]"
check "wifi_reapply tenté" "audit_action wifi_reapply"
check "nm_restart tenté (wifi bloqué)" "audit_action nm_restart"
check "ssh_restart tenté" "audit_action ssh_restart"
check "tailscale_restart tenté" "audit_action tailscale_restart"
check "pas de reboot au 1er passage (garde-fou 2 constats)" "[ \"$(audit_count reboot)\" -eq 0 ]"
check "JSON valide en mode json" "bash '$MON' json 2>/dev/null | python3 -m json.tool >/dev/null"
check "jamais 'start pidog.service'" "! grep -qE 'systemctl start pidog\.service' '$CALLS'"
check "jamais 'start pidog-agent-stream'" "! grep -qE 'systemctl start pidog-agent-stream' '$CALLS'"
check "jamais 'enable'" "! grep -q 'systemctl enable' '$CALLS'"
teardown

# ============================================================== test 3 ======
echo "3. anti-boucle de reboot → jamais à la 1re détection, une seule fois sur 3 passages"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko           > "$FAKE_STATE/ping"
echo ko           > "$FAKE_STATE/dns"
echo ko           > "$FAKE_STATE/ssh"
echo Stopped      > "$FAKE_STATE/tailscale"
run_mon check
check "1er passage : aucun reboot (panne non encore confirmée)" "[ \"$(audit_count reboot)\" -eq 0 ]"
run_mon check
check "2e passage : reboot (panne constatée 2×)" "[ \"$(audit_count reboot)\" -eq 1 ]"
run_mon check
check "3e passage : reboot refusé (intervalle 30 min)" "grep -q 'reboot refusé' '$T/boot.log'"
check "au total un seul reboot" "[ \"$(audit_count reboot)\" -eq 1 ]"
teardown

# ============================================================== test 4 ======
echo "4. IA hors whitelist → refusée, rien n'est exécuté"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko           > "$FAKE_STATE/ping"
echo ko           > "$FAKE_STATE/dns"
echo ko           > "$FAKE_STATE/ssh"
echo Stopped      > "$FAKE_STATE/tailscale"
printf '#!/bin/bash\nprintf "rm -rf /\\t1.0\\tstub\\tobeis\\n"\n' > "$T/ai-stub"
chmod +x "$T/ai-stub"
run_mon check
check "action refusée (hors whitelist)" "grep -q 'hors whitelist' '$MON_STATE_DIR/audit.tsv'"
check "aucune commande rm exécutée" "! grep -q 'rm -rf' '$CALLS'"
teardown

# ============================================================== test 5 ======
echo "5. IA dans la whitelist → exécutée et auditée (source=ia)"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko           > "$FAKE_STATE/ping"
echo ko           > "$FAKE_STATE/dns"
echo ko           > "$FAKE_STATE/ssh"
echo Stopped      > "$FAKE_STATE/tailscale"
printf '#!/bin/bash\nprintf "ssh_restart\\t0.9\\tstub\\tssh muet\\n"\n' > "$T/ai-stub"
chmod +x "$T/ai-stub"
run_mon check
check "ssh_restart exécuté par l'IA" "audit_action ssh_restart ia"
check "décision IA auditée" "audit_action ai_call"
teardown

# ============================================================== test 6 ======
echo "6. confiance IA trop basse → ignorée"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko           > "$FAKE_STATE/ping"
echo ko           > "$FAKE_STATE/dns"
echo ko           > "$FAKE_STATE/ssh"
echo Stopped      > "$FAKE_STATE/tailscale"
printf '#!/bin/bash\nprintf "ssh_restart\\t0.2\\tstub\\tpas sur\\n"\n' > "$T/ai-stub"
chmod +x "$T/ai-stub"
run_mon check
check "aucune action IA exécutée" "! grep -qE '^[0-9]*	ssh_restart	1	ia' '$MON_STATE_DIR/audit.tsv'"
check "refus tracé" "grep -q 'confiance 0.2' '$MON_STATE_DIR/audit.tsv'"
teardown

# ============================================================== test 7 ======
echo "7. mode report → toujours bavard, jamais réparateur"
setup
echo disconnected > "$FAKE_STATE/wifi"
run_mon report
check "code retour 1" "[ $RC -eq 1 ]"
check "rapport non vide" "[ -s '$T/out' ]"
check "aucune action en mode report" "[ ! -s '$MON_STATE_DIR/audit.tsv' ]"
teardown

# ============================================================== test 8 ======
echo "8. Wi-Fi soft-bloqué → rfkill_unblock tenté"
setup
echo disconnected > "$FAKE_STATE/wifi"
touch "$FAKE_STATE/rfkill_blocked"
run_mon check
check "rfkill_unblock tenté" "audit_action rfkill_unblock"
check "rfkill list appelé" "grep -q 'rfkill list' '$CALLS'"
teardown

echo "9. wifi rétabli → repair_net enchaîne dans le même passage"
setup
echo disconnected > "$FAKE_STATE/wifi"
echo ko > "$FAKE_STATE/ping"
echo ko > "$FAKE_STATE/dns"
MON_DRY_RUN=0 run_mon check
check "wifi_reapply exécuté" "audit_action wifi_reapply"
check "nm_restart via repair_net" "audit_action nm_restart"
teardown

echo "10. wifi_provision (mode repair) → recrée le profil depuis SSID+clé"
setup
MON_WIFI_SSID=Livebox-Test MON_WIFI_PSK=secret \
    MON_DRY_RUN=0 run_mon repair wifi_provision
check "wifi_provision exécuté" "audit_action wifi_provision"
check "nmcli device wifi connect appelé" "grep -q 'device wifi connect' '$CALLS'"
check "SSID passé en argument" "grep -q 'Livebox-Test' '$CALLS'"
teardown

echo "11. wifi_provision sans SSID/PSK → refus explicite, rien exécuté"
setup
MON_DRY_RUN=0 run_mon repair wifi_provision
check "refus tracé" "grep -q 'non renseignés' '$T/boot.log'"
check "aucune commande nmcli connect lancée" "! grep -q 'device wifi connect' '$CALLS'"
teardown

echo
if [ "$FAIL" -eq 0 ]; then
    printf 'Tous les tests passent (%d assertions).\n' "$PASS"
    exit 0
fi
printf '%d échec(s) / %d assertions.\n' "$FAIL" "$PASS"
exit 1
