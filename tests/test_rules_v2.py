"""Rule-file contracts with non-Bluetooth data and no server access."""

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.blank import BlankPlanner
from bt_delta.comparison import compare_changelist, comparison_summary
from bt_delta.config import validate, load
from bt_delta.demo import DemoP4
from bt_delta.executor import execute
from bt_delta.expressions import Values, Unresolved
from bt_delta.operations import (
    remove_content,
    ensure_text_lines,
    xml_operation,
    apply_actions,
    select_actions,
)
from bt_delta.planner import Planner, verify_seal
from bt_delta.resolver import Resolver, DiscoveryMiss
from bt_delta.spec import load_spec, validate_spec, SpecError
from test_comparison import HistoryP4

ROOT = Path(__file__).resolve().parents[1]


class RuleFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = load_spec(ROOT / "examples/camera-rules.json")
        self.config = {
            "baseline_template": "OLD",
            "destination_template": "NEW",
            "component": "camera",
            "perforce": {"port": "OFFLINE", "user": "demo", "client": "demo_workspace"},
        }
        self.p4 = DemoP4(self.root / "workspace")
        for name, side in [("OLD", "old"), ("NEW", "new")]:
            self.p4.specs[name] = {
                "Client": name,
                "View0": f"//CUSTOM/{side}/... //{name}/...",
            }
        self.put(
            "old",
            "board.mk",
            "CAMERA_ENABLED := true\nCAMERA_SENSOR := wide\nUNRELATED := leave\n",
        )
        self.put("new", "board.mk", "UNRELATED := keep\n")
        self.put(
            "old",
            "modules.mk",
            "ifeq ($(MODE),pro)\nCAMERA_MODULES += camera_front camera_raw\nelse\nCAMERA_MODULES += camera_front\nendif\n",
        )
        self.put("new", "modules.mk", "CAMERA_MODULES += diagnostics\n")
        self.put(
            "old",
            "init.rc",
            "on post-fs-data\n    mkdir /data/vendor/camera 0770 camera system\n",
        )
        self.put("new", "init.rc", "on boot\n    setprop unrelated.keep true\n")
        self.put(
            "old",
            "devices.xml",
            '<devices><device name="front"><version>2.1</version><driver>camera</driver></device><device name="unrelated"/></devices>',
        )
        self.put(
            "new",
            "devices.xml",
            '<devices><!-- keep comment --><device name="rear"><version>9</version></device></devices>',
        )
        self.put(
            "old",
            "settings.json",
            '{"capture":{"mode":"new","fps":60},"formats":["raw","jpeg"]}',
        )
        self.put(
            "new",
            "settings.json",
            '{"capture":{"mode":"user"},"formats":["jpeg"],"keep":true}',
        )
        self.put("new", "legacy.conf", "camera.legacy=true\nother.legacy=keep\n")
        self.put("old", "assets/lens.bin", b"\x00\x01reference", kind="binary")
        self.reads = []
        original = self.p4.read_file

        def read(path, rev=None):
            self.reads.append(path)
            return original(path, rev)

        self.p4.read_file = read

    def path(self, side, name):
        return "//CUSTOM/" + side + "/src/camera/" + name

    def put(self, side, name, content, kind="text"):
        path = self.path(side, name)
        self.p4.data[path] = (
            1,
            content.encode() if isinstance(content, str) else content,
            kind,
        )
        return path

    def plan(self, spec=None, blank=False):
        return (BlankPlanner if blank else Planner)(
            self.p4, self.config, spec or self.spec
        ).build()

    def after(self, plan, name):
        return base64.b64decode(
            next(c for c in plan["changes"] if c["path"] == self.path("new", name))[
                "after"
            ]
        )

    def history(self):
        self.p4.__dict__.pop("read_file", None)
        self.p4.__class__ = HistoryP4
        self.p4.history = {(p, v[0]): v[1] for p, v in self.p4.data.items()}
        self.p4.reads = []
        self.p4.unreadable = set()
        self.p4.descriptions = {
            "100": {
                "number": "100",
                "status": "submitted",
                "user": "author",
                "client": "other",
                "description": "Camera changes",
            }
        }
        self.p4.change_files = {"100": []}
        self.p4.shelves = {}
        self.p4.shelf_content = {}
        self.p4.states = {}

    def submit(self, name, after):
        path = self.path("new", name)
        old = self.p4.data[path]
        rev = old[0] + 1
        self.p4.history[path, rev] = after
        self.p4.data[path] = (rev, after, old[2])
        self.p4.change_files["100"].append(
            {"path": path, "revision": rev, "type": old[2], "action": "edit"}
        )

    def compare(self):
        return compare_changelist(self.p4, self.config, "100", catalog=self.spec)


