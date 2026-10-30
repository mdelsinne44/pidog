#!/bin/bash
# Wrapper pour sondes rapides + check auto-repair
# Frequency : timer 30s (sondes), repair toutes les 4 executions (2min)
HEART=/tmp/pidog-monitor-heartbeat
mkdir -p /tmp/pidog-monitor

echo  > 

# Sondes (rapide)
bash /home/mdelsinne/pidog/extensions/pidog-monitor/monitor.sh || true

# Auto-repair si échec : toutes les 4 executions (~2min)
if [ -f /tmp/pidog-monitor-failcount ]; then
  FAIL=
else
  FAIL=0
fi
FAIL=1
echo  > /tmp/pidog-monitor-failcount

if [  -ge 4 ]; then
  echo Auto-repair triggered at  >> /tmp/pidog-monitor-repair.log
  # Insert repair commands (bluetooth, wifi, audio, robot hat)
  sudo systemctl restart bluetooth || true
  # Add your specific repair script here
  FAIL=0
fi
echo 0 > /tmp/pidog-monitor-failcount
