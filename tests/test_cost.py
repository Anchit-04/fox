import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

from providers import Usage
from providers.config import MODEL_REGISTRY, call_cost, cost_of, get_provider


class CostOf(unittest.TestCase):
    def test_each_kind_of_token_has_its_own_price(self):
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000,
                      cache_write_tokens=1_000_000)
        self.assertAlmostEqual(cost_of("claude-opus", usage), 4.0 + 20.0 + 0.20 + 5.0)

    def test_cache_reads_cost_a_fraction_of_input(self):
        cached = cost_of("claude-opus", Usage(cache_read_tokens=10_000))
        uncached = cost_of("claude-opus", Usage(input_tokens=10_000))
        self.assertAlmostEqual(cached / uncached, 0.05)

    def test_long_prompts_use_the_long_context_price(self):
        short = cost_of("claude-haiku", Usage(input_tokens=100_000))
        long = cost_of("claude-haiku", Usage(input_tokens=100_001))
        self.assertAlmostEqual(short, 0.01)
        self.assertAlmostEqual(long, 100_001 * 0.50 / 1_000_000)

    def test_free_models_cost_nothing(self):
        self.assertEqual(cost_of("nemotron-super", Usage(input_tokens=50_000, output_tokens=2_000)), 0.0)

    def test_unknown_price_or_model_is_none(self):
        self.assertIsNone(cost_of("deepseek-chat", Usage(input_tokens=10)))
        self.assertIsNone(cost_of("no-such-model", Usage(input_tokens=10)))


class CallCost(unittest.TestCase):
    def test_the_providers_bill_wins_over_the_catalog(self):
        self.assertEqual(call_cost("claude-opus", Usage(input_tokens=1_000_000, reported_cost=3.5)), (3.5, "provider"))

    def test_without_a_bill_the_catalog_estimates(self):
        self.assertEqual(call_cost("claude-opus", Usage(input_tokens=1_000_000)), (4.0, "catalog"))

    def test_without_either_the_cost_is_unknown(self):
        self.assertEqual(call_cost("deepseek-chat", Usage(input_tokens=10)), (None, None))


class Registry(unittest.TestCase):
    def test_every_model_states_its_price(self):
        missing = [key for key, cfg in MODEL_REGISTRY.items() if "price" not in cfg]
        self.assertEqual(missing, [])

    def test_price_is_not_passed_to_the_provider(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            self.assertEqual(get_provider("nemotron-super").model_id, "nvidia/nemotron-3.5-super-vl:free")


if __name__ == "__main__":
    unittest.main()
