#!/bin/bash
# PiDog Monitor — installation / mise à jour idempotente.
#
#   sudo ./install.sh          # installe et démarre le timer
#   sudo ./install.sh --check  # vérifie sans rien modifier
#
# Ce que fait le script :
#   * crée /var/lib/pidog-monitor (état + budget d'essais, survit aux reboots) ;
#   * copie les unités dans /etc/systemd/system ;
#   * supprime le symlink mort pidog-agent.service s'il traîne encore ;
#   * désactive le service au boot (le timer suffit : plus de double exécution) ;
#   * active le timer et lance un premier diagnostic.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
STATE_DIR="${MON_STATE_DIR:-/var/lib/pidog-monitor}"
UNIT_DIR="/etc/systemd/system"
# Le script tourne via sudo : `id -un` vaudrait "root" et le service (User=
# mdelsinne) ne pourrait plus écrire son état. On cible l'utilisateur appelant.
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || id -gn)"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }
die() { printf 'ERREUR : %s\n' "$*" >&2; exit 1; }

[ -f "$HERE/monitor.sh" ] || die "monitor.sh introuvable dans $HERE"
[ -f "$HERE/monitor_ai.py" ] || die "monitor_ai.py introuvable dans $HERE"

say "== PiDog Monitor — $([ "$CHECK_ONLY" = 1 ] && echo vérification || echo installation) =="
say "état        : $STATE_DIR (propriétaire $RUN_USER)"
say "unités      : $UNIT_DIR"

if [ "$CHECK_ONLY" = 1 ]; then
    command -v systemd-analyze >/dev/null && \
        systemd-analyze verify "$HERE/pidog-monitor.service" "$HERE/pidog-monitor.timer" 2>&1
    "$HERE/monitor.sh" report
    exit $?
fi

# 1. Répertoire d'état persistant, possédé par l'utilisateur du service.
sudo install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0755 "$STATE_DIR" \
    || die "création de $STATE_DIR impossible"
# Rattrapage si le répertoire existait déjà en root (ancienne installation).
sudo chown "$RUN_USER:$RUN_GROUP" "$STATE_DIR" 2>/dev/null || true

# 2. Unités.
for unit in pidog-monitor.service pidog-monitor.timer; do
    sudo install -m 0644 "$HERE/$unit" "$UNIT_DIR/$unit" \
        || die "copie de $unit impossible"
done
chmod +x "$HERE/monitor.sh" "$HERE/monitor_ai.py" 2>/dev/null || true

# 3. Symlink mort laissé par l'ancienne unité pidog-agent.service.
LINK="$UNIT_DIR/multi-user.target.wants/pidog-agent.service"
if [ -L "$LINK" ] && [ ! -e "$LINK" ]; then
    sudo rm -f "$LINK" && say "symlink mort supprimé : $LINK"
fi
# Idem pour un éventuel pidog-agent.service orphelin dans /etc/systemd/system.
if [ -L "$UNIT_DIR/pidog-agent.service" ] && [ ! -e "$UNIT_DIR/pidog-agent.service" ]; then
    sudo rm -f "$UNIT_DIR/pidog-agent.service" && say "symlink mort supprimé : $UNIT_DIR/pidog-agent.service"
fi

# 4. Rechargement + cadence par le timer uniquement.
sudo systemctl daemon-reload
sudo systemctl disable --now pidog-monitor.service >/dev/null 2>&1 || true
sudo systemctl enable --now pidog-monitor.timer \
    || die "activation de pidog-monitor.timer impossible"

# 5. Premier passage (exit 1 = alerte critique : ce n'est pas une erreur
#    d'installation, on l'affiche sans faire échouer le script).
say ""
say "== premier diagnostic =="
sudo systemctl start pidog-monitor.service || true
systemctl show pidog-monitor.service -p Result -p ExecMainStatus --no-pager
say ""
say "Vérifier ensuite :"
say "  systemctl status pidog-monitor.timer"
say "  systemctl --failed"
say "  tail -20 ~/mon-boot.log"
