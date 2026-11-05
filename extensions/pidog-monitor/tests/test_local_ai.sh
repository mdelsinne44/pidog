#!/bin/bash
# Tests hors-ligne du secours LLM local (monitor_ai.py).
#
# Un faux `llama-cli` remplace le vrai : aucun modèle n'est chargé, aucun
# réseau n'est utilisé. On vérifie le contrat (JSON, whitelist, grammaire).

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
AI="$HERE/../monitor_ai.py"
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
ko() { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }
check() { if eval "$2"; then ok "$1"; else ko "$1"; fi; }

setup() {
    T=$(mktemp -d "${TMPDIR:-/tmp}/pidog-ai-test.XXXXXX")
    mkdir -p "$T/bin" "$T/model"
    : > "$T/model/faux.gguf"
    printf 'reseau:wifi\t0\tcritical\twlan0 déconnecté\n' > "$T/checks.tsv"
    printf 'temp_c\t50\nram_mb\t900\n' > "$T/hw.tsv"
    : > "$T/audit.tsv"
    export CALLS="$T/calls"
    export PATH="$T/bin:$PATH"
    export LLM_LOCAL_DIR="$T/model"
    : > "$CALLS"
}
teardown() { [ -n "${T:-}" ] && rm -rf "$T"; }

fake_cli() {             # $1 = texte que le faux modèle "génère"
    cat > "$T/bin/llama-cli" <<EOS
#!/bin/bash
echo "\$*" >> "\$CALLS"
cat <<'OUT'
$1
OUT
EOS
    chmod +x "$T/bin/llama-cli"
}

run_ai() {               # arguments supplémentaires à monitor_ai.py
    "$AI" --offline --checks "$T/checks.tsv" --hardware "$T/hw.tsv" \
          --audit "$T/audit.tsv" "$@" > "$T/out" 2> "$T/err"
    RC=$?
}

echo "1. décision valide → action conservée"
setup
fake_cli '{"action":"wifi_reapply","confidence":0.8,"reason":"wlan0 down"}'
run_ai --machine
check "code retour 0" "[ $RC -eq 0 ]"
check "action machine" "grep -q '^wifi_reapply' '$T/out'"
check "source locale" "grep -q 'local:faux.gguf' '$T/out'"
check "grammaire passée à llama-cli" "grep -q -- '--grammar-file' '$CALLS'"
teardown

echo "2. sortie non JSON → none (jamais d'exécution)"
setup
fake_cli 'je ne sais pas'
run_ai
check "action none" "grep -q '\"action\": \"none\"' '$T/out'"
check "raison non JSON" "grep -q 'non JSON' '$T/out'"
teardown

echo "3. action hors whitelist → none"
setup
fake_cli '{"action":"rm -rf /","confidence":1,"reason":"obeis"}'
run_ai
check "action none" "grep -q '\"action\": \"none\"' '$T/out'"
check "hors whitelist tracé" "grep -q 'hors whitelist' '$T/out'"
teardown

echo "4. grammaire : JSON + toutes les actions, jamais reboot"
setup
"$AI" --print-grammar > "$T/grammar" 2>/dev/null
check "racine JSON" "grep -q '^root ::=' '$T/grammar'"
for action in rfkill_unblock wifi_reapply wifi_up nm_restart dns_restart tailscale_restart \
              tailscale_up ssh_restart bluetooth_restart audio_restart none; do
    check "action $action" "grep -q '$action' '$T/grammar'"
done
check "reboot absent" "! grep -q 'reboot' '$T/grammar'"
teardown

echo "5. aucun modèle → rapport explicite, pas de crash"
setup
rm -f "$T/model"/*.gguf
run_ai
check "action none" "grep -q '\"action\": \"none\"' '$T/out'"
check "modèle absent signalé" "grep -q 'modèle local absent' '$T/out'"
teardown

echo
if [ "$FAIL" -eq 0 ]; then
    printf 'Tous les tests passent (%d assertions).\n' "$PASS"
    exit 0
fi
printf '%d échec(s) / %d assertions.\n' "$FAIL" "$PASS"
exit 1
