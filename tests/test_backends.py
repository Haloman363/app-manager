"""Headless tests for the parsing and safety logic. Run: python3 -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import appmanager as m  # noqa: E402


def fake_run(outputs):
    """Replacement for m.run: look the command up by its first words, return "" when unknown."""
    return lambda *cmd: next((v for k, v in outputs.items() if " ".join(cmd).startswith(k)), "")


class PackageOwner(unittest.TestCase):
    def test_rpm(self):
        with mock.patch.object(m, "run", fake_run({"rpm -qf": "waydroid 1.6.3 710000\n"})):
            self.assertEqual(m.package_owner("rpm-ostree", "/x.desktop"), ("waydroid", "1.6.3", 710000))

    def test_rpm_two_owners_refuses_to_guess(self):
        with mock.patch.object(m, "run", fake_run({"rpm -qf": "a 1 2\nb 3 4\n"})):
            self.assertEqual(m.package_owner("dnf", "/x.desktop")[0], "")

    def test_dpkg(self):
        out = {"dpkg-query -S": "gimp: /usr/share/applications/gimp.desktop\n", "dpkg-query -W": "2.10.36 50000"}
        with mock.patch.object(m, "run", fake_run(out)):
            self.assertEqual(m.package_owner("apt-get", "/x.desktop"), ("gimp", "2.10.36", 50000 * 1024))

    def test_dpkg_strips_arch_and_rejects_shared_files(self):
        with mock.patch.object(m, "run", fake_run({"dpkg-query -S": "libfoo:amd64: /x\n", "dpkg-query -W": "1 1"})):
            self.assertEqual(m.package_owner("apt-get", "/x")[0], "libfoo")
        with mock.patch.object(m, "run", fake_run({"dpkg-query -S": "a, b: /x\n"})):
            self.assertEqual(m.package_owner("apt-get", "/x")[0], "")

    def test_pacman(self):
        with mock.patch.object(m, "run", fake_run({"pacman -Qo": "vlc\n", "pacman -Q vlc": "vlc 3.0.21-1\n"})):
            self.assertEqual(m.package_owner("pacman", "/x"), ("vlc", "3.0.21-1", 0))

    def test_unowned(self):
        with mock.patch.object(m, "run", fake_run({})):
            for pm in ("rpm-ostree", "apt-get", "pacman"):
                self.assertEqual(m.package_owner(pm, "/x")[0], "")


def safe_which(name, path=None):
    """Only a root-owned PATH finds /usr/bin binaries; the user PATH would find an impostor in ~/.local/bin."""
    return f"/usr/bin/{name}" if path == m.SAFE_PATH else f"/home/x/.local/bin/{name}"


class SystemCommands(unittest.TestCase):
    def test_commands_per_package_manager(self):
        with mock.patch.object(m.shutil, "which", safe_which):
            self.assertEqual(m.system_cmd("rpm-ostree", "waydroid"), ["/usr/bin/rpm-ostree", "override", "remove", "waydroid"])
            self.assertEqual(m.system_cmd("apt-get", "gimp"),
                             ["/usr/bin/pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", "remove", "-y", "gimp"])
            self.assertEqual(m.system_cmd("dnf", "gimp"), ["/usr/bin/pkexec", "/usr/bin/dnf", "remove", "-y",
                                                           "--setopt=clean_requirements_on_remove=False", "gimp"])
            self.assertEqual(m.system_cmd("pacman", "vlc"), ["/usr/bin/pkexec", "/usr/bin/pacman", "-R", "--noconfirm", "vlc"])
            self.assertEqual(m.root_cmd("snap", "remove", "x"), ["/usr/bin/pkexec", "/usr/bin/snap", "remove", "x"])

    def test_protected_and_unsafe_names_are_never_offered(self):
        with mock.patch.object(m.shutil, "which", safe_which):
            for pkg in ("plasma-desktop", "gnome-shell", "ubuntu-desktop-minimal", "systemd", "sddm-theme", "NetworkManager-wifi",
                        "kwin-wayland", "mutter", "pipewire-pulse", "gdm3", "", "-rf"):
                self.assertEqual(m.system_cmd("apt-get", pkg), [], pkg)
            self.assertEqual(m.system_cmd("", "gimp"), [])

    def test_protection_matches_whole_names_only(self):
        for pkg in ("aptitude", "gimp", "dolphin", "konsole", "bazzite-updater", "libreoffice"):
            self.assertFalse(m.is_protected(pkg), pkg)
        for pkg in ("apt", "apt-utils", "dnf", "bazzite", "SystemD-udev"):
            self.assertTrue(m.is_protected(pkg), pkg)

    def test_mutable_distros_need_pkexec_but_ostree_does_not(self):
        no_pkexec = lambda name, path=None: None if name == "pkexec" else f"/usr/bin/{name}"
        with mock.patch.object(m.shutil, "which", no_pkexec):
            self.assertEqual(m.system_cmd("apt-get", "gimp"), [])
            self.assertEqual(m.root_cmd("snap", "remove", "x"), [])
            self.assertTrue(m.system_cmd("rpm-ostree", "waydroid"))

    def test_package_manager_is_found_on_the_safe_path_only(self):
        with mock.patch.object(m.shutil, "which", lambda name, path=None: "/x/" + name if path == m.SAFE_PATH and name == "dnf" else None):
            self.assertEqual(m.system_pm(), "dnf")


class Containers(unittest.TestCase):
    def test_toolbox(self):
        out = {"podman ps": "dev\tregistry.fedoraproject.org/fedora-toolbox:42\n"}
        with mock.patch.object(m, "run", fake_run(out)), mock.patch.object(m.shutil, "which", return_value="/usr/bin/toolbox"):
            (a,) = m.toolbox_apps()
        self.assertEqual((a.name, a.source, a.version, a.cmd), ("dev", "Toolbox", "fedora-toolbox:42", ["toolbox", "rm", "-f", "dev"]))

    def test_snap_hides_runtimes(self):
        table = ("Name       Version  Rev   Tracking       Publisher  Notes\n"
                 "core22     20240111 1380  latest/stable  canonical✓ base\n"
                 "snapd      2.63     2000  latest/stable  canonical✓ snapd\n"
                 "snapd-desktop-integration 0.9 100 latest/stable canonical✓ -\n"
                 "gnome-42-2204 0+git 176   latest/stable  canonical✓ -\n"
                 "mesa-core22 24.0 1000 latest/stable canonical✓ -\n"
                 "gnome-calculator 46.1 900 latest/stable canonical✓ -\n"
                 "firefox    128.0    4336  latest/stable  mozilla✓   -\n")
        with mock.patch.object(m, "run", fake_run({"snap list": table})), mock.patch.object(m.shutil, "which", safe_which):
            self.assertEqual([a.name for a in m.snap_apps()], ["gnome-calculator", "firefox"])


class Leftovers(unittest.TestCase):
    def test_only_exact_name_matches_and_never_system_dirs(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / ".config"
            for d in ("tabby", "Tabby Terminal", "applications", "other"):
                (cfg / d).mkdir(parents=True)
            with mock.patch.object(m, "DATA_DIRS", [cfg]), mock.patch.object(m, "DEMO", False):
                found = {Path(p).name for p in m.leftovers(m.App("Tabby", "AppImage", "", "", ["x"], path="/a/tabby.appimage"))}
                self.assertEqual(found, {"tabby"})
                self.assertEqual({Path(p).name for p in m.leftovers(m.App("Tabby Terminal", "Local", "", "", ["x"]))}, {"Tabby Terminal"})
                self.assertEqual(m.leftovers(m.App("applications", "Local", "", "", ["x"])), [])
                self.assertEqual(m.leftovers(m.App("tabby", "Flatpak", "", "", ["x"])), [])
                self.assertEqual(m.leftovers(m.App("tabby", "Homebrew", "", "", ["x"])), [])  # hand-written config is never offered for formulae
                (cfg / "pulse").mkdir()
                self.assertEqual(m.leftovers(m.App("Pulse", "AppImage", "", "", ["x"])), [])  # PulseAudio's real config


class Updates(unittest.TestCase):
    def test_update_all_updates_only_pending_apps_and_never_snap(self):
        a = m.App("A", "Flatpak", "", "", ["flatpak", "uninstall", "-y", "--noninteractive", "org.a"])
        b = m.App("B", "Flatpak", "", "", ["flatpak", "uninstall", "-y", "--noninteractive", "org.b"])
        calls = []
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(m, "flatpak_apps", lambda: iter([a, b])), mock.patch.object(m, "brew_apps", lambda: iter([])), \
                mock.patch.object(m, "appimage_apps", lambda: iter([])), \
                mock.patch.object(m, "pending_updates", lambda: {("Flatpak", "org.a"), ("Snap", "firefox")}), \
                mock.patch.object(m.shutil, "which", return_value=None), \
                mock.patch.object(m.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd) or ok):
            self.assertEqual(m.update_all(), 0)
        self.assertEqual(calls, [["flatpak", "update", "-y", "--noninteractive", "org.a"]])

    def test_update_all_exit_code_reflects_failures(self):
        a = m.App("A", "Flatpak", "", "", ["flatpak", "uninstall", "-y", "--noninteractive", "org.a"])
        bad = mock.Mock(returncode=1, stderr="boom")
        with mock.patch.object(m, "flatpak_apps", lambda: iter([a])), mock.patch.object(m, "brew_apps", lambda: iter([])), \
                mock.patch.object(m, "appimage_apps", lambda: iter([])), mock.patch.object(m, "pending_updates", lambda: {("Flatpak", "org.a")}), \
                mock.patch.object(m.shutil, "which", return_value=None), mock.patch.object(m.subprocess, "run", return_value=bad):
            self.assertEqual(m.update_all(), 1)

    def test_version_compare(self):
        self.assertTrue(m.newer("v0.2.0", "0.1.0"))
        self.assertTrue(m.newer("1.0.0", "0.9.9"))
        self.assertFalse(m.newer("v0.1.0", "0.1.0"))
        self.assertFalse(m.newer("", "0.1.0"))
        self.assertFalse(m.newer("nightly", "0.1.0"))

    def test_timer_units(self):
        service, timer = m.auto_update_units()
        self.assertIn("--update-all", service)
        self.assertIn(f'"{m.SELF}"', service)
        self.assertIn("OnCalendar=daily", timer)
        with mock.patch.object(m, "SELF", Path("/odd %h/$HOME/app.py")):
            self.assertIn('"/odd %%h/$$HOME/app.py"', m.auto_update_units()[0])
        self.assertIn("Persistent=true", timer)

    def test_enabling_auto_update_writes_units_and_starts_timer(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(m, "UNIT_DIR", Path(t)), mock.patch.object(m, "DEMO", False), \
                mock.patch.object(m.subprocess, "run") as run:
            self.assertTrue(m.set_auto_update(True))
            self.assertTrue((Path(t) / "app-manager-update.service").exists() and (Path(t) / m.TIMER).exists())
            run.assert_called_with(["systemctl", "--user", "enable", "--now", m.TIMER], check=True, capture_output=True, timeout=30)

    def test_failed_systemctl_reports_failure(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(m, "UNIT_DIR", Path(t)), mock.patch.object(m, "DEMO", False), \
                mock.patch.object(m.subprocess, "run", side_effect=m.subprocess.CalledProcessError(1, "systemctl")):
            self.assertFalse(m.set_auto_update(True))

    def test_self_update_only_for_git_checkouts(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(m, "SELF", Path(t) / "appmanager.py"):
            self.assertEqual(m.self_update_cmd(), [])
            (Path(t) / ".git").mkdir()
            with mock.patch.object(m.shutil, "which", return_value="/usr/bin/git"):
                with mock.patch.object(m, "run", fake_run({"git": "https://github.com/someone/dotfiles.git\n"})):
                    self.assertEqual(m.self_update_cmd(), [])  # some other repo
                with mock.patch.object(m, "run", fake_run({"git": "https://github.com/Haloman363/app-manager.git\n"})):
                    self.assertEqual(m.self_update_cmd(), ["env", "GIT_TERMINAL_PROMPT=0", "git", "-C", t, "pull", "--ff-only"])


class Smoke(unittest.TestCase):
    def test_every_backend_runs_on_a_machine_without_any_tools(self):
        with mock.patch.object(m, "run", lambda *cmd: ""), mock.patch.object(m.shutil, "which", return_value=None):
            for backend in (m.flatpak_apps, m.snap_apps, m.brew_apps, m.ostree_apps, m.distrobox_apps, m.toolbox_apps, m.desktop_apps, m.appimage_apps):
                list(backend())
            self.assertEqual(m.pending_updates(), set())

    def test_demo_backend_has_valid_commands(self):
        with mock.patch.object(m, "DEMO", True):
            apps = list(m.demo_apps())
        self.assertTrue(apps)
        self.assertTrue(all(a.source in m.SOURCES for a in apps))


class Misc(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(m.parse_size("1.0 GB"), 10**9)
        self.assertEqual(m.parse_size("28.9 MB"), 28_900_000)
        self.assertEqual(m.parse_size("512 bytes"), 512)
        self.assertEqual(m.parse_size(""), 0)

    def test_update_key_matches_pending_updates_identity(self):
        a = m.App("Tabby", "AppImage", "", "", ["gio", "trash", "/a/t.AppImage", "/a/t.desktop"], path="/a/t.AppImage")
        self.assertEqual(a.key, ("AppImage", "/a/t.AppImage"))
        self.assertEqual(m.App("bat", "Homebrew", "", "", ["brew", "uninstall", "bat"]).key, ("Homebrew", "bat"))

    def test_demo_never_executes(self):
        self.assertTrue(m.DEMO is False)  # real runs must not be in demo mode

    def test_every_source_has_a_backend_label(self):
        self.assertEqual(len(m.SOURCES), len(set(m.SOURCES)))


if __name__ == "__main__":
    unittest.main()
