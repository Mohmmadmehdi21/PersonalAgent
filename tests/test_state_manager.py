import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import state_manager


class StatePersistenceTests(unittest.TestCase):
    def test_state_round_trip_uses_atomic_replace(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            state_path = Path(temp) / "state.json"
            with patch.object(state_manager, "STATE_FILE", str(state_path)):
                state_manager.save_state_atomic({"done": ["video"], "clips": []})
                self.assertEqual(state_manager.load_state()["done"], ["video"])
                self.assertEqual(list(Path(temp).glob(".state-*.tmp")), [])

    def test_corrupt_state_is_preserved_and_not_replaced_with_empty_state(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            state_path = Path(temp) / "state.json"
            original = "{broken"
            state_path.write_text(original, encoding="utf-8")
            with patch.object(state_manager, "STATE_FILE", str(state_path)):
                with self.assertRaisesRegex(RuntimeError, "preserved"):
                    state_manager.load_state()
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)

    def test_failed_replace_keeps_previous_state_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            state_path = Path(temp) / "state.json"
            original = {"done": ["previous"]}
            state_path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(state_manager, "STATE_FILE", str(state_path)), \
                 patch.object(state_manager.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(RuntimeError, "previous file was preserved"):
                    state_manager.save_state_atomic({"done": ["new"]})
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)
            self.assertEqual(list(Path(temp).glob(".state-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
