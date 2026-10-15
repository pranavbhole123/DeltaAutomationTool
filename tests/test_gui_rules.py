"""Tk integration smoke tests; run with a Python that includes Tk and a display."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
    from bt_delta.gui import App
except ImportError:
    tk = None
from bt_delta.planner import canonical, digest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(tk is None, "Tkinter is not installed in this Python")
class DynamicFormTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.app = App(self.root)
        self.root.update_idletasks()
        self.addCleanup(self.root.destroy)

    def select(self, name):
        self.app.rules_path.set(str(ROOT / "checklist" / name))
        self.app.select_rules()
        self.root.update_idletasks()

    def test_eight_tabs_and_pack_specific_fields(self):
        self.assertEqual(8, len(self.app.tabs.tabs()))
        self.assertIn("check_csc_features", self.app.values)
        self.assertNotIn("ap", self.app.values)
        self.select("qcom-qca.json")
        self.assertEqual("qcom-qca", self.app.catalog["id"])
        self.assertIn("ant_support", self.app.values)
        self.assertNotIn("current.system_template", self.app.values)
        self.assertNotIn("check_csc_features", self.app.values)
        self.select("qcom-wcn.json")
        self.assertIn("current.system_template", self.app.values)
        self.assertNotIn("ant_support", self.app.values)

    def test_invalid_rules_leave_previous_selection_and_display_error(self):
        before = self.app.active_rules_path
        self.app.rules_path.set("/not/a/rules/file.json")
        with patch("bt_delta.gui.messagebox.showerror") as error:
            self.app.select_rules()
            self.assertTrue(error.called)
        self.assertEqual(before, self.app.active_rules_path)
        self.assertEqual(before, self.app.rules_path.get())

    def test_disk_rule_change_disables_apply_and_requires_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            spec = json.loads((ROOT / "examples/camera-rules.json").read_text())
            path.write_text(json.dumps(spec))
            self.app.set_config(
                {
                    "rules_file": str(path),
                    "baseline_template": "BASE",
                    "destination_template": "CUR",
                    "component": "camera",
                    "perforce": {"port": "server", "user": "user", "client": "client"},
                }
            )
            config = self.app.get_config()
            self.app.input_hash = digest(canonical(config))
            self.app.plan = {"mode": "live"}
            self.app.approved.set(True)
            self.app.update_apply()
            self.assertEqual("normal", str(self.app.apply_button["state"]))
            spec["rules"][0]["steps"][0]["select"]["assignment_patterns"] = ["^NEW_"]
            path.write_text(json.dumps(spec))
            self.app.update_apply()
            self.assertEqual("disabled", str(self.app.apply_button["state"]))
            with self.assertRaisesRegex(ValueError, "Reload"):
                self.app.get_config()
            self.app.select_rules()
            self.assertFalse(self.app.approved.get())
            self.assertEqual("camera-example", self.app.catalog["id"])

    def test_saved_hidden_input_and_list_round_trip(self):
        config = {
            "rules_file": str(ROOT / "checklist/slsi.json"),
            "ap": "erd8825",
            "products": ["m34xnsxx", "m34xjxx"],
            "model": "m34x",
            "jdm": True,
            "perforce": {"port": "p", "user": "u", "client": "c"},
        }
        self.app.set_config(config)
        saved = self.app.get_config(checked=False)
        self.assertEqual("erd8825", saved["ap"])
        self.assertEqual(config["products"], saved["products"])
        self.assertIs(True, saved["jdm"])
