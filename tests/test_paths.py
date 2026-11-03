import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import paths


class ValidateWorkspace(unittest.TestCase):
    def test_a_real_folder_is_returned_resolved_and_the_global_is_untouched(self):
        before = paths.workspace()
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(paths.validate_workspace(folder), Path(folder).resolve())
        self.assertEqual(paths.workspace(), before)

    def test_missing_files_home_and_roots_are_refused(self):
        with tempfile.NamedTemporaryFile() as file:
            for bad in (Path(file.name).with_name("does-not-exist"), file.name, Path.home(),
                        Path(Path.home().anchor)):
                with self.subTest(bad=bad), self.assertRaises(paths.WorkspaceError):
                    paths.validate_workspace(bad)

    def test_home_is_allowed_when_asked(self):
        self.assertEqual(paths.validate_workspace(Path.home(), allow_unsafe=True), Path.home().resolve())


class WorkspaceKey(unittest.TestCase):
    def test_spelling_differences_collapse(self):
        same = paths.workspace_key("C:/Users/me/Desktop/shop") == paths.workspace_key(r"c:\users\me\desktop\shop\.")
        self.assertEqual(same, sys.platform == "win32")


if __name__ == "__main__":
    unittest.main()
