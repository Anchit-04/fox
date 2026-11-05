import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import paths
from browser import launch


def _fake_install(root: Path, name: str) -> Path:
    exe = root / launch.CANDIDATES[name][0]
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    return exe


class FindBrowser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.program_files = self.root / "pf"
        self.local = self.root / "local"
        self.program_files.mkdir()
        self.local.mkdir()
        self.env = {"ProgramFiles": str(self.program_files), "LOCALAPPDATA": str(self.local)}

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, extra=None, preferred=None):
        with mock.patch.dict(os.environ, {**self.env, **(extra or {})}, clear=True):
            return launch.find_browser(preferred)

    def test_chrome_wins_by_default(self):
        _fake_install(self.program_files, "brave")
        chrome = _fake_install(self.program_files, "chrome")
        self.assertEqual(self.find(), chrome)

    def test_falls_back_to_brave(self):
        brave = _fake_install(self.program_files, "brave")
        self.assertEqual(self.find(), brave)

    def test_finds_a_per_user_install(self):
        chrome = _fake_install(self.local, "chrome")
        self.assertEqual(self.find(), chrome)

    def test_fox_browser_name_picks_brave(self):
        _fake_install(self.program_files, "chrome")
        brave = _fake_install(self.program_files, "brave")
        self.assertEqual(self.find({"FOX_BROWSER": "Brave"}), brave)

    def test_an_argument_beats_the_env_var(self):
        chrome = _fake_install(self.program_files, "chrome")
        _fake_install(self.program_files, "brave")
        self.assertEqual(self.find({"FOX_BROWSER": "brave"}, preferred="chrome"), chrome)

    def test_fox_browser_path_is_used_as_is(self):
        custom = self.root / "portable" / "chromium.exe"
        custom.parent.mkdir()
        custom.write_bytes(b"")
        self.assertEqual(self.find({"FOX_BROWSER": str(custom)}), custom)

    def test_a_preferred_browser_that_is_missing_falls_back(self):
        chrome = _fake_install(self.program_files, "chrome")
        self.assertEqual(self.find({"FOX_BROWSER": "brave"}), chrome)

    def test_an_unknown_name_raises(self):
        _fake_install(self.program_files, "chrome")
        with self.assertRaises(launch.BrowserNotFound) as caught:
            self.find({"FOX_BROWSER": "chorme"})
        self.assertIn("chorme", str(caught.exception))

    def test_nothing_installed_raises_and_names_the_setting(self):
        with self.assertRaises(launch.BrowserNotFound) as caught:
            self.find()
        self.assertIn("FOX_BROWSER", str(caught.exception))

    def test_no_install_vars_at_all(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(launch.BrowserNotFound):
                launch.find_browser()

    def test_x86_folder_is_searched(self):
        x86 = self.root / "pf86"
        chrome = _fake_install(x86, "chrome")
        self.assertEqual(self.find({"ProgramFiles(x86)": str(x86)}), chrome)


class ProfileAndPortFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch.object(paths, "FOX_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_profile_lives_under_fox_home(self):
        profile = launch.profile_dir("work")
        self.assertEqual(profile, self.home / "browser" / "work")
        self.assertTrue(profile.is_dir())

    def test_reads_port_and_path(self):
        profile = launch.profile_dir()
        (profile / launch.PORT_FILE).write_text("53111\n/devtools/browser/abc-123\n", encoding="utf-8")
        self.assertEqual(launch.read_port_file(profile), (53111, "/devtools/browser/abc-123"))

    def test_missing_or_half_written_port_file_is_none(self):
        profile = launch.profile_dir()
        self.assertIsNone(launch.read_port_file(profile))
        (profile / launch.PORT_FILE).write_text("53111\n", encoding="utf-8")
        self.assertIsNone(launch.read_port_file(profile))
        (profile / launch.PORT_FILE).write_text("abc\n/devtools/browser/x\n", encoding="utf-8")
        self.assertIsNone(launch.read_port_file(profile))

    def test_attach_ignores_a_dead_port(self):
        profile = launch.profile_dir()
        (profile / launch.PORT_FILE).write_text("1\n/devtools/browser/gone\n", encoding="utf-8")
        self.assertIsNone(launch.attach(profile))

    def test_attach_ignores_a_browser_that_is_not_ours(self):
        profile = launch.profile_dir()
        (profile / launch.PORT_FILE).write_text("9222\n/devtools/browser/mine\n", encoding="utf-8")
        other = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/someone-else"}
        with mock.patch.object(launch, "_version_info", return_value=other):
            self.assertIsNone(launch.attach(profile))

    def test_headless_flag_and_profile_go_on_the_command_line(self):
        profile = launch.profile_dir()
        args = launch._launch_args(Path("chrome.exe"), profile, headless=True)
        self.assertIn("--remote-debugging-port=0", args)
        self.assertIn(f"--user-data-dir={profile}", args)
        self.assertIn("--headless=new", args)
        windowed = launch._launch_args(Path("chrome.exe"), profile, headless=False)
        self.assertNotIn("--headless=new", windowed)
        self.assertIn("--no-startup-window", windowed)
        self.assertNotIn("about:blank", windowed)

    def test_windowed_is_the_default_unless_asked(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(launch.headless_by_default())
        with mock.patch.dict(os.environ, {"FOX_BROWSER_HEADLESS": "1"}):
            self.assertTrue(launch.headless_by_default())


class LaunchLock(unittest.TestCase):
    def test_a_second_holder_waits_then_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "default"
            profile.mkdir()
            result = {}

            def contend():
                try:
                    with launch._launch_lock(profile, timeout=0.3):
                        result["got"] = True
                except launch.BrowserLaunchFailed:
                    result["got"] = False

            with launch._launch_lock(profile, timeout=1):
                worker = threading.Thread(target=contend)
                worker.start()
                worker.join()
            self.assertFalse(result["got"])
            with launch._launch_lock(profile, timeout=0.3):
                pass


def _installed_browser():
    try:
        return launch.find_browser()
    except launch.BrowserNotFound:
        return None


@unittest.skipIf(_installed_browser() is None, "no Chrome or Brave installed")
class LiveBrowser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "default"
        self.profile.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_launch_then_a_second_caller_attaches(self):
        first = launch.attach_or_launch(profile=self.profile, headless=True)
        try:
            self.assertIsNotNone(first.process)
            self.assertTrue(first.ws_url.startswith(f"ws://127.0.0.1:{first.port}/devtools/browser/"))
            second = launch.attach_or_launch(profile=self.profile, headless=True)
            self.assertIsNone(second.process)
            self.assertEqual(second.ws_url, first.ws_url)
        finally:
            launch.close(first)
        self.assertIsNotNone(first.process.poll())
        self.assertIsNone(launch.attach(self.profile))


if __name__ == "__main__":
    unittest.main()
