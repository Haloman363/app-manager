#!/usr/bin/env python3
"""Control-panel-style app manager for Linux: Flatpak, Snap, Homebrew, AppImage, Steam, native packages and containers.

GUI: ./appmanager.py        Headless check: ./appmanager.py --list        Try the UI safely: ./appmanager.py --demo
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.request
import http.client
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0"
if "--version" in sys.argv:
    print(f"App Manager {VERSION}")
    sys.exit(0)

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    assert hasattr(Adw, "AlertDialog")  # libadwaita 1.5+
except (ImportError, ValueError, AssertionError) as e:
    sys.exit(f"App Manager needs PyGObject, GTK 4 and libadwaita 1.5+ ({e or 'libadwaita is too old'}).\n"
             "  Fedora:        sudo dnf install python3-gobject gobject-introspection gtk4 libadwaita\n"
             "  Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
             "  Arch:          sudo pacman -S python-gobject gtk4 libadwaita\n"
             "  openSUSE:      sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1")

os.environ["PATH"] = os.environ.get("PATH", "/usr/bin:/bin") + f":{Path.home()}/.local/bin:/home/linuxbrew/.linuxbrew/bin"  # desktop launches often lack these on PATH

HOME = Path.home()
DEMO = "--demo" in sys.argv  # fake sample apps; nothing is ever executed (for screenshots and trying the UI)
APPIMAGE_DIRS = [HOME / d for d in ("Applications", "AppImages", ".local/bin", "bin", "Downloads", "Desktop")] + [Path("/opt")]
APP_ID = "io.github.haloman363.AppManager"
REPO = "Haloman363/app-manager"
SELF = Path(__file__).resolve()
TIMER = "app-manager-update.timer"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "systemd/user"
OPT = (HOME / ".local/opt").resolve()  # resolved: /home is a symlink to /var/home
PROTECTED = {"bazzite", "systemsettings", "gdm3", "dnf5", "network-manager", "policykit-1"}  # exact names never offered for removal
PROTECTED_BASE = ("plasma-desktop", "plasma-workspace", "plasma-systemsettings", "kde-settings", "kde-plasma", "kwin", "gnome-shell",
                  "gnome-control-center", "gnome-session", "gnome-settings-daemon", "mutter", "ubuntu-desktop", "ubuntu-session",
                  "xfce4-session", "xfce4-settings", "cinnamon", "mate-session", "budgie-desktop", "sddm", "gdm", "lightdm",
                  "systemd", "networkmanager", "network-manager", "polkit", "xorg", "xwayland", "pipewire", "wireplumber",
                  "pulseaudio", "grub", "plymouth", "sudo", "ublue", "rpm-ostree", "dnf", "apt", "pacman", "zypper", "flatpak",
                  "snapd")  # name or name-*: session, display, audio, boot and package-manager core
SAFE_PATH = "/usr/bin:/usr/sbin:/bin:/sbin:/snap/bin"  # root-owned dirs only: privileged commands must never resolve to something in ~/.local/bin
DESKTOPS = {d for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d}
DATA_DIRS = [HOME / ".config", HOME / ".local/share", HOME / ".cache"]
SKIP_DIRS = {"applications", "autostart", "icons", "fonts", "systemd", "mime", "themes", "keyrings", "flatpak", "trash", "pulse", "pipewire",
             "dconf", "gtk-3.0", "gtk-4.0", "dbus-1", "gnupg", "git", "ssh", "nautilus", "konsole", "plasma", "kde", "steam", "mozilla",
             "pip", "fontconfig", "user-dirs"}  # shared or system-owned folders that merely look like an app name
REMOVE = {  # package manager -> (binary, arguments before the package, command prefix)
    "rpm-ostree": ("rpm-ostree", ["override", "remove"], []),
    "dnf": ("dnf", ["remove", "-y", "--setopt=clean_requirements_on_remove=False"], []),
    "apt-get": ("apt-get", ["remove", "-y"], ["env", "DEBIAN_FRONTEND=noninteractive"]),
    "pacman": ("pacman", ["-R", "--noconfirm"], []),
    "zypper": ("zypper", ["--non-interactive", "remove"], []),
}
CELLAR = "/home/linuxbrew/.linuxbrew/Cellar"
DESKTOP_DIR = HOME / ".local/share/applications"


@dataclass
class App:
    name: str
    source: str
    version: str
    size: str
    cmd: list  # removes the app when run
    icon: str = ""  # theme icon name or absolute file path
    bytes: int = 0  # for sorting
    path: str = ""  # install location

    @property
    def key(self):  # identity used to match update lists
        return (self.source, self.path if self.source == "AppImage" else self.cmd[-1] if self.cmd else self.name)


def run(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def parse_size(text):
    m = re.match(r"([\d.]+)\s*([kKMGT]?)(?:B|bytes)", text.replace("\xa0", " ").strip())
    return int(float(m[1]) * 1000 ** " KMGT".index(m[2].upper() or " ")) if m else 0  # flatpak prints base-1000


def flatpak_path(appid):
    return next((str(d / appid) for d in (HOME / ".local/share/flatpak/app", Path("/var/lib/flatpak/app")) if (d / appid).exists()), "")


def flatpak_apps():
    for line in run("flatpak", "list", "--app", "--columns=application,name,version,size").splitlines():
        appid, name, ver, size = (line.split("\t") + ["", "", "", ""])[:4]
        yield App(name or appid, "Flatpak", ver, size, ["flatpak", "uninstall", "-y", "--noninteractive", appid], appid, parse_size(size), flatpak_path(appid))


def brew_apps():
    leaves = set(run("brew", "leaves").split())  # skip dependencies; removing them breaks other formulae
    for line in run("brew", "list", "--formula", "--versions").splitlines():
        name, *vers = line.split()
        if name in leaves:
            kb = run("du", "-sk", f"{CELLAR}/{name}").split()
            size = int(kb[0]) * 1024 if kb else 0
            yield App(name, "Homebrew", " ".join(vers), human(size) if size else "", ["brew", "uninstall", name], "", size, f"{CELLAR}/{name}")


def deployment():  # newest rpm-ostree deployment (the pending one, if any)
    try:
        return json.loads(run("rpm-ostree", "status", "--json"))["deployments"][0]
    except (ValueError, KeyError, IndexError):
        return {}


def ostree_apps():
    for pkg in deployment().get("requested-packages", []):
        yield App(pkg, "rpm-ostree", "", "", ["rpm-ostree", "uninstall", pkg])


def distrobox_apps():
    for line in run("distrobox", "list").splitlines()[1:]:
        cols = [c.strip() for c in line.split("|")]
        if len(cols) >= 4:
            yield App(cols[1], "Distrobox", cols[3], "", ["distrobox", "rm", "-f", cols[1]])


def toolbox_apps():
    fmt = "{{.Names}}\t{{.Image}}"
    for line in run("podman", "ps", "-a", "--filter", "label=com.github.containers.toolbox=true", "--format", fmt).splitlines():
        name, _, image = line.partition("\t")
        if name:
            yield App(name, "Toolbox", image.rsplit("/", 1)[-1], "", ["toolbox" if shutil.which("toolbox") else "podman", "rm", "-f", name])


SNAP_RUNTIME = re.compile(r"^(core\d*|snapd|snapd-desktop-integration|bare|gtk-common-themes|gnome-\d+-\d+.*|mesa-core\d+|kf[56]-.*|kde-frameworks-.*)$")


def snap_apps():
    for line in run("snap", "list").splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6 or SNAP_RUNTIME.match(cols[0]) or "base" in cols[-1] or "snapd" in cols[-1]:
            continue
        name, ver, rev = cols[:3]
        try:
            size = os.stat(f"/var/lib/snapd/snaps/{name}_{rev}.snap").st_size
        except OSError:
            size = 0
        yield App(name, "Snap", ver, human(size) if size else "", root_cmd("snap", "remove", name), name, size)


def system_pm():
    return next((pm for pm in REMOVE if shutil.which(REMOVE[pm][0], path=SAFE_PATH)), "")


def root_cmd(*argv):  # run as root through polkit with absolute, root-owned binaries; [] if either is missing
    exe, pkexec = shutil.which(argv[0], path=SAFE_PATH), shutil.which("pkexec", path=SAFE_PATH)
    return [pkexec, exe, *argv[1:]] if exe and pkexec else []


SYSTEM_PM = "rpm-ostree" if DEMO else system_pm()
IMMUTABLE = SYSTEM_PM == "rpm-ostree"
SYSTEM_NOTE = (  # shown on System rows and in the remove confirmation
    "Part of the base OS image. Removal is an rpm-ostree override: it applies after a reboot and can break the desktop if you "
    "remove something it needs. Undo with 'rpm-ostree override reset <package>'." if IMMUTABLE else
    f"Installed with the system package manager ({SYSTEM_PM or 'unknown'}). Removal asks for your admin password and can remove "
    "other packages that depend on it, or break the desktop if you remove something it needs.")


def is_protected(pkg):
    p = pkg.lower()
    return p in PROTECTED or any(p == b or p.startswith(b + "-") for b in PROTECTED_BASE)


def package_owner(pm, path):  # (name, version, size in bytes) of the package owning a file; name "" if unowned or ambiguous
    if pm in ("rpm-ostree", "dnf", "zypper"):
        out = run("rpm", "-qf", "--qf", "%{NAME} %{VERSION} %{SIZE}\n", path).strip()
        name, _, rest = ("", "", "") if "\n" in out else out.partition(" ")
        ver, _, size = rest.partition(" ")
        return name, ver, int(size) if size.isdigit() else 0
    if pm == "apt-get":
        out = run("dpkg-query", "-S", path).strip()
        head = out.partition(": ")[0] if out and "\n" not in out else ""
        name = "" if "," in head else head.split(":")[0]
        ver, _, kib = run("dpkg-query", "-W", "-f", "${Version} ${Installed-Size}", name).partition(" ") if name else ("", "", "")
        return name, ver, int(kib) * 1024 if kib.isdigit() else 0
    if pm == "pacman":
        out = run("pacman", "-Qo", "-q", path).strip()
        name = "" if "\n" in out else out
        parts = run("pacman", "-Q", name).split() if name else []
        return name, parts[1] if len(parts) == 2 else "", 0
    return "", "", 0


def system_cmd(pm, pkg):  # remove command for a base/native package, or [] when it must not be offered
    if not pm or not pkg or pkg.startswith("-") or is_protected(pkg):
        return []
    binary, args, pre = REMOVE[pm]
    exe = shutil.which(binary, path=SAFE_PATH)
    if not exe:
        return []
    if pm == "rpm-ostree":  # polkit-aware by itself
        return [exe, *args, pkg]
    pkexec = shutil.which("pkexec", path=SAFE_PATH)
    return [pkexec, *pre, exe, *args, pkg] if pkexec else []


def leftovers(a):  # config/data/cache folders named like the app; the user must opt in to trashing them
    if DEMO:
        return [f"~/.config/{a.name.lower().replace(' ', '-')}"] if a.source in ("AppImage", "Local", "Homebrew") and a.bytes > 100 * 2**20 else []
    if a.source not in ("AppImage", "Local", "Homebrew"):
        return []
    bases = {a.name.lower(), Path(a.path).stem.lower() if a.source == "AppImage" else ""}
    names = {n for b in bases for n in (b, b.replace(" ", "-"), b.replace(" ", "")) if len(n) >= 3} - SKIP_DIRS
    dirs = [d for d in DATA_DIRS if a.source != "Homebrew" or d.name != ".config"]  # a formula's name often matches hand-written config (git, tmux, nvim)
    return [str(c) for d in dirs if d.is_dir() for c in d.iterdir() if c.name.lower() in names]


def pending_updates():  # {(source, id)} with a newer version available
    if DEMO:
        return {(d[1], d[2]) for d in DEMO_APPS if d[6]}
    ups = {("Flatpak", i) for i in run("flatpak", "remote-ls", "--updates", "--app", "--columns=application").split()}
    ups |= {("Homebrew", n) for n in run("brew", "outdated", "--formula", "--quiet").split()}
    ups |= {("Snap", line.split()[0]) for line in run("snap", "refresh", "--list").splitlines()[1:] if line.split()}
    tool = shutil.which("appimageupdatetool")
    if tool:
        def check(a):  # exit code 1 = update available; other codes (even for broken update info) = none
            try:
                return a.path if subprocess.run([tool, "-j", a.path], capture_output=True, timeout=40).returncode == 1 else None
            except (OSError, subprocess.SubprocessError):
                return None
        with ThreadPoolExecutor(8) as pool:
            ups |= {("AppImage", p) for p in pool.map(check, list(appimage_apps())) if p}
    return ups


def update_cmd(a):
    return {"Flatpak": ["flatpak", "update", "-y", "--noninteractive", a.cmd[-1]], "Homebrew": ["brew", "upgrade", a.cmd[-1]],
            "Snap": root_cmd("snap", "refresh", a.name), "AppImage": ["appimageupdatetool", "-O", a.path]}[a.source]


def update_all():  # headless: what the daily timer runs. Snap and native packages need root, so they are left to their own updaters
    apps = [a for backend in (flatpak_apps, brew_apps, appimage_apps) for a in backend()]
    pending = pending_updates()
    todo = [a for a in apps if a.key in pending]
    done = 0
    for a in todo:
        print(f"updating {a.name} ({a.source})", flush=True)
        r = subprocess.run(update_cmd(a), capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
        done += r.returncode == 0
        print("  ok" if r.returncode == 0 else f"  failed: {(r.stderr.strip().splitlines() or [r.returncode])[-1]}", flush=True)
    print(f"{done}/{len(todo)} updated", flush=True)
    if todo and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "App Manager", "-i", APP_ID, "App Manager", f"Updated {done} of {len(todo)} apps"])
    return int(done < len(todo))  # non-zero shows the systemd run as failed


def newer(tag, current=VERSION):
    version = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    return version(tag) > version(current)


def latest_release():  # the newest GitHub release if it is newer than this copy; one anonymous request, opt out with APPMANAGER_NO_UPDATE_CHECK=1
    if DEMO or os.environ.get("APPMANAGER_NO_UPDATE_CHECK"):
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": f"app-manager/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.load(resp)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return data if isinstance(data, dict) and newer(str(data.get("tag_name", ""))) else None


def self_update_cmd():  # a git checkout can update itself; anything else is sent to the release page
    here = str(SELF.parent)  # only this project's own checkout, never some other repo the script happens to sit in
    if (SELF.parent / ".git").exists() and shutil.which("git") and REPO.lower() in run("git", "-C", here, "remote", "get-url", "origin").lower():
        return ["env", "GIT_TERMINAL_PROMPT=0", "git", "-C", here, "pull", "--ff-only"]
    return []


def auto_update_units():  # systemd user units for the daily update run
    q = lambda p: '"' + p.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
    service = ("[Unit]\nDescription=App Manager: update apps\n\n[Service]\nType=oneshot\nNice=10\n"
               f"ExecStart={q(sys.executable)} {q(str(SELF))} --update-all\n")
    timer = ("[Unit]\nDescription=Daily App Manager updates\n\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\n"
             "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return service, timer


def auto_update_enabled():
    return run("systemctl", "--user", "is-enabled", TIMER).strip() == "enabled"


def set_auto_update(on):  # True on success
    if DEMO:
        return True
    try:
        if on:
            service, timer = auto_update_units()
            UNIT_DIR.mkdir(parents=True, exist_ok=True)
            (UNIT_DIR / "app-manager-update.service").write_text(service)
            (UNIT_DIR / TIMER).write_text(timer)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
        subprocess.run(["systemctl", "--user", "enable" if on else "disable", "--now", TIMER], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def local_files(df, tokens):  # what removing a user-installed menu entry trashes; never anything outside these roots
    files = [str(df)]
    p = Path(tokens[0]).expanduser() if tokens else None
    if p:
        for q in (p, p.resolve()):
            try:
                files.append(str(OPT / q.relative_to(OPT).parts[0]))
                break
            except (ValueError, IndexError):
                pass
        else:
            if p.parent.resolve() == (HOME / ".local/bin").resolve() and p.exists():
                files.append(str(p))
    return list(dict.fromkeys(files))


def desktop_apps():  # menu entries the package managers above don't own: Steam games, manual installs, base OS apps
    flatpaks = tuple(run("flatpak", "list", "--app", "--columns=application").split())
    pm = system_pm()
    dep = deployment() if pm == "rpm-ostree" else {}  # layered, local and already-removed packages must not show up as removable base packages
    skip = {*dep.get("requested-packages", []), *dep.get("requested-base-removals", []), *dep.get("base-removals", []),
            *(re.sub(r"-[^-]+-[^-]+$", "", p) for p in dep.get("requested-local-packages", []))}
    seen = set()
    for d, user in ((HOME / ".local/share/applications", True), (Path("/usr/local/share/applications"), False), (Path("/usr/share/applications"), False)):
        for f in sorted(d.glob("*.desktop")):
            if f.name in seen:  # a user entry overrides a system one of the same name
                continue
            seen.add(f.name)
            if f.stem == APP_ID or f.stem.startswith(flatpaks):
                continue
            try:
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            g = lambda k: (re.search(rf"^{k}=(.*)$", text, re.M) or [0, ""])[1].strip()
            only, notin, exe = g("OnlyShowIn"), g("NotShowIn"), g("Exec")
            if (g("Type") != "Application" or "true" in (g("NoDisplay").lower(), g("Hidden").lower())
                    or (DESKTOPS and ((only and not DESKTOPS & set(only.split(";"))) or DESKTOPS & set(notin.split(";"))))
                    or ".appimage" in exe.lower() or "/snap/bin/" in exe):
                continue
            name, icon = g("Name") or f.stem, g("Icon")
            steam = re.search(r"steam://rungameid/(\d+)", exe)
            if user and steam:  # uninstall opens Steam's own confirmation
                yield App(name, "Steam", "", "", ["steam", f"steam://uninstall/{steam[1]}"], icon, 0, str(f))
            elif user:
                try:
                    tokens = shlex.split(exe)
                except ValueError:
                    tokens = []
                files = local_files(f, tokens)
                kb = run("du", "-sk", *files[1:]).split()[::2] if len(files) > 1 else []
                size = sum(int(k) for k in kb) * 1024
                yield App(name, "Local", "", human(size) if size else "", ["gio", "trash", *files], icon, size, str(f))
            else:  # native/base package, removed with the distro's package manager
                pkg, ver, size = package_owner(pm, str(f)) if pm else ("", "", 0)
                if pkg in skip:
                    continue
                yield App(name, "System", ver, human(size) if size else "", system_cmd(pm, pkg), icon, size, str(f))


def appimage_apps():
    found = {}  # resolved file -> {extra paths to trash, name, ver}

    def entry(path):
        return found.setdefault(path.resolve(), {"extra": set(), "name": None, "ver": None, "icon": ""})

    for d in APPIMAGE_DIRS:
        try:
            files = list(d.iterdir())
        except OSError:
            continue
        for f in files:
            if f.suffix.lower() == ".appimage" and f.is_file():
                e = entry(f)
                if f.is_symlink():
                    e["extra"].add(f)

    # launcher entries also reveal AppImages kept outside the scanned dirs
    for df in DESKTOP_DIR.glob("*.desktop"):
        try:
            text = df.read_text(errors="ignore")
            m = re.search(r"^Exec=(.*)$", text, re.M)
            tokens = shlex.split(m[1]) if m else []
        except (OSError, ValueError):
            continue
        path = next((Path(t).expanduser() for t in tokens if t.lower().endswith(".appimage")), None)
        if path and path.is_file():
            e = entry(path)
            e["extra"].add(df)
            for key, pat in (("name", r"^Name=(.*)$"), ("ver", r"^X-AppImage-Version=(.*)$"), ("icon", r"^Icon=(.*)$")):
                m = re.search(pat, text, re.M)
                if m:
                    e[key] = m[1]

    for path, e in found.items():
        ver = e["ver"] or (re.search(r"\d+(?:\.\d+)+", path.name) or [""])[0]
        size = path.stat().st_size
        yield App(e["name"] or path.stem, "AppImage", ver, human(size),
                  ["gio", "trash", str(path), *map(str, e["extra"])], e["icon"], size, str(path))  # trash = recoverable


DEMO_APPS = [  # name, source, id, version, MB, icon, has update
    ("VLC", "Flatpak", "org.videolan.VLC", "3.0.21", 356, "org.videolan.VLC", True),
    ("GIMP", "Flatpak", "org.gimp.GIMP", "3.0.4", 784, "org.gimp.GIMP", False),
    ("Discord", "Flatpak", "com.discordapp.Discord", "0.0.98", 412, "com.discordapp.Discord", True),
    ("Kdenlive", "Flatpak", "org.kde.kdenlive", "25.04.2", 523, "org.kde.kdenlive", False),
    ("Flatseal", "Flatpak", "com.github.tchx84.Flatseal", "2.3.1", 3, "com.github.tchx84.Flatseal", False),
    ("Inkscape", "Flatpak", "org.inkscape.Inkscape", "1.4.2", 409, "", False),
    ("OBS Studio", "Flatpak", "com.obsproject.Studio", "31.0.3", 287, "", True),
    ("Blender", "Flatpak", "org.blender.Blender", "4.4.3", 1331, "", False),
    ("bat", "Homebrew", "bat", "0.25.0", 6, "", False),
    ("btop", "Homebrew", "btop", "1.4.2", 3, "btop", False),
    ("ffmpeg", "Homebrew", "ffmpeg", "7.1.1", 54, "", True),
    ("neovim", "Homebrew", "neovim", "0.11.1", 29, "", False),
    ("ripgrep", "Homebrew", "ripgrep", "14.1.1", 5, "", False),
    ("htop", "rpm-ostree", "htop", "3.4.1", 0, "", False),
    ("wireguard-tools", "rpm-ostree", "wireguard-tools", "1.0.2", 0, "", False),
    ("fedora-dev", "Distrobox", "fedora-dev", "fedora:latest", 0, "", False),
    ("ubuntu-build", "Distrobox", "ubuntu-build", "ubuntu:24.04", 0, "", False),
    ("fedora-toolbox-42", "Toolbox", "fedora-toolbox-42", "fedora-toolbox:42", 0, "", False),
    ("rust-dev", "Toolbox", "rust-dev", "fedora-toolbox:42", 0, "", False),
    ("Obsidian", "AppImage", "/home/you/Applications/Obsidian-1.8.9.AppImage", "1.8.9", 118, "", True),
    ("PrusaSlicer", "AppImage", "/home/you/Applications/PrusaSlicer-2.9.1.AppImage", "2.9.1", 164, "", False),
    ("Balena Etcher", "AppImage", "/home/you/Applications/balenaEtcher.AppImage", "1.19.25", 102, "", False),
    ("Hades", "Steam", "1145360", "", 0, "", False),
    ("Celeste", "Steam", "504230", "", 0, "", False),
    ("Stardew Valley", "Steam", "413150", "", 0, "", False),
    ("Android Studio", "Local", "/home/you/.local/opt/android-studio", "", 1126, "", False),
    ("Postman", "Local", "/home/you/.local/opt/Postman", "", 421, "", False),
    ("Dolphin", "System", "dolphin", "25.04.1", 13, "org.kde.dolphin", False),
    ("Firewall", "System", "firewalld", "2.3.1", 4, "firewall-config", False),
    ("Disks", "System", "gnome-disk-utility", "46.1", 7, "org.gnome.DiskUtility", False),
    ("Steam", "System", "steam", "1.0.0.82", 4, "steam", False),
    ("System Settings", "System", "", "6.3.5", 9, "systemsettings", False),
]


def demo_apps():
    for name, source, ident, ver, mb, icon, _ in DEMO_APPS:
        cmd = {"Flatpak": ["flatpak", "uninstall", "-y", "--noninteractive", ident], "Homebrew": ["brew", "uninstall", ident],
               "rpm-ostree": ["rpm-ostree", "uninstall", ident], "Distrobox": ["distrobox", "rm", "-f", ident],
               "Toolbox": ["toolbox", "rm", "-f", ident],
               "Steam": ["steam", f"steam://uninstall/{ident}"], "System": ["rpm-ostree", "override", "remove", ident] if ident and not is_protected(ident) else []
               }.get(source, ["gio", "trash", ident])
        yield App(name, source, ver, human(mb * 2**20) if mb else "", cmd, icon, mb * 2**20, ident if source in ("AppImage", "Local") else "")


SOURCES = ["Flatpak", "Snap", "Homebrew", "rpm-ostree", "Distrobox", "Toolbox", "AppImage", "Steam", "Local", "System"]
BACKENDS = [demo_apps] if DEMO else [flatpak_apps, snap_apps, brew_apps, ostree_apps, distrobox_apps, toolbox_apps, appimage_apps, desktop_apps]


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=860, default_height=700)
        self.gen = self.pending = self.count = 0
        self.updates = set()
        self.busy = ""
        self.theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        self.title = Adw.WindowTitle(title="App Manager", subtitle="")
        header = Adw.HeaderBar(title_widget=self.title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda _: self.refresh())
        header.pack_start(refresh)
        select_all = Gtk.Button(icon_name="edit-select-all-symbolic", tooltip_text="Select / deselect all shown")
        select_all.connect("clicked", self.on_select_all)
        header.pack_start(select_all)
        self.remove_btn = Gtk.Button(label="Remove", sensitive=False)
        self.remove_btn.add_css_class("destructive-action")
        self.remove_btn.connect("clicked", self.on_remove)
        header.pack_end(self.remove_btn)
        pop = Gtk.Popover(child=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=4, margin_bottom=4))
        have = lambda tool: DEMO or shutil.which(tool)
        updaters = [(n, c) for n, t, c in (("Flatpak", "flatpak", ["flatpak", "update", "-y", "--noninteractive"]),
                                           ("Homebrew", "brew", ["brew", "upgrade"]),
                                           ("Snap", "snap", root_cmd("snap", "refresh") or ["pkexec", "snap", "refresh"])) if have(t)]
        actions = []
        if updaters:
            actions.append((f"Update all ({' + '.join(n for n, _ in updaters)})", [(f"{n} update", c) for n, c in updaters]))
        if have("flatpak"):
            actions.append(("Remove unused Flatpak runtimes", [("Flatpak unused", ["flatpak", "uninstall", "--unused", "-y", "--noninteractive"])]))
        if have("brew"):
            actions.append(("Clean Homebrew cache", [("Homebrew cleanup", ["brew", "cleanup"])]))
        for label, jobs in actions:
            btn = Gtk.Button(label=label, css_classes=["flat"])
            btn.connect("clicked", lambda _, j=jobs, l=label: (pop.popdown(), self.confirm_task(l, j)))
            pop.get_child().append(btn)
        if have("systemctl"):
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
            row.append(Gtk.Label(label="Update apps automatically (daily)", hexpand=True, xalign=0,
                                 tooltip_text="Updates Flatpak, Homebrew and AppImage apps in the background with a systemd timer."))
            self.auto_guard = False
            self.auto = Gtk.Switch(valign=Gtk.Align.CENTER, active=False if DEMO else auto_update_enabled())
            self.auto.connect("state-set", self.on_auto_update)
            row.append(self.auto)
            pop.get_child().append(Gtk.Separator(margin_top=4, margin_bottom=4))
            pop.get_child().append(row)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", popover=pop, tooltip_text="Maintenance and updates"))

        self.search = Gtk.SearchEntry(hexpand=True, placeholder_text="Search installed apps")
        self.search.connect("search-changed", self.on_filter)
        self.source = Gtk.DropDown.new_from_strings(["All", *SOURCES])
        self.source.connect("notify::selected", self.on_filter)
        self.sort = Gtk.DropDown.new_from_strings(["Name", "Size", "Source"])
        self.sort.set_tooltip_text("Sort by")
        self.sort.connect("notify::selected", lambda *_: self.list.invalidate_sort())
        bar = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        bar.append(self.search)
        bar.append(self.source)
        bar.append(self.sort)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                margin_bottom=12, margin_start=12, margin_end=12)
        self.list.add_css_class("boxed-list")
        self.list.set_filter_func(self.visible)
        self.list.set_sort_func(self.order)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Gtk.ScrolledWindow(child=self.list), "list")
        self.stack.add_named(Adw.StatusPage(title="No matching apps", icon_name="edit-find-symbolic",
                                            description="Try another search or source."), "empty")
        self.search.set_key_capture_widget(self)  # type anywhere to search
        keys = Gtk.ShortcutController()
        for trigger, fn in (("<Control>f", self.search.grab_focus), ("F5|<Control>r", self.refresh),
                            ("<Control>a", lambda: self.on_select_all(None)),
                            ("Delete", lambda: self.selected() and self.on_remove(None))):
            keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                                           action=Gtk.CallbackAction.new(lambda *_, fn=fn: fn() or True)))
        self.add_controller(keys)
        self.banner = Adw.Banner(button_label="Update", use_markup=False)  # the release title comes from the network
        self.banner.connect("button-clicked", self.on_self_update)
        self.release = None
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.banner)
        body.append(bar)
        body.append(self.stack)

        view = Adw.ToolbarView(content=body)
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)
        self.refresh()
        threading.Thread(target=self.check_release, daemon=True).start()

    def check_release(self):
        release = latest_release()
        if release:
            GLib.idle_add(self.show_release, release)

    def show_release(self, release):
        self.release = release
        self.banner.set_title(f"App Manager {release['tag_name'].lstrip('v')} is available")
        self.banner.set_button_label("Update" if self_update_cmd() else "What's new")
        self.banner.set_revealed(True)

    def on_self_update(self, _):
        cmd = self_update_cmd()
        if not cmd:
            url = str(self.release.get("html_url", ""))
            if not url.startswith(f"https://github.com/{REPO}/"):
                url = f"https://github.com/{REPO}/releases/latest"
            Gio.AppInfo.launch_default_for_uri(url, None)
            return
        self.banner.set_revealed(False)
        self.run_task([("App Manager", cmd)], "Updated", "; restart App Manager to use the new version")

    def on_auto_update(self, switch, state):
        if self.auto_guard:  # programmatic revert after a failure: let the default handler apply it
            return False
        threading.Thread(target=lambda: GLib.idle_add(self.auto_done, switch, state, set_auto_update(state)), daemon=True).start()
        return True  # keep the old visual state until systemctl has answered

    def auto_done(self, switch, state, ok):
        if ok:
            switch.set_state(state)
            msg = "Apps will update daily in the background" if state else "Automatic updates are off"
        else:
            self.auto_guard = True
            switch.set_active(not state)
            self.auto_guard = False
            msg = "Could not change the automatic update timer (is systemd --user running?)"
        self.toasts.add_toast(Adw.Toast(title=msg, use_markup=False))

    def make_icon(self, a):  # app icon if the theme or a file has one, else a coloured initials avatar
        if a.icon.startswith("/") and os.path.isfile(a.icon):
            img = Gtk.Image(pixel_size=32)
            img.set_from_file(a.icon)
            return img
        if a.icon and self.theme.has_icon(a.icon):
            return Gtk.Image(icon_name=a.icon, pixel_size=32)
        return Adw.Avatar(size=32, text=a.name, show_initials=True)

    def row_button(self, icon, tip, cb):
        btn = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", cb)
        return btn

    def show_details(self, a):
        cmd = shlex.join(a.cmd)
        dialog = Adw.AlertDialog(heading=a.name, body=(
            f"Source: {a.source}\nVersion: {a.version or '-'}\nSize: {a.size or '-'}\n"
            f"Location: {a.path or '-'}\nRemove command: {cmd or 'none, not removable here: unowned or protected core package'}"))
        dialog.add_response("close", "Close")
        if cmd:
            dialog.add_response("copy", "Copy remove command")
        dialog.set_close_response("close")
        dialog.connect("response", lambda _, r: r == "copy" and self.get_clipboard().set(cmd))
        dialog.present(self)

    def rows(self):
        row = self.list.get_first_child()
        while row:
            yield row
            row = row.get_next_sibling()

    def visible(self, row):
        i = self.source.get_selected()
        return ((not 0 < i <= len(SOURCES) or row.app.source == SOURCES[i - 1])
                and self.search.get_text().lower() in row.app.name.lower())

    def order(self, a, b):
        k = self.sort.get_selected()
        key = lambda r: ((r.app.source, r.app.name.lower()) if k == 2
                         else (-r.app.bytes, r.app.name.lower()) if k == 1 else (r.app.name.lower(),))
        return (key(a) > key(b)) - (key(a) < key(b))

    def on_select_all(self, _):
        shown = [r for r in self.rows() if self.visible(r) and r.app.cmd and r.app.source != "System"]  # base image: tick by hand only
        state = not all(r.check.get_active() for r in shown)
        for r in shown:
            r.check.set_active(state)

    def mark_updates(self, updates, gen):
        if gen != self.gen:
            return
        self.updates = updates
        for r in self.rows():
            r.chip.set_visible(r.app.key in updates)
        self.update_state()

    def find_updates(self, gen):
        try:
            ups = pending_updates()
        except Exception:
            traceback.print_exc()
            ups = set()
        GLib.idle_add(self.mark_updates, ups, gen)

    def update_counts(self):
        counts = {s: 0 for s in SOURCES}
        for r in self.rows():
            counts[r.app.source] += 1
        sel = self.source.get_selected()
        self.source.get_model().splice(0, len(SOURCES) + 1, [f"All ({sum(counts.values())})", *(f"{s} ({counts[s]})" for s in SOURCES)])
        self.source.set_selected(sel if sel <= len(SOURCES) else 0)

    def selected(self):  # hidden rows never count, so a filter can't remove unseen apps
        return [r for r in self.rows() if r.check.get_active() and self.visible(r)]

    def on_filter(self, *_):
        self.list.invalidate_filter()
        self.update_state()

    def update_state(self):
        n = len(self.selected())
        self.remove_btn.set_sensitive(n > 0)
        self.remove_btn.set_label(f"Remove ({n})" if n else "Remove")
        self.stack.set_visible_child_name("list" if self.pending or any(self.visible(r) for r in self.rows()) else "empty")
        ups = sum(1 for r in self.rows() if r.chip.get_visible())
        self.title.set_subtitle(self.busy or f"{self.count} apps" + (f" · {ups} updates" if ups else "") + (" · loading…" if self.pending else ""))

    def refresh(self):
        self.gen += 1
        self.count = 0
        self.busy = ""
        self.updates = set()
        self.pending = len(BACKENDS)
        self.list.remove_all()
        self.update_state()
        for backend in BACKENDS:
            threading.Thread(target=self.load, args=(backend, self.gen), daemon=True).start()
        threading.Thread(target=self.find_updates, args=(self.gen,), daemon=True).start()

    def load(self, backend, gen):
        try:
            apps = list(backend())
        except Exception:
            traceback.print_exc()
            apps = []
        GLib.idle_add(self.add, apps, gen)

    def add(self, apps, gen):
        if gen != self.gen:
            return
        for a in apps:
            row = Adw.ActionRow(title=a.name, subtitle=" · ".join(filter(None, [a.source, a.version, a.size])), use_markup=False)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda *_: self.update_state())
            check.set_sensitive(bool(a.cmd))  # entries no package owns can't be removed
            row.add_prefix(self.make_icon(a))  # later prefixes sit further left: checkbox ends up leftmost
            row.add_prefix(check)
            row.chip = Gtk.Button(label="Update", css_classes=["flat", "accent"], valign=Gtk.Align.CENTER, visible=False,
                                  tooltip_text=f"Update {a.name}")
            row.chip.connect("clicked", lambda _, a=a: self.run_task([(a.name, update_cmd(a))], "Updated"))
            row.add_suffix(row.chip)
            if a.source == "System":
                row.add_suffix(Gtk.Label(label="System", css_classes=["caption", "warning"], valign=Gtk.Align.CENTER,
                                         tooltip_text=SYSTEM_NOTE))
            row.add_suffix(self.row_button("dialog-information-symbolic", "Details", lambda _, a=a: self.show_details(a)))
            row.set_activatable_widget(check)
            row.app, row.check = a, check
            self.list.append(row)
        self.count += len(apps)
        self.pending -= 1
        self.update_counts()
        self.mark_updates(self.updates, gen)
        self.update_state()

    def on_remove(self, _):
        apps = [r.app for r in self.selected()]
        left = list(dict.fromkeys(p for a in apps for p in leftovers(a)))
        data = Gtk.CheckButton(label="Also delete Flatpak app data")
        clean = Gtk.CheckButton(label="Also move leftover config and data to the trash")
        body = "\n".join(dict.fromkeys(shlex.join(a.cmd) for a in apps))
        if left:
            body += "\n\nLeftover folders found:\n" + "\n".join(p.replace(str(HOME), "~") for p in left)
        if any(a.source == "System" for a in apps):
            body += "\n\nSYSTEM: " + SYSTEM_NOTE
        dialog = Adw.AlertDialog(heading=f"Remove {len(apps)} app(s)?", body=body)
        extras = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if any(a.source == "Flatpak" for a in apps):
            extras.append(data)
        if left:
            extras.append(clean)
        if extras.get_first_child():
            dialog.set_extra_child(extras)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, r: r == "remove" and self.remove(apps, data.get_active(), left if clean.get_active() else []))
        dialog.present(self)

    def remove(self, apps, wipe, left=()):
        jobs = {tuple(c): (a.name, c) for a in apps for c in [a.cmd + ["--delete-data"] if wipe and a.source == "Flatpak" else a.cmd]}  # several menu entries can share one package
        jobs = list(jobs.values()) + ([("Leftover data", ["gio", "trash", *left])] if left else [])
        reboot = any(a.source == "rpm-ostree" or (a.source == "System" and IMMUTABLE) for a in apps)
        self.run_task(jobs, "Removed", "; reboot to finish rpm-ostree changes" if reboot else "")

    def confirm_task(self, label, jobs):
        dialog = Adw.AlertDialog(heading=label, body="\n".join(shlex.join(c) for _, c in jobs))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("run", "Run")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, r: r == "run" and self.run_task(jobs, "Finished"))
        dialog.present(self)

    def run_task(self, jobs, verb, note=""):  # jobs: [(label, cmd)]
        self.busy = "Working…"
        self.set_sensitive(False)
        self.update_state()
        threading.Thread(target=self.do_task, args=(jobs, verb, note), daemon=True).start()

    def do_task(self, jobs, verb, note):
        errors = []
        try:
            for label, cmd in jobs:
                r = subprocess.run(["true"] if DEMO else cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
                if r.returncode:
                    errors.append(f"{label}: {(r.stderr.strip().splitlines() or [str(r.returncode)])[-1]}")
        except Exception as e:  # window must always become usable again
            errors.append(str(e))
        finally:
            GLib.idle_add(self.task_done, len(jobs), errors, verb, note)

    def task_done(self, total, errors, verb, note):
        self.set_sensitive(True)
        msg = f"{verb} {total - len(errors)}/{total}{note}" + (" — " + "; ".join(errors) if errors else "")
        self.toasts.add_toast(Adw.Toast(title=msg, use_markup=False, timeout=0 if errors else 5))
        self.refresh()


class Manager(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE if SHOT else Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        Gtk.Window.set_default_icon_name(APP_ID)
        win = self.props.active_window or Window(self)
        win.present()
        if SHOT:  # verification aid: save a window screenshot once apps have loaded, then quit
            GLib.timeout_add_seconds(SHOT[1], self.shoot, win)

    def shoot(self, win):
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snap, win.get_width(), win.get_height())
        win.get_native().get_renderer().render_texture(snap.to_node(), None).save_to_png(SHOT[0])
        self.quit()


SHOT = None
if "--shot" in sys.argv:  # --shot out.png [seconds]
    i = sys.argv.index("--shot")
    SHOT = (sys.argv[i + 1], int(sys.argv[i + 2]) if len(sys.argv) > i + 2 else 8)

if __name__ == "__main__":
    if "--update-all" in sys.argv:
        sys.exit(update_all())
    if "--list" in sys.argv:
        for backend in BACKENDS:
            for a in backend():
                print(f"{a.source}\t{a.name}\t{a.version}\t{a.size}\t{shlex.join(a.cmd)}")
    else:
        sys.exit(Manager().run(sys.argv[:1]))