class GenericWorkflowTests(RuleFixture):
    def test_xml_source_selector_runs_without_top_level_selector(self):
        step = self.spec["rules"][3]["steps"][0]
        step["from"]["select"] = step.pop("select")
        plan = self.plan()
        self.assertFalse([c for c in plan["checks"] if c["status"] == "blocked"])
        self.assertIn(b'name="front"', self.after(plan, "devices.xml"))

    def test_drop_in_non_bluetooth_pack_plan_apply_and_idempotency(self):
        plan = self.plan()
        verify_seal(plan)
        self.assertFalse(
            [c for c in plan["checks"] if c["status"] in ("blocked", "review")]
        )
        self.assertEqual(7, len(plan["changes"]))
        self.assertIn(b"UNRELATED := keep", self.after(plan, "board.mk"))
        self.assertIn(b"ifeq ($(MODE),pro)", self.after(plan, "modules.mk"))
        self.assertIn(b"else", self.after(plan, "modules.mk"))
        self.assertIn(b"<!-- keep comment -->", self.after(plan, "devices.xml"))
        self.assertNotIn(b'name="unrelated"', self.after(plan, "devices.xml"))
        self.assertEqual(
            {"capture": {"mode": "user", "fps": 60}, "formats": ["jpeg"], "keep": True},
            json.loads(self.after(plan, "settings.json")),
        )
        self.assertEqual(b"other.legacy=keep\n", self.after(plan, "legacy.conf"))
        self.assertFalse(self.p4.calls)
        result = execute(
            self.p4, plan, plan["digest"], journal_path=self.root / "apply.json"
        )
        self.assertEqual("12345", str(result["change"]))
        for change in plan["changes"]:
            self.assertEqual(
                base64.b64decode(change["after"]),
                Path(change["local_path"]).read_bytes(),
            )
            self.p4.data[change["path"]] = (
                2,
                base64.b64decode(change["after"]),
                change["type"],
            )
        again = self.plan()
        self.assertFalse(again["changes"])
        self.assertFalse(
            [c for c in again["checks"] if c["status"] in ("review", "blocked")]
        )

    def test_blank_reads_reference_only_and_retains_removal_requirements(self):
        plan = self.plan(blank=True)
        self.assertFalse([c for c in plan["checks"] if c["status"] == "blocked"])
        self.assertFalse([p for p in self.reads if "/new/" in p])
        self.assertEqual(
            "absent",
            next(r for r in plan["requirements"] if r["rule"] == "remove.legacy")[
                "desired"
            ],
        )
        self.assertFalse(self.p4.calls)

    def test_comparison_removals_do_not_hide_unrelated_removed_lines(self):
        self.history()
        self.submit("legacy.conf", b"")
        result = next(
            f for f in self.compare()["files"] if f["filename"] == "legacy.conf"
        )
        self.assertEqual(1, len(result["matched"]))
        self.assertTrue(any(u["text"] == "other.legacy=keep" for u in result["extra"]))
        self.assertFalse(
            any(u["text"] == "camera.legacy=true" for u in result["extra"])
        )

    def test_required_absence_present_at_head_is_missing(self):
        self.history()
        report = self.compare()
        item = next(f for f in report["files"] if f["filename"] == "legacy.conf")
        self.assertEqual("absent", item["missing"][0]["desired"])
        self.assertIn("Required removal is not satisfied", comparison_summary(report))
        self.assertFalse(self.p4.calls)

    def test_required_absence_already_clean_is_satisfied(self):
        self.put("new", "legacy.conf", "other.legacy=keep\n")
        self.history()
        item = next(
            f for f in self.compare()["files"] if f["filename"] == "legacy.conf"
        )
        self.assertFalse(item["missing"])
        self.assertEqual("absent", item["already_present"][0]["desired"])

    def test_latest_xml_minimum_version_and_json_preserved_values(self):
        self.put(
            "new",
            "devices.xml",
            '<devices><device name="front"><version>2.9</version><driver>camera</driver></device></devices>',
        )
        self.put(
            "new",
            "settings.json",
            '{"capture":{"mode":"custom","fps":30},"formats":[]}',
        )
        self.history()
        report = self.compare()
        for name in ("devices.xml", "settings.json"):
            item = next(f for f in report["files"] if f["filename"] == name)
            self.assertFalse(item["missing"], item)
            self.assertTrue(
                any(u.get("semantic_match") for u in item["already_present"])
            )
        self.assertIn("minimum versions", comparison_summary(report))

    def test_xml_one_good_one_missing_identity_keeps_precise_results(self):
        self.spec["rules"][3]["steps"][0]["select"]["names"] = ["front", "unrelated"]
        self.put(
            "old",
            "devices.xml",
            '<devices><device name="front"><version>2.1</version><driver>camera</driver></device><device name="unrelated"><version>1</version><driver>other</driver></device></devices>',
        )
        self.put(
            "new",
            "devices.xml",
            '<devices><device name="front"><version>3.0</version><driver>camera</driver></device></devices>',
        )
        self.history()
        item = next(
            f for f in self.compare()["files"] if f["filename"] == "devices.xml"
        )
        self.assertTrue(item["missing"])
        self.assertTrue(all("unrelated" in u["text"] for u in item["missing"]))
        self.assertTrue(any("front" in u["text"] for u in item["already_present"]))

    def test_latest_init_command_anywhere_uses_declared_policy(self):
        self.put(
            "new",
            "init.rc",
            "on boot\n    mkdir /data/vendor/camera 0770 camera system\n",
        )
        self.history()
        item = next(f for f in self.compare()["files"] if f["filename"] == "init.rc")
        self.assertFalse(item["missing"])
        self.assertTrue(item["already_present"])
        self.spec["files"]["new.startup"]["comparison"]["latest_scope"] = "context"
        item = next(f for f in self.compare()["files"] if f["filename"] == "init.rc")
        self.assertTrue(item["missing"])

    def test_failed_later_step_rolls_back_entire_rule(self):
        self.spec["rules"] = [
            {
                "id": "atomic",
                "title": "Atomic rule",
                "steps": [
                    {
                        "op": "make.ensure",
                        "to": {"file": "new.board"},
                        "literal": {"CAMERA_NEW": "yes"},
                    },
                    {"op": "assert", "condition": False, "message": "No eligibility"},
                ],
            }
        ]
        plan = self.plan()
        self.assertEqual("blocked", plan["checks"][0]["status"])
        self.assertFalse(plan["changes"])
        self.assertFalse(plan["requirements"])

    def test_later_step_restoring_original_cancels_proposal(self):
        self.spec["rules"] = [
            {
                "id": "roundtrip",
                "title": "Roundtrip",
                "steps": [
                    {
                        "op": "make.ensure",
                        "to": {"file": "new.board"},
                        "literal": {"UNRELATED": "changed"},
                    },
                    {
                        "op": "make.ensure",
                        "to": {"file": "new.board"},
                        "literal": {"UNRELATED": "keep"},
                    },
                ],
            }
        ]
        self.assertFalse(self.plan()["changes"])

    def test_created_file_can_be_edited_by_following_rule(self):
        self.spec["rules"] = [
            {
                "id": "copy",
                "title": "Copy board",
                "steps": [
                    {
                        "op": "file.copy_if_missing",
                        "from": {"file": "old.board"},
                        "to": {"file": "new.board"},
                    }
                ],
            },
            {
                "id": "adjust",
                "title": "Adjust board",
                "depends_on": ["copy"],
                "steps": [
                    {
                        "op": "make.ensure",
                        "to": {"file": "new.board"},
                        "literal": {"CAMERA_NEW": "yes"},
                    }
                ],
            },
        ]
        del self.p4.data[self.path("new", "board.mk")]
        plan = self.plan()
        self.assertFalse(
            [c for c in plan["checks"] if c["status"] not in ("pass", "change")]
        )
        self.assertIn(b"CAMERA_NEW := yes", self.after(plan, "board.mk"))
        self.assertIn(b"CAMERA_SENSOR := wide", self.after(plan, "board.mk"))

    def test_renamed_inputs_sources_files_and_rules_work_without_python_mapping(self):
        encoded = (
            json.dumps(self.spec)
            .replace("baseline_template", "snapshot_name")
            .replace("destination_template", "working_name")
            .replace("old.", "input.")
            .replace("new.", "output.")
            .replace("configure.", "install.")
        )
        self.spec = json.loads(encoded)
        self.config["snapshot_name"] = self.config.pop("baseline_template")
        self.config["working_name"] = self.config.pop("destination_template")
        plan = self.plan()
        self.assertEqual(7, len(plan["changes"]))
        self.assertFalse([c for c in plan["checks"] if c["status"] == "blocked"])

    def test_false_file_condition_sends_no_query(self):
        self.spec["files"]["new.assets"]["when"] = False
        queried = []
        original = self.p4.files

        def files(path):
            queried.append(path)
            return original(path)

        self.p4.files = files
        plan = self.plan()
        self.assertFalse([q for q in queried if "/new/" in q and "/assets/" in q])
        self.assertEqual(
            "skipped",
            next(c for c in plan["checks"] if c["rule"] == "configure.assets")[
                "status"
            ],
        )

    def test_rules_file_change_invalidates_existing_preview(self):
        plan = self.plan()
        self.history()
        self.spec["rules"][0]["steps"][0]["select"]["assignment_patterns"] = ["^OTHER_"]
        with self.assertRaisesRegex(ValueError, "(?i)rules|catalog|rule"):
            compare_changelist(
                self.p4, self.config, "100", catalog=self.spec, preview_plan=plan
            )


