#!/usr/bin/env python3
"""Control-panel-style app manager: Flatpak, Homebrew, rpm-ostree layers, Distrobox, AppImage.

GUI: ./appmanager.py        Headless check: ./appmanager.py --list
"""
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

os.environ["PATH"] += ":/home/linuxbrew/.linuxbrew/bin"  # desktop launches lack brew on PATH

HOME = Path.home()
APPIMAGE_DIRS = [HOME / d for d in ("Applications", "AppImages", ".local/bin", "bin", "Downloads", "Desktop")] + [Path("/opt")]
APP_ID = "io.github.haloman363.AppManager"
OPT = (HOME / ".local/opt").resolve()  # resolved: /home is a symlink to /var/home
PROTECTED = {"plasma-desktop", "plasma-workspace", "plasma-systemsettings", "kde-settings", "kde-settings-plasma", "kwin", "sddm", "rpm-ostree", "bazzite"}  # session/OS core: never offered for removal
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
        return (self.source, self.cmd[-1] if self.cmd else self.name)


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


def pending_updates():  # {(source, id)} with a newer version available
    ups = {("Flatpak", i) for i in run("flatpak", "remote-ls", "--updates", "--app", "--columns=application").split()}
    return ups | {("Homebrew", n) for n in run("brew", "outdated", "--formula", "--quiet").split()}


def update_cmd(a):
    return ["flatpak", "update", "-y", "--noninteractive", a.cmd[-1]] if a.source == "Flatpak" else ["brew", "upgrade", a.cmd[-1]]


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
    dep = deployment()  # layered, local and already-removed packages must not show up as removable base packages
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
                    or (only and "KDE" not in only.split(";")) or "KDE" in notin.split(";") or ".appimage" in exe.lower()):
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
            else:  # base image package: removable only as an rpm-ostree override (reboot, reversible)
                out = run("rpm", "-qf", "--qf", "%{NAME} %{VERSION} %{SIZE}\n", str(f)).strip()
                pkg, _, rest = ("", "", "") if "\n" in out else out.partition(" ")  # several owners: refuse to guess
                if pkg in skip:
                    continue
                if pkg.startswith("-") or pkg in PROTECTED:
                    pkg = ""
                ver, _, size = rest.partition(" ")
                size = int(size) if size.isdigit() else 0
                yield App(name, "System", ver, human(size) if size else "", ["rpm-ostree", "override", "remove", pkg] if pkg else [], icon, size, str(f))


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


SOURCE_ICONS = {  # first icon present in the theme wins
    "Flatpak": ["flatpak-discover", "package-x-generic"],
    "Homebrew": ["utilities-terminal", "application-x-executable"],
    "rpm-ostree": ["system-software-install", "package-x-generic"],
    "Distrobox": ["container", "utilities-terminal"],
    "AppImage": ["application-x-executable", "package-x-generic"],
    "Steam": ["steam", "applications-games"],
    "Local": ["folder-download", "application-x-executable"],
    "System": ["computer", "preferences-system"],
}
SOURCES = ["Flatpak", "Homebrew", "rpm-ostree", "Distrobox", "AppImage", "Steam", "Local", "System"]
BACKENDS = [flatpak_apps, brew_apps, ostree_apps, distrobox_apps, appimage_apps, desktop_apps]


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=760, default_height=720)
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
        for label, jobs in (
            ("Update all (Flatpak + Homebrew)", [("Flatpak update", ["flatpak", "update", "-y", "--noninteractive"]), ("Homebrew upgrade", ["brew", "upgrade"])]),
            ("Remove unused Flatpak runtimes", [("Flatpak unused", ["flatpak", "uninstall", "--unused", "-y", "--noninteractive"])]),
            ("Clean Homebrew cache", [("Homebrew cleanup", ["brew", "cleanup"])]),
        ):
            btn = Gtk.Button(label=label, css_classes=["flat"])
            btn.connect("clicked", lambda _, j=jobs, l=label: (pop.popdown(), self.confirm_task(l, j)))
            pop.get_child().append(btn)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", popover=pop, tooltip_text="Maintenance"))

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
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(bar)
        body.append(self.stack)

        view = Adw.ToolbarView(content=body)
        view.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_content(self.toasts)
        self.refresh()

    def make_icon(self, a):
        img = Gtk.Image(pixel_size=32)
        if a.icon.startswith("/") and os.path.isfile(a.icon):
            img.set_from_file(a.icon)
        elif a.icon and self.theme.has_icon(a.icon):
            img.set_from_icon_name(a.icon)
        else:
            img.set_from_icon_name(next((n for n in SOURCE_ICONS[a.source] if self.theme.has_icon(n)), "application-x-executable"))
        return img

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
                                         tooltip_text="Part of the base OS image. Removal is an rpm-ostree override: needs a reboot, undo with 'rpm-ostree override reset <package>'."))
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
        data = Gtk.CheckButton(label="Also delete Flatpak app data")
        body = "\n".join(dict.fromkeys(shlex.join(a.cmd) for a in apps))
        if any(a.source == "System" for a in apps):
            body += ("\n\nSYSTEM: this removes packages from the base OS image (rpm-ostree override). It applies after a reboot "
                     "and can break the desktop if you remove something it needs. Undo: rpm-ostree override reset <package>.")
        dialog = Adw.AlertDialog(heading=f"Remove {len(apps)} app(s)?", body=body)
        if any(a.source == "Flatpak" for a in apps):
            dialog.set_extra_child(data)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, r: r == "remove" and self.remove(apps, data.get_active()))
        dialog.present(self)

    def remove(self, apps, wipe):
        jobs = {tuple(c): (a.name, c) for a in apps for c in [a.cmd + ["--delete-data"] if wipe and a.source == "Flatpak" else a.cmd]}  # several menu entries can share one package
        self.run_task(list(jobs.values()), "Removed", "; reboot to finish rpm-ostree changes" if any(a.source in ("rpm-ostree", "System") for a in apps) else "")

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
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
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
    if "--list" in sys.argv:
        for backend in BACKENDS:
            for a in backend():
                print(f"{a.source}\t{a.name}\t{a.version}\t{a.size}\t{shlex.join(a.cmd)}")
    else:
        sys.exit(Manager().run(sys.argv[:1]))
