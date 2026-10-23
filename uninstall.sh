#!/usr/bin/bash
set -euo pipefail
pkexec /usr/bin/bash /opt/neo-control/uninstall-system.sh
rm -f "$HOME/.local/share/applications/neo-control.desktop"
if [[ -f "$HOME/.local/state/neo-control/previous-hotkey-service" ]] &&
   [[ $(cat "$HOME/.local/state/neo-control/previous-hotkey-service") == enabled ]]; then
  systemctl --user enable --now predator-sense-hotkey.service
fi
echo 'Removed Neo Control startup and launcher; original enabled services restored.'