class GrammarTests(RuleFixture):
    def invalid_before_network(self, update):
        spec = copy.deepcopy(self.spec)
        update(spec)
        with patch.object(
            self.p4, "identity", side_effect=AssertionError("network called")
        ):
            with self.assertRaises(SpecError):
                Planner(self.p4, self.config, spec)

    def test_invalid_grammar_and_references_fail_before_network(self):
        mutations = [
            lambda s: s.update(schema_version=1),
            lambda s: s["rules"][0]["steps"][0].update(op="bluetooth.magic"),
            lambda s: s["rules"][0]["steps"][0]["select"].update(typo=[]),
            lambda s: s["rules"][0]["steps"][0]["from"].update(file="missing"),
            lambda s: s["files"]["old.board"]["routes"][0].update(
                client_relative="${typo}/file"
            ),
            lambda s: s["rules"][0]["steps"][0]["select"].update(
                assignment_patterns=["["]
            ),
            lambda s: s["rules"][0]["steps"][0].update(literal=["wrong type"]),
            lambda s: s["inputs"]["component"].update(default=123),
            lambda s: s["rules"][0]["steps"][0].update(policy={"made_up": True}),
            lambda s: s["rules"][0]["steps"][0].update(policy={"on_missing": "skip"}),
            lambda s: s["rules"][0].update(depends_on=["not_a_rule"]),
            lambda s: s["rules"][0].update(depends_on=[s["rules"][0]["id"]]),
            lambda s: s["derive"]["component"].update(value={"var": "component"}),
            lambda s: s["derive"]["component"].update(value={"file_text": "old.board"}),
            lambda s: s["rules"][0]["steps"][0].update(literal={"FLAG": "${typo}"}),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.invalid_before_network(mutation)

    def test_invalid_explicit_file_never_falls_back(self):
        with self.assertRaisesRegex(SpecError, "Cannot load"):
            Planner(
                self.p4, {**self.config, "rules_file": str(self.root / "missing.json")}
            )
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":2,"schema_version":2}')
        with self.assertRaisesRegex(SpecError, "Duplicate"):
            load_spec(path)

    def test_input_derivation_order_and_cycle(self):
        self.spec["inputs"] = {
            "first": {
                "type": "string",
                "required": True,
                "derive": {"input": "second"},
            },
            "second": {"type": "string", "derive": {"input": "last"}},
            "last": {"type": "string", "default": "camera"},
            **self.spec["inputs"],
        }
        spec = validate_spec(self.spec)
        config = validate(self.config, spec)
        self.assertEqual("camera", config["first"])
        self.spec["inputs"]["last"]["derive"] = {"input": "first"}
        with self.assertRaisesRegex(SpecError, "cycle"):
            validate_spec(self.spec)

    def test_literal_data_keys_are_not_treated_as_expressions(self):
        self.spec["rules"] = [
            {
                "id": "literal",
                "title": "Literal",
                "steps": [
                    {
                        "op": "make.ensure",
                        "to": {"file": "new.board"},
                        "literal": {"var": "ordinary-setting"},
                    }
                ],
            }
        ]
        self.spec["lookups"] = {"data": {"a": {"var": "ordinary-json-key"}}}
        self.assertIn(b"var := ordinary-setting", self.after(self.plan(), "board.mk"))

    def test_saved_relative_catalog_path(self):
        (self.root / "rules.json").write_text(json.dumps(self.spec))
        (self.root / "inputs.json").write_text(
            json.dumps({**self.config, "rules_file": "rules.json"})
        )
        config = load(self.root / "inputs.json")
        self.assertEqual(str(self.root / "rules.json"), config["rules_file"])
        self.assertEqual("camera-example", Planner(self.p4, config).build()["rules_id"])

    def test_three_valued_conditions_short_circuit(self):
        spec = copy.deepcopy(self.spec)
        spec["derive"]["unknown"] = {"value": {"template": "${missing}"}}
        values = Values(spec, self.config)
        self.assertFalse(values.eval({"all": [False, {"var": "unknown"}]}))
        self.assertTrue(values.eval({"any": [True, {"var": "unknown"}]}))
        self.assertIsNone(values.eval({"all": [True, None]}))
        with self.assertRaises(Unresolved):
            values.condition({"any": [False, None]})

    def test_unresolved_client_route_can_use_explicit_alternative(self):
        self.spec["derive"]["optional"] = {"value": None}
        self.spec["files"]["old.board"]["routes"].insert(
            0, {"client_relative": "${optional}/board.mk"}
        )
        plan = self.plan()
        self.assertEqual("change", plan["checks"][0]["status"])


class PrimitiveTests(unittest.TestCase):
    def test_preserve_or_review_include_keeps_path_but_adds_missing_setting(self):
        for policy in ("preserve", "review"):
            with self.subTest(policy=policy):
                notices = []
                actions = select_actions(
                    "make.ensure",
                    "include new/common.mk\nB := yes\n",
                    {"include_basenames": ["common.mk"], "assignment_names": ["B"]},
                )
                actions[0]["on_value_difference"] = policy
                result = apply_actions(
                    "include old/common.mk # keep\n", actions, notices.append
                )
                self.assertEqual("include old/common.mk # keep\nB := yes\n", result)
                self.assertEqual(
                    policy == "review",
                    any(isinstance(n, dict) and n["review"] for n in notices),
                )

    def test_preserve_or_review_conflict_still_adds_missing_keys(self):
        for policy in ("preserve", "review"):
            notices = []
            actions = select_actions("make.ensure", "", "", {"A": "new", "B": "yes"})
            actions[0]["on_value_difference"] = policy
            result = apply_actions(
                "A := old # keep\nOTHER := yes\n", actions, notices.append
            )
            self.assertIn("A := old # keep", result)
            self.assertIn("B := yes", result)
            self.assertEqual(
                policy == "review",
                any(isinstance(n, dict) and n["review"] for n in notices),
            )

    def test_define_replace_and_raw_text_remove(self):
        before = "#define CAMERA_ON FALSE\n#define KEEP TRUE\n"
        result = ensure_text_lines(
            before,
            {
                "lines": ["#define CAMERA_ON TRUE"],
                "identity_pattern": r"^#define\s+(CAMERA_ON)\b",
            },
        )
        self.assertEqual("#define CAMERA_ON TRUE\n#define KEEP TRUE\n", result)
        self.assertEqual(
            "#define KEEP TRUE\n",
            remove_content(result, {"patterns": [r"^#define CAMERA_ON\b"]}),
        )

    def test_make_removal_keeps_unrelated_nested_else_and_preexisting_empty_guard(self):
        text = "ifeq ($(FEATURE),y)\nREMOVE := yes\nifdef OTHER\nDROP += gone keep\nelse\nKEEP := yes\nendif\nendif\nifdef EMPTY\nendif\n"
        result = remove_content(
            text,
            {
                "within": "FEATURE",
                "assignment_names": ["REMOVE"],
                "variable": "DROP",
                "member_names": ["gone"],
            },
            make=True,
        )
        self.assertIn("DROP += keep", result)
        self.assertIn("else\nKEEP := yes", result)
        self.assertIn("ifdef EMPTY\nendif", result)
        self.assertNotIn("REMOVE", result)

    def test_nested_xml_preserves_bytes_and_preview_context(self):
        ref = '<settings><cameras><device name="front"><version>2</version></device></cameras></settings>'
        current = '<settings>\n<!-- keep exactly -->\n<cameras><device name="rear" /></cameras>\n<other x="yes"/>\n</settings>'
        step = {
            "op": "xml.ensure",
            "select": {
                "path": "./cameras/device",
                "name_path": "@name",
                "names": ["front"],
            },
            "identity": ["@name"],
            "parent": "./cameras",
        }
        after, preview, notes = xml_operation(ref, current, step)
        self.assertFalse(notes)
        self.assertIn('<cameras><device name="front">', preview)
        self.assertIn("<!-- keep exactly -->", after)
        self.assertIn('<device name="rear" />', after)
        self.assertIn('<other x="yes"/>', after)
        again, _, _ = xml_operation(ref, after, step)
        self.assertEqual(after, again)

    def test_duplicate_xml_identity_fails(self):
        step = {"op": "xml.ensure", "select": {"path": "./item"}, "identity": ["@id"]}
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            xml_operation(
                '<root><item id="a"/></root>',
                '<root><item id="a"/><item id="a"/></root>',
                step,
            )


class PreservationTests(unittest.TestCase):
    def test_slsi_proposed_file_bytes_match_pre_refactor_baseline(self):
        from bt_delta.demo import fixture
        from bt_delta.planner import digest

        expected = json.loads((ROOT / "tests/fixtures/slsi-v1-output.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            for name, cls in [("normal", Planner), ("blank", BlankPlanner)]:
                with self.subTest(mode=name):
                    p4, config = fixture(directory)
                    config["check_csc_features"] = True
                    plan = cls(p4, config).build()
                    actual = {
                        c["path"]: digest(base64.b64decode(c["after"]))
                        for c in plan["changes"]
                    }
                    self.assertEqual(expected[name], actual)

    def test_cli_validates_file_without_perforce(self):
        from bt_delta.cli import main
        import contextlib, io

        with patch(
            "bt_delta.cli.P4CLI", side_effect=AssertionError("No P4 during validation")
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                0, main(["validate-rules", str(ROOT / "examples/camera-rules.json")])
            )
        self.assertIn("camera-example", output.getvalue())
