#!/usr/bin/bash
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
[[ $EUID -eq 0 ]] || exit 1
systemctl disable --now neo-control.service
if [[ -d /sys/module/neo_control ]]; then modprobe -r neo_control; fi
dkms remove -m neo-control -v 0.1.0 --all
rm -f /etc/systemd/system/neo-control.service /etc/modprobe.d/neo-control.conf /var/lib/neo-control/reboot-required
python3 /opt/neo-control/setup_support.py --refresh-initramfs
if [[ -f /var/lib/neo-control/previous-boot-service ]] &&
   [[ $(cat /var/lib/neo-control/previous-boot-service) == enabled ]]; then
  systemctl enable predator-sense-boot-apply.service
fi
systemctl daemon-reload
echo 'Driver removed. Reboot to let the original Acer driver load again. Source, app files and test records remain under /opt/neo-control and /usr/src/neo-control-0.1.0.'
