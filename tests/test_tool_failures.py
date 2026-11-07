import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import paths
import resume
import transport
from agent import execute_tool
from browser.cdp import CDPError
from sandbox import sandbox_client
from tools import browser_tools
from tools.file_tools import FILE_TOOL_HANDLERS


class AToolErrorNeverEndsTheChat(unittest.TestCase):
    def test_an_exception_comes_back_as_a_tool_error(self):
        def broken(args):
            raise OSError(22, "Invalid argument")
        result = json.loads(execute_tool("thing", {}, "m", None, None, {"thing": broken}))
        self.assertIn("thing failed: OSError", result["error"])
        self.assertIn("don't repeat the same call", result["error"])

    def test_interrupts_still_get_through(self):
        def stop(args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            execute_tool("thing", {}, "m", None, None, {"thing": stop})


class FileToolsGivenAUrl(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        before = paths.workspace()
        paths.set_workspace(self.dir.name, allow_unsafe=True)
        self.addCleanup(paths.set_workspace, before, allow_unsafe=True)

    def test_the_rocketreach_crash_now_points_at_web_fetch(self):
        url = "https://rocketreach.co/prashant-deshpande-email_154215452"
        for tool, args in (("read_file", {"path": url}), ("write_file", {"path": url, "content": "x"}),
                           ("list_directory", {"path": url})):
            result = FILE_TOOL_HANDLERS[tool](args)
            self.assertIn("is a web address, not a file", result, tool)
            self.assertIn("web_fetch", result, tool)

    def test_ordinary_paths_still_work(self):
        (Path(self.dir.name) / "a.txt").write_text("hello", encoding="utf-8")
        self.assertIn("hello", FILE_TOOL_HANDLERS["read_file"]({"path": "a.txt"}))


class BrowserOpenAfterTheWindowWentAway(unittest.TestCase):
    def session(self, dispatch):
        browser = browser_tools.BrowserSession("m")
        browser.actions = object()
        browser._dispatch = mock.Mock(side_effect=dispatch)
        browser._lost = mock.Mock(return_value=True)
        browser._drop = mock.Mock()
        return browser

    def test_open_starts_a_new_window_instead_of_failing(self):
        browser = self.session([CDPError("Target closed"), "Opened https://x.test."])
        self.assertEqual(browser.call("browser_open", {"url": "https://x.test"}), "Opened https://x.test.")
        self.assertEqual(browser._dispatch.call_count, 2)
        browser._drop.assert_called_once()

    def test_open_gives_up_after_one_retry(self):
        browser = self.session([CDPError("Target closed"), CDPError("Target closed")])
        result = json.loads(browser.call("browser_open", {"url": "https://x.test"}))
        self.assertIn("closed or crashed", result["error"])
        self.assertEqual(browser._dispatch.call_count, 2)

    def test_other_tools_are_not_retried(self):
        browser = self.session([CDPError("Target closed")])
        result = json.loads(browser.call("browser_click", {"element": 3}))
        self.assertIn("browser_open starts a new one", result["error"])
        self.assertEqual(browser._dispatch.call_count, 1)


@unittest.skipUnless(sys.platform == "win32", "console windows are a Windows thing")
class NoConsoleFlashes(unittest.TestCase):
    def test_workers_get_a_hidden_console_not_none(self):
        flags = transport.detached()["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)

    def test_git_during_resume_opens_no_window(self):
        with mock.patch.object(resume.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "M a.py\n", "")
            resume.git_lines(".", "status", "--short")
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_sandboxd_opens_no_window(self):
        with mock.patch.object(sandbox_client.subprocess, "Popen") as popen, \
                mock.patch.object(sandbox_client._SANDBOXD_PATH.__class__, "exists", return_value=True):
            sandbox_client._start()
        self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_killing_a_worker_opens_no_window(self):
        with mock.patch.object(transport, "pid_alive", return_value=True), \
                mock.patch.object(transport.subprocess, "run") as run:
            transport.terminate(1234)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
