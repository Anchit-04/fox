import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_run


def call(seq, task_id=None, *, inp=0, out=0, read=0, write=0, cost=0.0, source="catalog", ms=1000,
         tools="T", system="S", messages=("m1",)):
    return {"seq": seq, "task_id": task_id, "input_tokens": inp, "output_tokens": out, "cache_read_tokens": read,
            "cache_write_tokens": write, "cost": cost, "cost_source": source, "duration_ms": ms,
            "prefix": {"tools": tools, "system": system, "messages": list(messages)}}


class UsageSummary(unittest.TestCase):
    def test_totals_and_cache_hit_rate(self):
        s = check_run.usage_summary([call(1, inp=1000, out=50, write=3000, cost=0.02, source="provider"),
                                     call(2, inp=200, out=40, read=3800, cost=0.01)])
        self.assertEqual((s["calls"], s["prompt_tokens"], s["output_tokens"]), (2, 8000, 90))
        self.assertAlmostEqual(s["cost"], 0.03)
        self.assertAlmostEqual(s["cache_hit_rate"], 3800 / 8000)
        self.assertEqual((s["billed"], s["estimated"], s["unknown"]), (1, 1, 0))
        self.assertEqual(s["model_seconds"], 2.0)

    def test_unknown_costs_are_counted_not_treated_as_zero(self):
        s = check_run.usage_summary([call(1, cost=None, source=None), call(2, cost=0.5)])
        self.assertEqual((s["cost"], s["unknown"]), (0.5, 1))
        self.assertIsNone(check_run.usage_summary([call(1, cost=None, source=None)])["cost"])

    def test_no_prompt_tokens_has_no_hit_rate(self):
        self.assertIsNone(check_run.usage_summary([call(1)])["cache_hit_rate"])


class PrefixBreaks(unittest.TestCase):
    def test_a_growing_conversation_never_breaks(self):
        calls = [call(1, messages=["a"]), call(2, messages=["a", "b", "c"]), call(3, messages=["a", "b", "c", "d"])]
        self.assertEqual(check_run.prefix_breaks(calls), [])

    def test_each_kind_of_break_is_named(self):
        calls = [call(1, messages=["a", "b"]), call(2, system="S2", messages=["a", "b", "c"]),
                 call(3, system="S2", tools="T2", messages=["a", "b", "c"]),
                 call(4, system="S2", tools="T2", messages=["a", "X", "c", "d"]),
                 call(5, system="S2", tools="T2", messages=["summary"])]
        self.assertEqual(check_run.prefix_breaks(calls), [
            "orchestrator call at seq 2: system prompt changed",
            "orchestrator call at seq 3: tools changed",
            "orchestrator call at seq 4: message 2 changed",
            "orchestrator call at seq 5: message 1 changed",
        ])

    def test_each_agent_is_compared_with_its_own_previous_call(self):
        calls = [call(1, messages=["a"]), call(2, "t1", system="exec", messages=["x"]),
                 call(3, messages=["a", "b"]), call(4, "t1", system="exec", messages=["x", "y"])]
        self.assertEqual(check_run.prefix_breaks(calls), [])


class Times(unittest.TestCase):
    def test_task_elapsed_runs_from_first_start_to_its_end(self):
        def status(ts, tid, s):
            return {"type": "task_status_changed", "ts": ts, "payload": {"task_id": tid, "status": s}}
        state = {"turns": [status("2026-10-30T10:00:00.000Z", "t1", "running"),
                           status("2026-10-30T10:00:30.000Z", "t1", "needs-approval"),
                           status("2026-10-30T10:00:40.000Z", "t1", "running"),
                           status("2026-10-30T10:01:15.500Z", "t1", "done"),
                           status("2026-10-30T10:00:05.000Z", "t2", "running")]}
        self.assertEqual(check_run.task_times(state), {"t1": 75.5})

    def test_clock(self):
        self.assertEqual((check_run._clock(208), check_run._clock(9.6), check_run._clock(None)), ("3m28s", "10s", "?"))


if __name__ == "__main__":
    unittest.main()
