#!/usr/bin/bash
# Use the distribution's standard command search path, not Fedora's sbin path.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
for module in acer_wmi facer linuwu_sense; do
  if [[ -d /sys/module/$module ]]; then
    echo "Competing driver $module is loaded. Reboot after installation; see README.md if it persists." >&2
    exit 1
  fi
done
exec modprobe neo_control
