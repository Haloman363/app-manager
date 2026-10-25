# App Manager

**One list of everything installed on your Linux desktop, with a checkbox and a Remove button.**

Think Windows "Installed apps", for immutable Fedora desktops (Bazzite, Silverblue, Kinoite)
where software comes from a pile of different places. Flatpaks, Homebrew, layered packages,
AppImages, Steam games and hand-installed apps all show up in one searchable window.

```
┌──────────────────────────────────────────────────────────┐
│ ⟳  ⛶                 App Manager        ☰   [Remove (2)] │
│                   144 apps · 16 updates                  │
│ ┌────────────────────────────┐ [All (144) ▾] [Name ▾]    │
│ │ 🔍 Search installed apps   │                           │
│ └────────────────────────────┘                           │
│ ☑ 🟩 Bambu Studio     Flatpak · 2.8.2 · 1.0 GB       ⓘ   │
│ ☐ ▶  atuin            Homebrew · 18.19 · 46 MB  Update ⓘ │
│ ☑ 🔷 ArtCraft         AppImage · 0.2.0 · 11 MB       ⓘ   │
│ ☐ 🎮 Garry's Mod      Steam                          ⓘ   │
│ ☐ 🧱 Dolphin          System · 26.08 · 13 MB  System ⓘ   │
└──────────────────────────────────────────────────────────┘
```

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

- **Nothing runs until you confirm**, and the dialog lists the exact commands.
- **AppImage and Local removals go to the trash**, so you can get them back.
- **System apps are marked** with an amber "System" label. Removing one is an
  `rpm-ostree override`, which takes effect after a reboot. To undo it:
  `rpm-ostree override reset <package>`.
- **Select-all never picks System apps.** Base-image packages must be ticked by hand.
- **Core packages are locked**: the Plasma shell, System Settings, `sddm`, `kwin`,
  `rpm-ostree` and the `bazzite` meta package cannot be removed from here.
- Other base packages (a file manager, a terminal) *can* be removed. Read the warning first.

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

`./appmanager.py --shot out.png [seconds]` saves a screenshot of the window and quits, which
is handy for checking UI changes without clicking around.

## License

[MIT](LICENSE)
