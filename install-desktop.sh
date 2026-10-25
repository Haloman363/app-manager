#!/usr/bin/env bash
# Add App Manager to the application menu, pointing at this checkout.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${XDG_DATA_HOME:-$HOME/.local/share}/applications/io.github.haloman363.AppManager.desktop"
mkdir -p "$(dirname "$dest")"
cat > "$dest" <<DESKTOP
[Desktop Entry]
Type=Application
Name=App Manager
Comment=View and uninstall Flatpak, Homebrew, rpm-ostree, Distrobox, AppImage, Steam and local apps
Exec=/usr/bin/python3 $here/appmanager.py
Icon=system-software-install
Categories=System;PackageManager;
Terminal=false
StartupNotify=true
DESKTOP
update-desktop-database "$(dirname "$dest")" 2>/dev/null || true
echo "Installed $dest"
