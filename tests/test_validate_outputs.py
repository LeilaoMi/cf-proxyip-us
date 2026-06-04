from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ValidateOutputsTest(unittest.TestCase):
    def test_validate_outputs_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/validate_outputs.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["valid_count"], 5)


if __name__ == "__main__":
    unittest.main()
