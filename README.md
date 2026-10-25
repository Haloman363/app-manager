<p align="center">
  <img src="docs/assets/icon.png" width="96" alt="App Manager icon">
</p>

<h1 align="center">App Manager</h1>

<p align="center">
  <b>One list of everything installed on your Linux desktop, with a checkbox and a Remove button.</b>
</p>

<p align="center">
  <a href="https://haloman363.github.io/app-manager/">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-finds">What it finds</a> ·
  <a href="#safety">Safety</a>
  <br><br>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="GTK 4 + libadwaita" src="https://img.shields.io/badge/GTK4-libadwaita-3584e4">
  <img alt="Python 3" src="https://img.shields.io/badge/python-3-yellow">
  <img alt="Made for Bazzite, Silverblue and Kinoite" src="https://img.shields.io/badge/for-Bazzite%20%7C%20Silverblue%20%7C%20Kinoite-5b6270">
</p>

<p align="center">
  <picture>
    <source srcset="docs/assets/main-light.png" media="(prefers-color-scheme: light)">
    <img src="docs/assets/main-dark.png" width="760" alt="App Manager listing Flatpak, Homebrew, AppImage, Steam and system apps">
  </picture>
</p>

Think Windows "Installed apps", for immutable Fedora desktops (Bazzite, Silverblue, Kinoite)
where software comes from a pile of different places. Flatpaks, Homebrew, layered packages,
AppImages, Steam games and hand-installed apps all show up in one searchable window.

## Quick start

```bash
git clone https://github.com/Haloman363/app-manager.git
cd app-manager
./install-desktop.sh     # adds "App Manager" to your application menu
./appmanager.py          # or just run it right now
```

That is it. There is nothing to build and nothing to pip install. It needs Python 3, GTK 4,
libadwaita 1.5+ and PyGObject, which are already on Fedora KDE and GNOME.

## How to use it

1. **Find an app.** Type anywhere to search, or pick a source from the first dropdown.
2. **Tick the checkbox** next to each app you want gone.
3. **Press Remove.** A dialog shows the exact commands before anything runs. Confirm, done.

Other things you can do:

| I want to... | Do this |
|--------------|---------|
| See details or copy the remove command | Click the **ⓘ** button on a row |
| Update one app | Click the blue **Update** button on its row |
| Update everything | **☰ menu → Update all** |
| Free space | **☰ menu → Remove unused Flatpak runtimes** or **Clean Homebrew cache** |
| Sort by size | Use the second dropdown (Name / Size / Source) |
| Tick everything shown | Click the select-all button, or press **Ctrl+A** |
| Reload the list | Click ⟳, or press **F5** |

| Shortcut | Action |
|----------|--------|
| Just start typing | Search |
| Ctrl+F | Focus the search box |
| F5 / Ctrl+R | Refresh |
| Ctrl+A | Select all shown (never System apps) |
| Delete | Remove the ticked apps (asks first) |

## What it finds

| Source | Where it looks | Remove does |
|--------|----------------|-------------|
| **Flatpak** | `flatpak list` | `flatpak uninstall`, with an optional "also delete app data" box |
| **Homebrew** | `brew leaves` (what you installed, not dependencies) | `brew uninstall` |
| **rpm-ostree** | packages you layered | `rpm-ostree uninstall` (needs a reboot) |
| **Distrobox** | `distrobox list` | `distrobox rm -f` |
| **AppImage** | `*.AppImage` files in common folders, plus apps with a menu launcher | Moves the file and its launcher to the trash |
| **Steam** | Steam shortcuts in your app menu | Opens Steam's own uninstall prompt |
| **Local** | Menu entries you added by hand (manual installs, web apps) | Moves the entry and its `~/.local/opt` folder or `~/.local/bin` script to the trash |
| **System** | Apps shipped in the base OS image | `rpm-ostree override remove` (reboot, reversible) |

Sources that are not installed on your machine (no `brew`, no `distrobox`) are skipped quietly.

## Safety

<p align="center">
  <img src="docs/assets/remove-dark.png" width="640" alt="Confirmation dialog showing the exact commands that will run">
</p>

- **Nothing runs until you confirm**, and the dialog lists the exact commands.
- **AppImage and Local removals go to the trash**, so you can get them back.
- **System apps are marked** with an amber "System" label. Removing one is an
  `rpm-ostree override`, which takes effect after a reboot. To undo it:
  `rpm-ostree override reset <package>`.
- **Select-all never picks System apps.** Base-image packages must be ticked by hand.
- **Core packages are locked**: the Plasma shell, System Settings, `sddm`, `kwin`,
  `rpm-ostree` and the `bazzite` meta package cannot be removed from here.
- Other base packages (a file manager, a terminal) *can* be removed. Read the warning first.

<p align="center">
  <img src="docs/assets/system-dark.png" width="640" alt="System apps carry an amber label; core packages are greyed out">
</p>

## FAQ

**Why can't I tick some apps?** They are locked: either no package owns that menu entry, or
it belongs to a protected core package. Click ⓘ to see which.

**Why do I see fewer Homebrew apps than `brew list`?** Only the formulae you installed
directly are listed. Dependencies are hidden so you can't break other tools by accident.

**Where did a removed AppImage go?** Your trash. Restore it from Dolphin or Files.

**It says "N updates" but a row has no Update button.** Only Flatpak and Homebrew apps can
be updated from here, and the badge appears on rows that exist in the list.

**Can I script it?** `./appmanager.py --list` prints every app and its remove command as
tab-separated text, without opening a window.

## Limits

- No update check for AppImage, rpm-ostree or Steam apps.
- Toolbox containers are not listed, only Distrobox.
- Removing an app does not hunt down every leftover config folder.

## For developers

It is a single file, `appmanager.py`. Each source is a small generator function that yields
`App` records (name, source, version, size, the command that removes it). The window just
lists them.

`./appmanager.py --demo` fills the window with fake sample apps and never runs a command,
so it is safe for trying the UI and taking screenshots. `--shot out.png [seconds]` saves a
screenshot of the window and quits.

## License

[MIT](LICENSE)
