import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from history import History
from providers import Turn


class HistoryTests(unittest.TestCase):
    def test_starts_with_the_task(self):
        h = History("rename get_user")
        self.assertEqual(h.turns, [Turn(role="user", text="rename get_user")])

    def test_add_appends_in_order(self):
        h = History("task")
        a, b = Turn(role="assistant", text="a"), Turn(role="user", text="b")
        h.add(a)
        h.add(b)
        self.assertEqual(h.turns[1:], [a, b])

    def test_human_messages_become_turns_and_events(self):
        seen = []
        h = History("task")
        h.add_human_messages(["stop", "use argparse"], "t2", lambda e, p: seen.append((e, p)))
        self.assertEqual([t.text for t in h.turns[1:]],
                         ["[Message from human]: stop", "[Message from human]: use argparse"])
        self.assertEqual(seen, [("human_message_injected", {"task_id": "t2", "content": "stop"}),
                                ("human_message_injected", {"task_id": "t2", "content": "use argparse"})])

    def test_no_messages_and_no_sink_are_fine(self):
        h = History("task")
        h.add_human_messages([], None, None)
        h.add_human_messages(["hi"], None, None)
        self.assertEqual(len(h.turns), 2)

    def test_summary_replaces_everything(self):
        h = History("task")
        h.add(Turn(role="assistant", text="a"))
        h.replace_with_summary("so far: renamed it")
        self.assertEqual(h.turns, [Turn(role="user", text="so far: renamed it")])


if __name__ == "__main__":
    unittest.main()
