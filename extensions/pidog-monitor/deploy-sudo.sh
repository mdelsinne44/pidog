#!/bin/bash
# Application des changements PRIVILÉGIÉS du PiDog Monitor — à exécuter avec
#   sudo bash ~/pidog/extensions/pidog-monitor/deploy-sudo.sh
# Idempotent : sans danger de le relancer.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 1/3 pidog-stream.service : creds déplacées vers EnvironmentFile =="
UNIT=/etc/systemd/system/pidog-stream.service
if grep -q '^Environment=STREAM_USER=' "$UNIT"; then
    sed -i \
        -e '/^Environment=STREAM_USER=/d' \
        -e '/^Environment=STREAM_PASSWORD=/d' \
        -e '/^Environment=PYTHONUNBUFFERED=1/a EnvironmentFile=/home/mdelsinne/.config/pidog/stream-credentials' \
        "$UNIT"
    echo "unité patchée : EnvironmentFile ajouté, lignes en clair retirées"
else
    echo "déjà en ordre (aucune ligne STREAM_USER en clair)"
fi
systemctl daemon-reload
systemctl restart pidog-stream.service
echo "pidog-stream.service : $(systemctl is-active pidog-stream.service)"

echo "== 2/3 pidog-monitor : réinstallation des unités (EnvironmentFile monitor.env) =="
cd "$HERE" && ./install.sh

echo "== 3/3 PSK Wi-Fi vers ~/.config/pidog/monitor.env (si récupérable) =="
PROFILE=$(cat /var/lib/pidog-monitor/wifi_profile 2>/dev/null | tr -d '\n')
[ -n "$PROFILE" ] || PROFILE="netplan-wlan0-Livebox-C210"
PSK=$(nmcli -s connection show "$PROFILE" 2>/dev/null \
      | sed -n 's/^802-11-wireless-security.psk:[[:space:]]*//p' | tr -d '[:space:]')
if [ -n "$PSK" ]; then
    sed -i "s|^MON_WIFI_PSK=.*|MON_WIFI_PSK=$PSK|" /home/mdelsinne/.config/pidog/monitor.env
    chmod 600 /home/mdelsinne/.config/pidog/monitor.env
    echo "MON_WIFI_PSK renseignée depuis le profil $PROFILE (fichier 600, non affichée)"
else
    echo "PSK non récupérable via nmcli — à renseigner à la main dans monitor.env"
fi

echo "== terminé. Vérifs : systemctl --failed ; systemctl status pidog-monitor.timer =="
