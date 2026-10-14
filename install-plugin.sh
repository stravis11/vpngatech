#!/bin/bash
# Install the bar widget into the Omarchy shell (hot-reloads on copy).
set -euo pipefail
src="$(dirname "$(realpath "$0")")/shell-plugin/st87.vpngatech"
dest="$HOME/.config/omarchy/plugins/st87.vpngatech"
omarchy plugin validate "$src"
mkdir -p "$dest"
cp "$src"/manifest.json "$src"/*.qml "$dest"/
echo "installed $dest"
