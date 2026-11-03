import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import worker

import agent
from providers import ProviderResponse


class Scripted:
    def __init__(self):
        self.calls = 0

    def generate(self, history, tools, system_prompt):
        self.calls += 1
        print("noise from agent code")
        os.write(1, b"raw noise on file descriptor 1\n")
        return ProviderResponse(text=f"reply {self.calls}: {history[-1].text}", tool_calls=[])


model = Scripted()
agent.get_provider = lambda key: model

if __name__ == "__main__":
    sys.exit(worker.main())
