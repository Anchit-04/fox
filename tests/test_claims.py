import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from orchestration.claims import FileClaims


class Claims(unittest.TestCase):
    def setUp(self):
        self.claims = FileClaims()

    def test_free_files_are_claimed(self):
        self.assertEqual(self.claims.claim("t1", ["a.py", "b.py"]), [])
        self.assertEqual((self.claims.owner("a.py"), self.claims.owner("b.py")), ("t1", "t1"))

    def test_unclaimed_file_has_no_owner(self):
        self.assertIsNone(self.claims.owner("a.py"))

    def test_conflict_returns_the_taken_files_and_claims_nothing(self):
        self.claims.claim("t1", ["a.py"])
        self.assertEqual(self.claims.claim("t2", ["b.py", "a.py"]), ["a.py"])
        self.assertIsNone(self.claims.owner("b.py"))
        self.assertEqual(self.claims.owner("a.py"), "t1")

    def test_a_task_can_reclaim_its_own_files(self):
        self.claims.claim("t1", ["a.py"])
        self.assertEqual(self.claims.claim("t1", ["a.py", "b.py"]), [])

    def test_paths_are_normalized(self):
        self.claims.claim("t1", ["./docs\\api.md"])
        self.assertEqual(self.claims.owner("docs/api.md"), "t1")
        self.assertEqual(self.claims.claim("t2", ["docs/api.md"]), ["docs/api.md"])

    def test_every_spelling_of_one_file_shares_a_claim(self):
        self.claims.claim("t1", ["docs/api.md"])
        for spelling in ("docs/x/../api.md", str(Path.cwd() / "docs" / "api.md")):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.claims.owner(spelling), "t1")
                self.assertEqual(self.claims.claim("t2", [spelling]), [spelling])

    @unittest.skipUnless(sys.platform == "win32", "paths are case-insensitive only on Windows")
    def test_case_does_not_matter_on_windows(self):
        self.claims.claim("t1", ["README.md"])
        self.assertEqual(self.claims.owner("readme.MD"), "t1")

    def test_release_frees_only_that_tasks_files(self):
        self.claims.claim("t1", ["a.py"])
        self.claims.claim("t2", ["b.py"])
        self.claims.release("t1")
        self.assertIsNone(self.claims.owner("a.py"))
        self.assertEqual(self.claims.owner("b.py"), "t2")
        self.assertEqual(self.claims.claim("t3", ["a.py"]), [])

    def test_only_one_of_many_racing_tasks_gets_a_file(self):
        winners = []
        lock = threading.Lock()

        def race(task_id):
            if not self.claims.claim(task_id, ["shared.py"]):
                with lock:
                    winners.append(task_id)

        threads = [threading.Thread(target=race, args=(f"t{i}",)) for i in range(32)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.claims.owner("shared.py"), winners[0])


if __name__ == "__main__":
    unittest.main()
