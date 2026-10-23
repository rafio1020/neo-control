#!/usr/bin/bash
set -euo pipefail
for attribute in fan_control profile keepalive battery_limit lighting zones; do
  chgrp linuwu_sense "/sys/devices/platform/neo-control/$attribute"
  chmod 0660 "/sys/devices/platform/neo-control/$attribute"
done
