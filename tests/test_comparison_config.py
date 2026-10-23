"""Companion comparison files preserve existing packs and reject invalid wiring."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.config import load
from bt_delta.planner import canonical, digest
from bt_delta.schema import validate_document
from bt_delta.spec import load_spec, validate_spec, SpecError


ROOT = Path(__file__).resolve().parents[1]
PACKS = ("checklist/slsi.json", "checklist/qcom-wcn.json", "checklist/qcom-qca.json", "examples/camera-rules.json")


class ComparisonConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rules = json.loads((ROOT / "examples/camera-rules.json").read_text())
        self.profile = json.loads(
            (ROOT / "examples/camera-rules.comparison.json").read_text()
        )
        self.rules["comparison_file"] = "policies/comparison.json"
        self.path = self.root / "rules.json"
        self.comparison_path = self.root / "policies/comparison.json"
        self.comparison_path.parent.mkdir()
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.rules))
        self.comparison_path.write_text(json.dumps(self.profile))

    def test_all_migrated_packs_preserve_effective_settings_and_plan_hash_inputs(self):
        for name in PACKS:
            with self.subTest(pack=name):
                raw = json.loads((ROOT / name).read_text())
                self.assertIn("comparison_file", raw)
                self.assertNotIn("comparison", raw)
                self.assertTrue(all("comparison" not in f for f in raw["files"].values()))
                # Rule selectors can evolve independently of comparison settings.
                # An equivalent inline profile must still produce identical plan hashes.
                profile = json.loads(((ROOT / name).parent / raw.pop("comparison_file")).read_text())
                for file_id, policy in profile["files"].items():
                    raw["files"][file_id]["comparison"] = policy
                if "exclude" in profile:
                    raw["comparison"] = {"exclude": profile["exclude"]}
                self.assertEqual(digest(canonical(validate_spec(raw))), digest(canonical(load_spec(ROOT / name))))

    def test_relative_companion_uses_rules_directory_not_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            resolved = load_spec(self.path)
        finally:
            os.chdir(previous)
        self.assertEqual(["CAMERA_MODULES"], resolved["files"]["new.modules"]["comparison"]["list_variables"])

    def test_saved_inputs_resolve_both_relative_paths(self):
        inputs = self.root / "inputs.json"
        inputs.write_text(json.dumps({
            "rules_file": "rules.json", "baseline_template": "OLD",
            "destination_template": "NEW", "component": "camera",
            "perforce": {"port": "OFFLINE", "user": "demo", "client": "demo_workspace"},
        }))
        config = load(inputs)
        self.assertEqual(str(self.path), config["rules_file"])
        self.assertEqual(load_spec(self.path), load_spec(config=config))

    def test_absolute_companion_and_explicit_base_directory(self):
        self.assertEqual(load_spec(self.path), validate_spec(self.rules, base_dir=self.root))
        original = copy.deepcopy(self.rules)
        with self.assertRaisesRegex(SpecError, "requires the rules directory"):
            validate_spec(self.rules)
        self.assertEqual(original, self.rules)
        self.rules["comparison_file"] = str(self.comparison_path)
        self.assertEqual(load_spec(self.path), validate_spec(self.rules))

    def test_old_inline_packs_and_resolved_catalogues_still_work(self):
        resolved = load_spec(self.path)
        self.assertNotIn("comparison_file", resolved)
        self.assertEqual(resolved, validate_spec(resolved))
        self.path.write_text(json.dumps(resolved))
        self.comparison_path.unlink()
        self.assertEqual(resolved, load_spec(self.path))

    def test_pack_with_no_comparison_configuration_uses_existing_defaults(self):
        del self.rules["comparison_file"]
        self.write()
        resolved = load_spec(self.path)
        self.assertNotIn("comparison", resolved)
        self.assertTrue(all("comparison" not in f for f in resolved["files"].values()))

    def test_mixed_inline_and_external_settings_are_rejected_even_when_empty(self):
        for top_level in (True, False):
            with self.subTest(top_level=top_level):
                rules = copy.deepcopy(self.rules)
                target = rules if top_level else rules["files"]["new.modules"]
                target["comparison"] = {}
                with self.assertRaisesRegex(SpecError, "cannot be combined"):
                    validate_spec(rules, base_dir=self.root)

    def test_missing_or_malformed_companion_does_not_fall_back(self):
        self.comparison_path.unlink()
        with self.assertRaisesRegex(SpecError, "Cannot load comparison file"):
            load_spec(self.path)
        self.comparison_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(SpecError, "Duplicate JSON key"):
            load_spec(self.path)
        self.comparison_path.write_text('{')
        with self.assertRaisesRegex(SpecError, "Cannot load comparison file"):
            load_spec(self.path)

    def test_wrong_pack_id_and_unknown_file_ids_are_rejected(self):
        self.profile["rules_id"] = "different-pack"
        self.write()
        with self.assertRaisesRegex(SpecError, "does not match rules pack"):
            load_spec(self.path)
        self.profile["rules_id"] = self.rules["id"]
        self.profile["files"]["new.typo"] = {}
        self.write()
        with self.assertRaisesRegex(SpecError, "Unknown comparison file IDs: new.typo"):
            load_spec(self.path)

    def test_invalid_policies_and_expressions_are_rejected(self):
        original = copy.deepcopy(self.profile)
        mutations = [
            lambda p: p.update(schema_version=2),
            lambda p: p.update(unknown=True),
            lambda p: p["files"]["new.modules"].update(list_variables="CAMERA_MODULES"),
            lambda p: p["files"]["new.modules"].update(include="false"),
            lambda p: p["files"]["new.modules"].update(unknown=True),
            lambda p: p["files"]["new.modules"].update(mode="requirements"),
            lambda p: p["files"]["new.startup"].update(latest_scope="anything"),
            lambda p: p.update(exclude=[{"path_regex": "["}]),
            lambda p: p.update(exclude=[{"basename": "x", "when": {"input": "typo"}}]),
            lambda p: p.update(exclude=[{"basename": "x", "when": {"var": "typo"}}]),
            lambda p: p.update(exclude=[{"basename": "x", "when": {"invalid": True}}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.profile = copy.deepcopy(original)
                mutate(self.profile)
                self.write()
                with self.assertRaises(SpecError):
                    load_spec(self.path)

    def test_conditional_exclusions_use_shared_expression_schema(self):
        self.profile["exclude"] = [{
            "basename": "skip.mk", "reason": "Disabled component",
            "when": {"not": {"eq": [{"input": "component"}, "camera"]}},
        }]
        self.write()
        validate_document(self.profile, "comparison-v1.schema.json")
        self.assertEqual(self.profile["exclude"], load_spec(self.path)["comparison"]["exclude"])

    def test_companion_edits_are_reloaded_and_change_effective_digest(self):
        previous = load_spec(self.path)
        self.profile["files"]["new.startup"]["latest_scope"] = "context"
        self.write()
        self.assertNotEqual(digest(canonical(previous)), digest(canonical(load_spec(self.path))))

    def test_cli_validates_companion_without_perforce(self):
        import contextlib
        import io
        from bt_delta.cli import main

        self.profile["files"]["typo"] = {}
        self.write()
        with patch("bt_delta.cli.P4CLI", side_effect=AssertionError("No P4")), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(1, main(["validate-rules", str(self.path)]))
        self.assertIn("Unknown comparison file IDs", error.getvalue())
