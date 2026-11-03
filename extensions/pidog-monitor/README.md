# PiDog Monitor

Diagnostic de santé de la Raspberry, pas un superviseur de processus.

## Ce qu'il fait

| Contrôle | Sévérité |
|---|---|
| `ssh`, `lightdm`, `NetworkManager` actifs | critique |
| `x11vnc`, `tailscaled` actifs | optionnel |
| `pidog-agent-stream` actif | optionnel |
| `vcgencmd get_throttled` | critique si sous-tension |
| Température CPU < 80 °C | critique au-delà |
| Disque < 92 %, RAM dispo > 80 Mo | critique au-delà |
| Synchro NTP initiale | informatif |
| Au moins une IPv4 | critique |
| Socket `/tmp/pidog.sock` | informatif |

Il **logge au boot** (uptime < 10 min), **sur changement d'état**, et reste
silencieux sinon — pour ne plus noyer le journal.

## Ce qu'il ne fait plus

* `sleep 120` dans `ExecStart` (bloquait le démarrage du service) ;
* `Restart=on-failure` + `RemainAfterExit=yes` sur un `Type=oneshot`
  (boucle de redémarrage : 567 démarrages en 20 h le 02/11/2026) ;
* `WatchdogSec=120` (ignoré par systemd sur un oneshot, `WatchdogUSec=0`) ;
* `ExecStartPost` mal parenthésés (`a && echo X || echo Y | logger` : `X`
  n'allait jamais dans le journal).

## Fichiers

* `monitor.sh` — le script (aucune dépendance hors coreutils/systemd).
* `pidog-monitor.service` — oneshot, `TimeoutStartSec=30`.
* `pidog-monitor.timer` — `OnBootSec=2min`, puis toutes les 15 min.

L'état précédent est mémorisé dans `/tmp/pidog-monitor.state` (répertoire
inscriptible pour `User=mdelsinne` ; `/run` ne l'est pas).

## Installation

```bash
sudo cp pidog-monitor.service pidog-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pidog-monitor.timer
sudo systemctl start pidog-monitor.service
```

## Vérifier

```bash
systemctl status pidog-monitor.timer
systemctl show pidog-monitor -p NRestarts     # doit rester à 0
systemctl --failed                            # ne doit rien afficher
tail -20 ~/mon-boot.log
```

Un échec critique fait sortir le script en 1 : `systemctl --failed` le rend
visible, sans redémarrage en boucle.
