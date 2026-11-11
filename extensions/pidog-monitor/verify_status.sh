#!/bin/bash
# Vérification rapide de l'état monitor (sans rien réparer).
# Usage : bash verify_status.sh

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
STATE_TMP=$(mktemp -d /tmp/verify-mon.XXXXXX)
trap 'rm -rf "$STATE_TMP"' EXIT

echo "=== monitor.sh json (état machine) ==="
MON_STATE_DIR="$STATE_TMP" MON_BOOT_LOG="$STATE_TMP/boot.log" \
    "$HERE/monitor.sh" json 2>/dev/null \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("ok:", d["ok"], "| critical:", d["critical"], "| degraded:", d["degraded"])
for c in d["checks"]:
    if any(k in c["name"] for k in ("stream", "lightdm", "llm", "robot", "etat")):
        print("  %-34s -> %s (%s)  %s" % (c["name"], "OK" if c["ok"] else "KO",
                                          c["severity"], c["detail"]))
'

echo "=== modèle en mémoire ? (doit être vide hors IA) ==="
if pgrep -f llama-cli >/dev/null 2>&1; then
    pgrep -af llama-cli
else
    echo "aucun llama-cli en mémoire"
fi

echo "=== RAM dispo ==="
free -m | head -2
