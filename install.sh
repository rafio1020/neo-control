#!/usr/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ $EUID -ne 0 ]] || { echo 'Run this launcher as your normal desktop user.' >&2; exit 1; }
python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk"
pkexec /usr/bin/bash "$PWD/install-system.sh"
if ! id -nG | tr ' ' '\n' | grep -qx linuwu_sense; then
  echo 'Join the linuwu_sense group and log in again before using controls.' >&2
  exit 2
fi
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
echo 'Installed. Open Neo Control from the application menu.'
