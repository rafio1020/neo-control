#!/usr/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ $EUID -ne 0 ]] || { echo 'Run this launcher as your normal desktop user.' >&2; exit 1; }
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Terminal installs work on KDE/Xfce/Wayland without requiring a GUI polkit agent.
elevate() {
  if [[ -t 0 ]] && command -v sudo >/dev/null; then
    sudo -- "$@"
  elif command -v pkexec >/dev/null; then
    pkexec "$@"
  else
    echo 'Install sudo (terminal) or polkit plus a desktop authentication agent.' >&2
    return 1
  fi
}
[[ $# -le 1 ]] || { echo 'Pass at most one installer option.' >&2; exit 2; }
case "${1:-}" in
  --check) exec python3 "$PWD/setup_support.py" --check ;;
  --install-deps) elevate /usr/bin/python3 "$PWD/setup_support.py" --install-deps; exit ;;
  --help|-h)
    echo 'Usage: ./install.sh [--check | --install-deps]'
    echo 'Run --install-deps first on a fresh CachyOS/Arch or Fedora installation.'
    exit ;;
  '') ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac
python3 "$PWD/setup_support.py" --check
elevate /usr/bin/bash "$PWD/install-system.sh" "$(id -un)"
install -d "$HOME/.local/state/neo-control" "$HOME/.local/share/applications"
if [[ ! -f "$HOME/.local/state/neo-control/previous-hotkey-service" ]]; then
  systemctl --user is-enabled predator-sense-hotkey.service > "$HOME/.local/state/neo-control/previous-hotkey-service" || true
fi
systemctl --user disable --now predator-sense-hotkey.service 2>/dev/null || true
cat > "$HOME/.local/share/applications/neo-control.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Neo Control
Comment=PHN16-71 fans, performance, lighting and battery
Exec=/usr/bin/python3 /opt/neo-control/neo_control.py
Icon=preferences-system
Terminal=false
Categories=System;Settings;HardwareSettings;
Keywords=Acer;Predator;fan;RGB;battery;
StartupWMClass=io.local.NeoControl
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
if [[ -f /var/lib/neo-control/reboot-required ]]; then
  echo 'Installed. Reboot to activate the driver and your control-group membership, then open Neo Control.'
elif ! id -nG | tr ' ' '\n' | grep -qx linuwu_sense; then
  echo 'Installed. Log out and back in to activate control permissions, then open Neo Control.'
else
  echo 'Installed. Open Neo Control from the application menu.'
fi
