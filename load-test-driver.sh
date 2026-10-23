#!/usr/bin/bash
# Load this build for the current boot only. No settings are written on load.
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ $EUID -eq 0 ]] || { echo 'Run with pkexec or sudo.' >&2; exit 1; }
[[ $(cat /sys/class/dmi/id/product_name) == 'Predator PHN16-71' ]] || exit 2
[[ $(modinfo -F vermagic kernel/neo_control.ko) == "$(uname -r) "* ]] || {
  echo 'Build does not match the running kernel.' >&2; exit 3;
}
for module in facer acer_wmi linuwu_sense; do
  [[ ! -d /sys/module/$module ]] || {
    echo "Competing driver $module is loaded; stopping without changing it." >&2; exit 4;
  }
done
if [[ -d /sys/module/neo_control ]]; then
  rmmod neo_control
fi
insmod "$PWD/kernel/neo_control.ko"
getent group linuwu_sense >/dev/null || groupadd --system linuwu_sense
for attribute in fan_control profile keepalive battery_limit lighting zones; do
  chgrp linuwu_sense "/sys/devices/platform/neo-control/$attribute"
  chmod 0660 "/sys/devices/platform/neo-control/$attribute"
done
cat /sys/devices/platform/neo-control/status
