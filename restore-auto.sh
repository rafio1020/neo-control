#!/usr/bin/bash
set -euo pipefail
if [[ -e /sys/devices/platform/neo-control/fan_control ]]; then
  echo auto > /sys/devices/platform/neo-control/fan_control
fi
