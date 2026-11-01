#!/bin/bash
# Sauvegarde quotidienne SD -> Google Drive, rep date, logue.
#   cible : gdrive:PiDog-backup/AAAA-MM-JJ/
#   log   : ~/pidog/extensions/pidog-monitor/backup.log
#   retention : 7 jours (purge des dossiers plus vieux)
# Cron : NON planifie (l ancien /etc/cron.d/pidog-backup n a jamais ete cree) — a lancer a la main
set -u
DATE=$(date +%F)
LOG=/home/mdelsinne/pidog/extensions/pidog-monitor/backup.log
DEST="gdrive:PiDog-backup/$DATE"
KEEP_DAYS=7
H=/home/mdelsinne

say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

say "=== Debut sauvegarde $DATE -> $DEST"

if ! ping -c1 -W3 8.8.8.8 >/dev/null 2>&1; then
  say "ABANDON : pas de reseau"
  exit 1
fi

# Contenu cible (essentiel, reconfigurable) - les grosses donnees
# re-telechargeables sont exclues pour rester leger sur le Drive.
PATHS=(/etc
  "$H/.bashrc" "$H/.profile"
  "$H/.config/pidog" "$H/.config/rclone" "$H/.local/bin"
  "$H/pidog" "$H/robot-hat" "$H/GoogleDrive" "$H/mount_drive.sh")

EXCL=(--exclude='*.onnx' --exclude='vosk-model-*' --exclude='*.log'
      --exclude='__pycache__' --exclude='*.bak' --exclude='.git'
      --exclude='*.egg-info' --exclude='memory.db')

# --- Upload en streaming (pas de temporaire : disque deja a 85%) ---
START=$(date +%s)
set -o pipefail
tar --ignore-failed-read --warning=no-file-changed "${EXCL[@]}" \
    -czf - "${PATHS[@]}" 2>>"$LOG" | rclone rcat "$DEST/pidog_backup.tar.gz"
RC=$?
set +o pipefail
# tar renvoie 1 sur "file changed while reading" : non bloquant
if [ "$RC" -gt 1 ]; then
  say "ECHEC upload (rc=$RC)"
  exit "$RC"
fi

SIZE=$(rclone lsf --format ps "$DEST/pidog_backup.tar.gz" 2>/dev/null | awk '{print $1}')
say "Upload OK (${SIZE:-inconnu}) en $(( $(date +%s) - START ))s"

# --- Retention : purge des dossiers plus vieux que KEEP_DAYS ---
CUTOFF=$(date -d "-${KEEP_DAYS} days" +%F)
rclone lsf "gdrive:PiDog-backup/" --dirs-only 2>/dev/null | sed 's#/##' | while read -r d; do
  [ -z "$d" ] && continue
  if [[ "$d" < "$CUTOFF" ]]; then
    say "Retention : purge de $d"
    rclone purge "gdrive:PiDog-backup/$d" >>"$LOG" 2>&1
  fi
done

say "=== Fin sauvegarde $DATE"
