#!/usr/bin/bash
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ $EUID -eq 0 ]] || { echo 'Administrator authentication is required.' >&2; exit 1; }
desktop_user=${1:-}
[[ -n "$desktop_user" && "$desktop_user" != -* ]] &&
  [[ $(id -u -- "$desktop_user") -ne 0 ]] || {
  echo 'Run ./install.sh as your normal desktop user.' >&2; exit 2;
}
python3 setup_support.py --check
conflict=false
for module in acer_wmi facer linuwu_sense; do
  if [[ -d /sys/module/$module ]]; then conflict=true; fi
done
# Stage and build before changing driver ownership or boot configuration.
install -d /usr/src/neo-control-0.1.0 /opt/neo-control/evidence /var/lib/neo-control
install -m 0644 kernel/neo_control.c kernel/Makefile kernel/dkms.conf /usr/src/neo-control-0.1.0/
if [[ ! -d /var/lib/dkms/neo-control/0.1.0 ]]; then
  dkms add -m neo-control -v 0.1.0
fi
dkms build -m neo-control -v 0.1.0 -k "$(uname -r)" --force
dkms install -m neo-control -v 0.1.0 -k "$(uname -r)" --force
install -m 0755 neo_control.py setup_support.py load-driver.sh set-permissions.sh restore-auto.sh uninstall-system.sh /opt/neo-control/
install -m 0644 README.md rea-findings.md LICENSE /opt/neo-control/
install -m 0644 evidence/*.json /opt/neo-control/evidence/
getent group linuwu_sense >/dev/null || groupadd --system linuwu_sense
usermod -aG linuwu_sense "$desktop_user"
# Preserve this project's previous blacklist if boot-image generation fails.
backup=$(mktemp)
had_blacklist=false
if [[ -f /etc/modprobe.d/neo-control.conf ]]; then
  cp /etc/modprobe.d/neo-control.conf "$backup"
  had_blacklist=true
fi
cat > /etc/modprobe.d/neo-control.conf <<'EOF'
# Neo Control owns the Acer firmware controls on this PHN16-71.
blacklist acer_wmi
blacklist facer
blacklist linuwu_sense
EOF
if ! python3 setup_support.py --refresh-initramfs; then
  if $had_blacklist; then
    cp "$backup" /etc/modprobe.d/neo-control.conf
  else
    rm -f /etc/modprobe.d/neo-control.conf
  fi
  rm -f "$backup"
  echo 'Boot-image update failed; the previous blacklist was restored. Repair boot-image generation and rerun ./install.sh before rebooting.' >&2
  exit 1
fi
rm -f "$backup"
install -m 0644 neo-control.service /etc/systemd/system/neo-control.service
if [[ ! -f /var/lib/neo-control/previous-boot-service ]]; then
  systemctl is-enabled predator-sense-boot-apply.service > /var/lib/neo-control/previous-boot-service || true
fi
systemctl disable predator-sense-boot-apply.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable neo-control.service
if $conflict; then
  touch /var/lib/neo-control/reboot-required
  echo 'Driver built and installed for next boot. Reboot to switch from the active Acer driver.'
  exit 0
fi
# Reruns replace our own module only; competing drivers are never unloaded here.
systemctl stop neo-control.service
if [[ -d /sys/module/neo_control ]]; then
  /opt/neo-control/restore-auto.sh
  modprobe -r neo_control
fi
if ! systemctl start neo-control.service; then
  echo 'Driver could not start. Check journalctl -u neo-control.service and Secure Boot/module signing; see README.md.' >&2
  exit 1
fi
rm -f /var/lib/neo-control/reboot-required
cat /sys/devices/platform/neo-control/status
echo 'Neo Control driver installed with DKMS and startup service.'
