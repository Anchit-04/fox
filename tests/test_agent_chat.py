import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import agent
import memory
from injection import InjectionQueue
from providers import ProviderResponse


class Chat:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def generate(self, history, tools, system_prompt):
        self.requests.append(list(history))
        return self.steps.pop(0)()


class KeepAliveChat(unittest.TestCase):
    def test_the_agent_remembers_its_own_reply_on_a_follow_up(self):
        queue = InjectionQueue()

        def first():
            queue.push(None, "and the tests?")
            return ProviderResponse(text="It is a shop app.", tool_calls=[])

        def second():
            queue.close(None)
            return ProviderResponse(text="Five test files.", tool_calls=[])

        chat, events = Chat(first, second), []
        with mock.patch.object(agent, "get_provider", lambda key: chat):
            final = agent.run_agent("what is this?", verbose=False, model_key="nemotron-super", interactive=False,
                                    mem=memory.Memory.scratch(), injection_queue=queue, keep_alive=True,
                                    event_sink=lambda e, p: events.append((e, p)))
        self.assertEqual(final, "Five test files.")
        follow_up = chat.requests[1]
        self.assertEqual([(t.role, t.text) for t in follow_up],
                         [("user", "what is this?"), ("assistant", "It is a shop app."),
                          ("user", "[Message from human]: and the tests?")])
        self.assertEqual([p for e, p in events if e == "awaiting_input"], [{"task_id": None}, {"task_id": None}])


if __name__ == "__main__":
    unittest.main()
