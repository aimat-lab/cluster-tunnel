#!/usr/bin/env bash
# Install the cluster-tunnel logout hook as a systemd *user* service so that
# `ctun logout` runs automatically on every logout / reboot / power off,
# cleanly tearing down all live tunnels and clearing their session records.
#
# Usage:  ./install.sh          # install + enable
#         ./install.sh --remove # disable + uninstall
set -euo pipefail

UNIT="cluster-tunnel-logout.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ "${1:-}" == "--remove" ]]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$DEST_DIR/$UNIT"
    systemctl --user daemon-reload
    echo "Removed $UNIT."
    exit 0
fi

ctun_path="$(command -v ctun || true)"
if [[ -z "$ctun_path" ]]; then
    echo "warning: 'ctun' is not on PATH; the unit defaults to ~/.local/bin/ctun." >&2
    echo "         Edit ExecStop in $DEST_DIR/$UNIT if it lives elsewhere." >&2
fi

mkdir -p "$DEST_DIR"
install -m 0644 "$SRC_DIR/$UNIT" "$DEST_DIR/$UNIT"

# Point ExecStop at the actual ctun if we found one (handles non-default installs).
if [[ -n "$ctun_path" ]]; then
    sed -i "s|^ExecStop=-.*ctun logout$|ExecStop=-$ctun_path logout|" "$DEST_DIR/$UNIT"
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
echo "Installed and enabled $UNIT."
echo "It will run 'ctun logout' on every logout, reboot, or shutdown."
echo "Test the stop hook now with:  systemctl --user stop $UNIT"
