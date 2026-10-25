#!/usr/bin/env bash
# Add App Manager (menu entry + icon) to this user account, pointing at this checkout.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/applications/io.github.haloman363.AppManager.desktop"
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps"
cp "$here/data/io.github.haloman363.AppManager.svg" "$data/icons/hicolor/scalable/apps/"
gtk-update-icon-cache -q -t "$data/icons/hicolor" 2>/dev/null || true
cat > "$dest" <<DESKTOP
[Desktop Entry]
Type=Application
Name=App Manager
Comment=View and uninstall Flatpak, Homebrew, rpm-ostree, Distrobox, AppImage, Steam and local apps
Exec=python3 "$here/appmanager.py"
Icon=io.github.haloman363.AppManager
Categories=System;PackageManager;
Terminal=false
StartupNotify=true
DESKTOP
update-desktop-database "$(dirname "$dest")" 2>/dev/null || true
echo "Installed $dest"
