"""Tk integration smoke tests; run with a Python that includes Tk and a display."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
    from bt_delta.gui import App, generate_reports
except ImportError:
    tk = None
from bt_delta.planner import canonical, digest, verify_seal

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

    def test_two_tabs_and_pack_specific_fields(self):
        self.assertEqual(
            ["1. Inputs", "2. Review & approve"],
            [self.app.tabs.tab(tab, "text") for tab in self.app.tabs.tabs()],
        )
        self.assertTrue(str(self.app.settings).startswith(str(self.app.templates)))
        self.assertTrue(
            str(self.app.changelist_entry).startswith(str(self.app.templates))
        )
        self.assertEqual("Check checklist differences", self.app.compare_button["text"])
        self.assertEqual(2, int(self.app.overrides["height"]))
        for removed in (
            "preview_box",
            "comparison_box",
            "detail_box",
            "log_box",
            "demo",
        ):
            self.assertFalse(hasattr(self.app, removed))
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

    def test_generated_reports_stay_on_disk_and_review_stays_compact(self):
        from bt_delta.demo import fixture

        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / "workspace")
            self.app.set_config(config)
            config = self.app.get_config()
            self.app.input_hash = digest(canonical(config))
            with patch("bt_delta.gui.BASE", Path(directory)), patch(
                "bt_delta.gui.P4CLI", return_value=p4
            ):
                result = generate_reports(config)
            self.app.show_plan(result)
            rendered = self.app.summary_box.get("1.0", "end")
            self.assertIn("PROPOSED EDITS", rendered)
            self.assertIn("Reference:", rendered)
            reason = next(
                c["message"]
                for c in result["plan"]["checks"]
                if c["status"] == "review"
            )
            self.assertNotIn(reason, rendered)
            self.assertIn(reason, result["path"].with_name("plan.txt").read_text())
            self.assertTrue(
                result["path"].with_name("empty-file-previews.txt").is_file()
            )
            self.assertFalse(result["path"].with_name("comparison.txt").exists())
            self.assertEqual(str(self.app.review), self.app.tabs.select())
            self.app.approved.set(True)
            self.app.update_apply()
            self.assertEqual("normal", str(self.app.apply_button["state"]))
            self.app.values["perforce.user"].set("changed-user")
            self.assertEqual("disabled", str(self.app.apply_button["state"]))

    def test_background_comparison_saves_reports_without_display(self):
        from bt_delta.demo import fixture
        from test_comparison import HistoryP4

        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / "workspace")
            config["comparison"] = {"source": "submitted"}
            self.app.set_config(config)
            self.app.changelist.set("100, 100")
            config = self.app.get_config()
            self.assertEqual(["100"], config["comparison"]["changelists"])
            p4.__class__ = HistoryP4
            p4.history = {(p, row[0]): row[1] for p, row in p4.data.items()}
            p4.reads, p4.unreadable = [], set()
            p4.descriptions = {
                "100": {
                    "number": "100",
                    "status": "submitted",
                    "user": "dev",
                    "client": "dev_workspace",
                    "description": "Bring-up",
                }
            }
            p4.change_files = {"100": []}
            retained_plan = {"mode": "live"}
            self.app.plan = retained_plan
            retained_path = Path(directory) / "existing-plan.json"
            self.app.plan_path = retained_path
            self.app.tabs.select(self.app.templates)

            def run_now(work, completed, message):
                completed(work())

            with patch("bt_delta.gui.BASE", Path(directory)), patch(
                "bt_delta.gui.P4CLI", return_value=p4
            ), patch.object(self.app, "run", side_effect=run_now):
                self.app.check_differences()
            folder = next((Path(directory) / "reports").iterdir())
            self.assertTrue((folder / "comparison.txt").is_file())
            self.assertTrue((folder / "comparison.json").is_file())
            self.assertTrue(
                (folder / "blank-plan" / "empty-file-previews.txt").is_file()
            )
            self.assertIs(retained_plan, self.app.plan)
            self.assertEqual(retained_path, self.app.plan_path)
            self.assertEqual(str(self.app.templates), self.app.tabs.select())
            self.assertEqual(2, len(self.app.tabs.tabs()))
            self.assertNotIn(
                "SELECTED CHANGELISTS", self.app.summary_box.get("1.0", "end")
            )
            self.assertIn("comparison.txt", self.app.status.get())
            self.assertFalse(p4.calls)

    def test_generate_plan_does_not_run_changelist_comparison(self):
        from bt_delta.demo import fixture

        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / "workspace")
            config["comparison"] = {"changelists": ["100"]}
            with patch("bt_delta.gui.BASE", Path(directory)), patch(
                "bt_delta.gui.P4CLI", return_value=p4
            ), patch("bt_delta.gui.compare_changelist") as compare:
                result = generate_reports(config)
            compare.assert_not_called()
            verify_seal(json.loads(result["path"].read_text()))
            self.assertFalse(result["path"].with_name("comparison.txt").exists())

    def test_difference_button_requires_changelists_before_starting_work(self):
        from bt_delta.demo import fixture

        with tempfile.TemporaryDirectory() as directory:
            _, config = fixture(directory)
            self.app.set_config(config)
            with patch.object(self.app, "run") as run, patch(
                "bt_delta.gui.messagebox.showerror"
            ) as error:
                self.app.check_differences()
            run.assert_not_called()
            self.assertIn("numbered developer changelist", error.call_args.args[1])

    def test_background_work_keeps_active_tab_and_does_not_stream_logs(self):
        import threading

        started, finish = threading.Event(), threading.Event()

        def work():
            started.set()
            finish.wait(5)
            return "done"

        completed = []
        self.app.tabs.select(self.app.templates)
        self.app.run(work, completed.append, "Generating plan…")
        self.assertTrue(started.wait(2))
        self.assertEqual(str(self.app.templates), self.app.tabs.select())
        self.assertEqual("Generating plan…", self.app.status.get())
        finish.set()
        # The worker reports only completion; no per-request GUI log messages.
        callback, result, error = self.app.events.get(timeout=2)
        self.app.events.put((callback, result, error))
        self.app.poll()
        self.assertEqual(["done"], completed)
        self.assertFalse(self.app.busy)

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
            self.app.review_plan = {"changes": [{}]}
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

    def test_saved_advanced_inputs_and_changelist_round_trip(self):
        config = {
            "rules_file": str(ROOT / "checklist/slsi.json"),
            "ap": "erd8825",
            "products": ["m34xnsxx", "m34xjxx"],
            "model": "m34x",
            "jdm": True,
            "perforce": {"port": "p", "user": "u", "client": "c"},
            "comparison": {"changelists": ["100"]},
        }
        self.app.set_config(config)
        saved = self.app.get_config(checked=False)
        self.assertEqual("erd8825", saved["ap"])
        self.assertEqual(config["products"], saved["products"])
        self.assertIs(True, saved["jdm"])
        self.assertEqual("100", self.app.changelist.get())
        self.app.select_rules()
        self.assertEqual(
            config["comparison"], self.app.get_config(checked=False)["comparison"]
        )
        self.app.changelist.set("")
        self.assertNotIn("comparison", self.app.get_config(checked=False))

    def test_changelist_edit_invalidates_approval_and_rejects_invalid_numbers(self):
        self.app.approved.set(True)
        self.app.changelist.set("123, invalid")
        self.assertFalse(self.app.approved.get())
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.app.get_config()

    def review_fixture(self, directory):
        from bt_delta.demo import fixture

        p4, config = fixture(Path(directory) / "workspace")
        self.app.set_config(config)
        config = self.app.get_config()
        self.app.input_hash = digest(canonical(config))
        with patch("bt_delta.gui.BASE", Path(directory)), patch("bt_delta.gui.P4CLI", return_value=p4):
            result = generate_reports(config)
        self.app.show_plan(result)
        return p4, result

    def test_uncheck_single_flag_updates_diff_and_clears_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            _, result = self.review_fixture(directory)
            path = next(c["path"] for c in result["plan"]["changes"] if "system.board" in c["rules"])
            choice = next(c for c in self.app.selection.choices[path] if "BOARD_HAVE_BLUETOOTH_SLSI :=" in c.label)
            self.app.approved.set(True)
            self.app.update_apply()
            self.assertEqual("normal", str(self.app.apply_button["state"]))
            self.app.edit_choices[path][choice.id].set(False)
            self.assertFalse(self.app.approved.get())
            self.assertEqual("disabled", str(self.app.apply_button["state"]))
            self.assertEqual(-1, self.app.file_choices[path].get())
            self.assertTrue(self.app.file_widgets[path].instate(["alternate"]))
            self.app.set_selection_enabled(True)
            self.assertTrue(self.app.file_widgets[path].instate(["alternate"]))
            diff = next(c["diff"] for c in self.app.review_plan["changes"] if c["path"] == path)
            self.assertNotIn("+BOARD_HAVE_BLUETOOTH_SLSI", diff)
            self.assertIn("+BOARD_HAVE_BLUETOOTH := true", diff)
            self.assertIn(diff, self.app.summary_box.get("1.0", "end"))
            verify_seal(self.app.plan)
            self.assertEqual(result["plan"]["digest"], self.app.plan["digest"])

    def test_file_and_all_checkboxes_update_children_and_empty_selection_disables_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            _, result = self.review_fixture(directory)
            path = result["plan"]["changes"][0]["path"]
            self.app.file_choices[path].set(0)
            self.app.toggle_file(path)
            self.assertFalse(any(v.get() for v in self.app.edit_choices[path].values()))
            self.assertNotIn(path, [c["path"] for c in self.app.review_plan["changes"]])
            self.app.file_choices[path].set(1)
            self.app.toggle_file(path)
            self.assertTrue(all(v.get() for v in self.app.edit_choices[path].values()))
            self.app.select_all_edits(False)
            self.app.approved.set(True)
            self.app.update_apply()
            self.assertEqual("disabled", str(self.app.apply_button["state"]))
            self.assertEqual([], self.app.review_plan["changes"])
            self.assertIn("0 of", self.app.selection_count.get())
            with patch.object(self.app, "run") as run:
                self.app.apply()
            run.assert_not_called()
            self.app.select_all_edits(True)
            self.assertFalse(self.app.approved.get())
            self.assertEqual(len(result["plan"]["changes"]), len(self.app.review_plan["changes"]))

    def test_apply_uses_selected_plan_and_saves_original_and_selection_separately(self):
        import base64

        with tempfile.TemporaryDirectory() as directory:
            p4, result = self.review_fixture(directory)
            original = result["path"].read_bytes()
            self.app.select_all_edits(False)
            path = next(c["path"] for c in result["plan"]["changes"] if "system.board" in c["rules"])
            choice = next(c for c in self.app.selection.choices[path] if "BOARD_HAVE_BLUETOOTH :=" in c.label)
            self.app.edit_choices[path][choice.id].set(True)
            reviewed = self.app.review_plan
            self.app.approved.set(True)

            def run_now(work, completed, message):
                completed(work())

            with patch.object(self.app, "run", side_effect=run_now), patch("bt_delta.gui.P4CLI", return_value=p4), patch("bt_delta.gui.messagebox.showinfo"), patch("bt_delta.gui.messagebox.showwarning"):
                self.app.apply()
            folder = result["path"].parent / ("selected-" + reviewed["digest"][:12])
            saved = json.loads((folder / "plan.json").read_text())
            verify_seal(saved)
            self.assertEqual(reviewed, saved)
            self.assertEqual(original, result["path"].read_bytes())
            self.assertIn("EXCLUDE", (folder / "plan.txt").read_text())
            self.assertTrue(list(folder.glob("execution-*.json")))
            self.assertEqual(base64.b64decode(reviewed["changes"][0]["after"]), Path(p4.where(path)).read_bytes())
            self.assertEqual(1, len([c for c in p4.calls if c[0] in ("edit", "add")]))
            self.assertIsNone(self.app.plan)
            self.assertFalse(self.app.approved.get())

    def test_new_plan_resets_choices_and_busy_work_freezes_them(self):
        with tempfile.TemporaryDirectory() as directory:
            _, result = self.review_fixture(directory)
            self.app.select_all_edits(False)
            self.app.show_plan(result)
            self.assertTrue(all(v.get() for f in self.app.edit_choices.values() for v in f.values()))
            self.app.busy = True
            self.app.set_selection_enabled(False)
            self.assertTrue(all(w.instate(["disabled"]) for w in self.app.choice_widgets))
            self.app.select_all_edits(False)
            self.assertTrue(all(v.get() for f in self.app.edit_choices.values() for v in f.values()))
            self.app.choice_widgets[2].invoke()
            self.assertTrue(all(v.get() for f in self.app.edit_choices.values() for v in f.values()))

    def test_failed_regeneration_cannot_apply_old_plan_with_new_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            self.review_fixture(directory)
            self.app.values["model"].set("different")
            with patch.object(self.app, "run"):
                self.app.generate()
            self.app.approved.set(True)
            self.app.update_apply()
            self.assertIsNone(self.app.plan)
            self.assertEqual("disabled", str(self.app.apply_button["state"]))
