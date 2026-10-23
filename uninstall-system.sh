#!/usr/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exit 1
systemctl disable --now neo-control.service
modprobe -r neo_control
dkms remove -m neo-control -v 0.1.0 --all
rm -f /etc/systemd/system/neo-control.service /etc/modprobe.d/neo-control.conf
if [[ -f /var/lib/neo-control/previous-boot-service ]] &&
   [[ $(cat /var/lib/neo-control/previous-boot-service) == enabled ]]; then
  systemctl enable predator-sense-boot-apply.service
fi
systemctl daemon-reload
echo 'Driver removed. Source, app files and test records remain under /opt/neo-control and /usr/src/neo-control-0.1.0.'
