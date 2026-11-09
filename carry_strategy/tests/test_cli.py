import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def test_demo_and_replay_reproduce_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            demo = Path(root) / "demo"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "carry_strategy",
                    "demo",
                    "--output-dir",
                    str(demo),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            replay = Path(root) / "replay.sqlite"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "carry_strategy",
                    "replay",
                    "--config",
                    str(demo / "config.json"),
                    "--db",
                    str(replay),
                    "--events",
                    str(demo / "events.jsonl"),
                    "--output",
                    str(Path(root) / "out.jsonl"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            def state(path):
                connection = sqlite3.connect(path)
                result = json.loads(
                    connection.execute("SELECT payload FROM state").fetchone()[0]
                )
                connection.close()
                return result

            original = state(demo / "state.sqlite")
            rebuilt = state(replay)
            self.assertEqual(original["pairs"], rebuilt["pairs"])
            self.assertEqual(original["balances"], rebuilt["balances"])
            self.assertEqual(original["pairs"]["demo_gold"]["spot"], "2.49975")
            self.assertEqual(original["pairs"]["demo_gold"]["short"], "2.499")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "carry_strategy",
                    "demo",
                    "--output-dir",
                    str(demo),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(original["pairs"], state(demo / "state.sqlite")["pairs"])
