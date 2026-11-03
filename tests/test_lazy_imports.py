import subprocess
import sys
import unittest
from pathlib import Path

LOOM = Path(__file__).resolve().parent.parent / "loom"
SDKS = ("google.genai", "openai", "anthropic")


def loaded_after(code: str) -> list[str]:
    probe = f"import sys\n{code}\nprint(','.join(m for m in {SDKS!r} if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", probe], cwd=LOOM, capture_output=True, text=True, check=True)
    return [m for m in out.stdout.strip().split(",") if m]


class LazyProviderImports(unittest.TestCase):
    def test_importing_fox_loads_no_provider_sdk(self):
        self.assertEqual(loaded_after("import providers, agent, calls\nfrom config import routing, vault"), [])

    def test_building_a_provider_loads_only_its_own_sdk(self):
        self.assertEqual(loaded_after("from providers.config import provider_class\nprovider_class('openai_compatible')"),
                         ["openai"])


if __name__ == "__main__":
    unittest.main()
