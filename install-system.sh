#!/usr/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ $EUID -eq 0 ]] || { echo 'Administrator authentication is required.' >&2; exit 1; }
[[ $(cat /sys/class/dmi/id/product_name) == 'Predator PHN16-71' ]] || {
  echo 'This build is only for Predator PHN16-71.' >&2; exit 2;
}
command -v dkms >/dev/null
[[ -d /lib/modules/$(uname -r)/build ]]
for module in acer_wmi facer linuwu_sense; do
  [[ ! -d /sys/module/$module ]] || {
    echo "Competing driver $module is active. Stop it before installing." >&2; exit 3;
  }
done
# Stage the complete source before building; do not change startup until DKMS succeeds.
install -d /usr/src/neo-control-0.1.0 /opt/neo-control/evidence /var/lib/neo-control
install -m 0644 kernel/neo_control.c kernel/Makefile kernel/dkms.conf /usr/src/neo-control-0.1.0/
if [[ ! -d /var/lib/dkms/neo-control/0.1.0 ]]; then
  dkms add -m neo-control -v 0.1.0
fi
dkms build -m neo-control -v 0.1.0 -k "$(uname -r)" --force
dkms install -m neo-control -v 0.1.0 -k "$(uname -r)" --force
install -m 0755 neo_control.py set-permissions.sh restore-auto.sh uninstall-system.sh /opt/neo-control/
install -m 0644 README.md rea-findings.md LICENSE /opt/neo-control/
install -m 0644 evidence/*.json /opt/neo-control/evidence/
getent group linuwu_sense >/dev/null || groupadd --system linuwu_sense
install -m 0644 neo-control.service /etc/systemd/system/neo-control.service
if [[ ! -f /var/lib/neo-control/previous-boot-service ]]; then
  systemctl is-enabled predator-sense-boot-apply.service > /var/lib/neo-control/previous-boot-service || true
fi
systemctl disable predator-sense-boot-apply.service 2>/dev/null || true
cat > /etc/modprobe.d/neo-control.conf <<'EOF'
# Neo Control owns the Acer firmware controls on this PHN16-71.
blacklist acer_wmi
blacklist facer
blacklist linuwu_sense
EOF
systemctl daemon-reload
# Ensure the running module matches the version just built, including reruns.
systemctl stop neo-control.service
if [[ -d /sys/module/neo_control ]]; then
  /opt/neo-control/restore-auto.sh
  modprobe -r neo_control
fi
systemctl enable neo-control.service
systemctl start neo-control.service
/opt/neo-control/set-permissions.sh
cat /sys/devices/platform/neo-control/status
echo 'Neo Control driver installed with DKMS and startup service.'
