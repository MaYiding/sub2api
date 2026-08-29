#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "sub2api-auto-recharge.py"
SPEC = importlib.util.spec_from_file_location("sub2api_auto_recharge", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AutoRechargeRuntimeTest(unittest.TestCase):
    def test_resolves_active_blue_green_container_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / ".blue-green-active"
            state_file.write_text(
                "active_container=sub2api-green\nactive_port=18081\n",
                encoding="utf-8",
            )
            with patch.object(MODULE, "STATE_FILE", state_file), patch.object(
                MODULE,
                "inspect_container",
                side_effect=["running", "healthy"],
            ):
                self.assertEqual(
                    MODULE.load_runtime(),
                    ("http://127.0.0.1:18081/api/v1", "sub2api-green"),
                )

    def test_rejects_unhealthy_active_container(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / ".blue-green-active"
            state_file.write_text(
                "active_container=sub2api-green\nactive_port=18081\n",
                encoding="utf-8",
            )
            with patch.object(MODULE, "STATE_FILE", state_file), patch.object(
                MODULE,
                "inspect_container",
                side_effect=["running", "unhealthy"],
            ):
                with self.assertRaisesRegex(RuntimeError, "not healthy"):
                    MODULE.load_runtime()


if __name__ == "__main__":
    unittest.main()
