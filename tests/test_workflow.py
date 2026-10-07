"""End-to-end checks using a synthetic depot, with no Perforce server access."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from bt_delta.config import ConfigError, parse_details, validate
from bt_delta.demo import fixture, demo_plan
from bt_delta.executor import ApprovalError, ExecutionError, execute, local_bytes
from bt_delta.planner import Planner, preview_summary, save_plan, seal, verify_seal
from bt_delta.resolver import relative_for
from bt_delta.perforce import parse_view
from bt_delta.perforce import PerforceTimeout
from bt_delta.transforms import TransformError, transform


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bt_delta_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.p4, self.config = fixture(self.root / "workspace")

    def build(self):
        plan = Planner(self.p4, self.config).build()
        self.assertEqual([], [c for c in plan["checks"] if c["status"] == "blocked"])
        return plan

    def execute(self, plan, **kwargs):
        return execute(self.p4, plan, plan["digest"], acknowledge_reviews=True,
                       journal_path=self.root / "execution.json", **kwargs)

    def test_user_pasted_input_format(self):
        data = parse_details("C OS:\nSystem Template - CUR_SYS\nVendor template - CUR_VENDOR\nCSC path - //COOSA_CSC/a/\n\nReference 8.5 details\nSystem Template - REF_SYS\nVendor template - REF_VENDOR\nCSC path - //BENI_CSC/a/\nCP template : CP")
        self.assertEqual(data["current"]["system_template"], "CUR_SYS")
        self.assertEqual(data["reference"]["vendor_template"], "REF_VENDOR")
        self.assertNotIn("cp_template", data)
        with self.assertRaises(ConfigError):
            parse_details("System Template - NO_SECTION")

    def test_actual_coosa_beni_mapping_shapes_resolve(self):
        plan = self.build()
        header = next(c for c in plan["changes"] if c["path"].endswith("bdroid_buildcfg.h") and "m36x_sssi" in c["path"])
        self.assertIn("//MODEL/PROD_COOSA/ONEUI_9_0/FLUMEN/Strawberry/EXYNOS/m36x_sssi/device/m36x_common/", header["path"])
        spec = plan["templates"]["current.system"]
        self.assertEqual(relative_for(parse_view(spec), header["path"]), "android/device/samsung/m36x_common/Bluetooth/bdroid_buildcfg.h")

    def test_plan_has_no_writes_and_covers_every_rule(self):
        plan = self.build()
        self.assertFalse(self.p4.calls)
        self.assertFalse((self.root / "workspace").exists())
        self.assertEqual(11, len(plan["changes"]))
        self.assertEqual(17, len({c["rule"] for c in plan["checks"] if c["rule"] != "sheet.review"}))

    def test_empty_file_previews_show_reference_and_checklist_inputs(self):
        plan = self.build()
        board = next(p for p in plan["previews"] if p["rule"] == "system.board")
        self.assertIn("WLAN_VENDOR", board["content"])
        self.assertIn("BluetoothBoardConfigCommon.mk", board["content"])
        self.assertNotIn("Existing model packages", board["content"])
        header = next(p for p in plan["previews"] if p["rule"] == "system.header")
        self.assertIn("Synthetic reference header", header["content"])
        boot = next(p for p in plan["previews"] if p["rule"] == "system.boot")
        self.assertIn("on boot", boot["content"])
        self.assertIn("/dev/btpower", boot["content"])
        rendered = preview_summary(plan)
        self.assertIn("Reference:", rendered)
        folder = self.root / "preview-report"
        save_plan(plan, folder)
        self.assertEqual((folder / "empty-file-previews.txt").read_text(), rendered)

    def test_legacy_cp_input_is_ignored_without_lookup(self):
        self.config["cp_template"] = "NONEXISTENT_CP"
        plan = self.build()
        self.assertNotIn("cp_template", plan["config"])
        self.assertNotIn("cp", plan["templates"])
        self.assertFalse(any(c["rule"] == "cp.context" for c in plan["checks"]))
        self.assertTrue(any(c["rule"] == "vendor.firmware" and c["status"] == "review" for c in plan["checks"]))

    def test_approval_and_review_required_before_any_mutation(self):
        plan = self.build()
        with self.assertRaises(ApprovalError):
            execute(self.p4, plan, "wrong")
        with self.assertRaises(ApprovalError):
            execute(self.p4, plan, plan["digest"])
        self.assertFalse(self.p4.calls)

    def test_tampered_plan_rejected(self):
        plan = self.build()
        plan["changes"][0]["after"] = base64.b64encode(b"unreviewed").decode()
        with self.assertRaises(ValueError):
            self.execute(plan)
        self.assertFalse(self.p4.calls)

    def test_changed_reference_stops_all_writes(self):
        plan = self.build()
        path = next(p for p in self.p4.data if "PROD_BENI" in p and p.endswith("BoardConfigCommon.mk"))
        _, data, type = self.p4.data[path]
        self.p4.data[path] = (2, data, type)
        with self.assertRaisesRegex(ExecutionError, "revision changed"):
            self.execute(plan)
        self.assertFalse(self.p4.calls)

    def test_changed_template_stops_all_writes(self):
        plan = self.build()
        self.p4.specs[self.config["current"]["system_template"]]["View2"] += "BROKEN"
        with self.assertRaisesRegex(ExecutionError, "Template view changed"):
            self.execute(plan)
        self.assertFalse(self.p4.calls)

    def test_dirty_file_blocks_entire_plan(self):
        plan = self.build()
        change = next(c for c in plan["changes"] if c["revision"] is not None)
        local = Path(change["local_path"])
        local.parent.mkdir(parents=True)
        local.write_bytes(b"my unfinished work")
        self.p4.have[change["path"]] = 1
        with self.assertRaisesRegex(ExecutionError, "Local changes"):
            self.execute(plan)
        self.assertEqual(local.read_bytes(), b"my unfinished work")
        self.assertFalse(self.p4.calls)

    def test_already_opened_file_blocks_entire_plan(self):
        plan = self.build()
        self.p4.opened[plan["changes"][0]["path"]] = "edit"
        with self.assertRaisesRegex(ExecutionError, "already open"):
            self.execute(plan)
        self.assertFalse(self.p4.calls)

    def test_approved_apply_creates_only_pending_exact_files(self):
        plan = self.build()
        result = self.execute(plan)
        self.assertEqual(result["status"], "applied_pending_review")
        self.assertEqual(len(result["files"]), len(plan["changes"]))
        for change in plan["changes"]:
            self.assertEqual(Path(change["local_path"]).read_bytes(), base64.b64decode(change["after"]))
        self.assertEqual([c[0] for c in self.p4.calls].count("change"), 1)
        self.assertFalse(any(c[0] in ("submit", "revert", "client") for c in self.p4.calls))

    def test_failed_apply_journals_partial_work(self):
        plan = self.build()
        original_edit = self.p4.edit
        def fail(path, cl):
            raise RuntimeError("Synthetic edit failure")
        self.p4.edit = fail
        with self.assertRaisesRegex(ExecutionError, "Pending CL: 12345"):
            self.execute(plan)
        journal = json.loads((self.root / "execution.json").read_text())
        self.assertEqual(journal["status"], "failed_partial")
        self.assertTrue(journal["files"])
        self.assertFalse(any(c[0] == "revert" for c in self.p4.calls))

    def test_blocked_plan_requires_acknowledgement_then_applies_other_changes(self):
        self.config["hcf_variant"] = "unknown_variant"
        self.config["products"] = []
        plan = Planner(self.p4, self.config).build()
        self.assertTrue(any(c["status"] == "blocked" and c["rule"] == "vendor.hcf" for c in plan["checks"]))
        with self.assertRaises(ApprovalError):
            self.execute(plan)
        self.assertFalse(self.p4.calls)
        result = execute(self.p4, plan, plan["digest"], acknowledge_reviews=True, acknowledge_blocked=True,
                         journal_path=self.root / "partial-execution.json")
        self.assertEqual(result["status"], "applied_pending_review")
        self.assertTrue(any(item["rule"] == "vendor.hcf" for item in result["blocked"]))
        self.assertTrue(result["files"])
        self.assertFalse(any(call[0] in ("submit", "shelve", "revert") for call in self.p4.calls))

    def test_hcf_variant_and_products_are_inferred_from_bluetooth_makefile(self):
        self.config["hcf_variant"] = ""
        self.config["products"] = []
        plan = self.build()
        self.assertEqual(plan["config"]["hcf_variant"], "m36xxx")
        self.assertEqual(plan["config"]["products"], ["m36xxx"])
        check = next(c for c in plan["checks"] if c["rule"] == "vendor.hcf")
        self.assertEqual(check["status"], "pass")
        self.assertIn("Folder m36xxx", check["message"])
        preview = next(p for p in plan["previews"] if p["rule"] == "vendor.hcf")
        self.assertIn("$(HCF_PATH)/m36xxx", preview["content"])
        self.assertIn("bt.hcf", preview["content"])

    def test_hidl_check_accepts_newer_version(self):
        manifest = next(path for path in self.p4.data
                        if "COOSA" in path and path.endswith("/manifest.xml"))
        revision, content, file_type = self.p4.data[manifest]
        self.p4.data[manifest] = (revision, content.replace(b"<version>1.0</version>", b"<version>1.1</version>", 1), file_type)
        plan = self.build()
        check = next(c for c in plan["checks"] if c["rule"] == "vendor.hals")
        self.assertEqual(check["status"], "pass")
        self.assertIn("minimum versions", check["message"])

    def test_hidl_check_rejects_version_below_checklist_minimum(self):
        manifest = next(path for path in self.p4.data
                        if "COOSA" in path and path.endswith("/manifest.xml"))
        revision, content, file_type = self.p4.data[manifest]
        self.p4.data[manifest] = (revision, content.replace(b"<version>1.0</version>", b"<version>0.9</version>", 1), file_type)
        plan = Planner(self.p4, self.config).build()
        check = next(c for c in plan["checks"] if c["rule"] == "vendor.hals")
        self.assertEqual(check["status"], "blocked")

    def test_hidl_check_discovers_additional_reference_bluetooth_hal(self):
        extra = (b'<hal format="hidl"><name>vendor.demo.hardware.a2dp</name><transport>hwbinder</transport>'
                 b'<version>1.0</version><interface><name>IA2dp</name><instance>default</instance>'
                 b'</interface></hal>')
        manifests = [path for path in self.p4.data if path.endswith("/manifest.xml")]
        for path in manifests:
            revision, content, file_type = self.p4.data[path]
            self.p4.data[path] = (revision, content.replace(b"</manifest>", extra + b"</manifest>"), file_type)
        plan = self.build()
        check = next(c for c in plan["checks"] if c["rule"] == "vendor.hals")
        self.assertEqual(check["status"], "pass")
        self.assertIn("1 additional", check["message"])

    def test_missing_current_hcf_filter_block_is_copied_from_reference(self):
        current = next(path for path in self.p4.data
                       if "COOSA" in path and path.endswith("/s5e8835/bluetooth.mk"))
        rev, _, file_type = self.p4.data[current]
        self.p4.data[current] = (rev, b"BT_CHIPSET=s5e8835\nHCF_PATH=$(BASE_PATH)/$(BT_CHIPSET)\n", file_type)
        self.config["hcf_variant"] = ""
        self.config["products"] = []
        plan = self.build()
        check = next(c for c in plan["checks"] if c["rule"] == "vendor.hcf")
        self.assertEqual(check["status"], "change")
        change = next(c for c in plan["changes"] if c["path"] == current)
        after = base64.b64decode(change["after"]).decode()
        self.assertIn("ifneq ($(filter m36xxx, $(TARGET_PRODUCT)),)", after)
        self.assertIn("$(HCF_PATH)/m36xxx", after)

    def test_reference_discovers_additional_bluetooth_package_and_include(self):
        reference = next(path for path in self.p4.data
                         if "BENI" in path and "m36x_vendor" in path and path.endswith("device_common.mk"))
        revision, content, file_type = self.p4.data[reference]
        self.p4.data[reference] = (revision, content +
                                   b"PRODUCT_PACKAGES += FutureBluetoothService\n"
                                   b"include vendor/future/bluetooth/future_device.mk\n", file_type)
        plan = self.build()
        change = next(c for c in plan["changes"] if c["rules"][0] == "vendor.packages")
        after = base64.b64decode(change["after"])
        self.assertIn(b"FutureBluetoothService", after)
        self.assertIn(b"include vendor/future/bluetooth/future_device.mk", after)
        preview = next(p for p in plan["previews"] if p["rule"] == "vendor.packages")
        self.assertEqual(preview["reference_path"], reference)

    def test_reference_comment_anchor_discovers_changed_bluetooth_tty_path(self):
        reference = next(path for path in self.p4.data
                         if "BENI/SYSTEM" in path and path.endswith("system/core/rootdir/init.rc"))
        revision, content, file_type = self.p4.data[reference]
        self.p4.data[reference] = (revision, content +
                                   b"\n# Bluetooth alternate tty port\n"
                                   b"    chown bluetooth bluetooth /dev/ttySAC9\n"
                                   b"    chmod 0660 /dev/ttySAC9\n", file_type)
        plan = self.build()
        boot_change = next(c for c in plan["changes"] if "system.boot" in c["rules"])
        after = base64.b64decode(boot_change["after"])
        self.assertIn(b"/dev/ttySAC1", after)
        self.assertIn(b"/dev/ttySAC9", after)

    def test_reference_discovers_additional_bluetooth_feature_name(self):
        reference = next(path for path in self.p4.data
                         if "BENI" in path and "m36x_sssi" in path and path.endswith("SecProductFeature.common"))
        revision, content, file_type = self.p4.data[reference]
        self.p4.data[reference] = (revision, content + b"SEC_PRODUCT_FEATURE_A2DP_FUTURE=TRUE\n", file_type)
        plan = self.build()
        change = next(c for c in plan["changes"] if "system.features" in c["rules"])
        self.assertIn(b"SEC_PRODUCT_FEATURE_A2DP_FUTURE = TRUE", base64.b64decode(change["after"]))

    def test_jdm_uses_efs_in_both_init_files(self):
        self.config["jdm"] = True
        plan = self.build()
        contents = [base64.b64decode(c["after"]).decode() for c in plan["changes"] if c["path"].endswith(".rc")]
        self.assertTrue(any("/efs/bluetooth/bt_addr" in x for x in contents))
        self.assertFalse(any("/mnt/vendor/efs" in x for x in contents))

    def test_region_merge_adds_missing_keys_and_preserves_existing_values(self):
        current = self.config["current"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        reference = self.config["reference"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        self.p4.data[current] = (1, b'{\r\n"CarrierFeature_BT_EnableSAP":"TRUE","Other":"keep","CurrentOnly":1,"nested":{"Keep":false}\r\n}', "text")
        content = b'{"CarrierFeature_BT_EnableSAP":"FALSE","Other":"different","Missing":true,"nested":{"Keep":true,"New":0},"NewSection":{"Feature":1}}'
        self.p4.data[reference] = (1, content, "text")
        plan = self.build()
        change = next(c for c in plan["changes"] if c["path"] == current)
        after = base64.b64decode(change["after"])
        self.assertEqual(json.loads(after), {"CarrierFeature_BT_EnableSAP":"TRUE", "Other":"keep",
                         "CurrentOnly":1, "Missing":True, "nested":{"Keep":False,"New":0},
                         "NewSection":{"Feature":1}})
        self.assertIn(b'\r\n', after)

    def test_existing_carrier_values_are_skipped_without_reformatting(self):
        current = self.config["current"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        reference = self.config["reference"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        self.p4.data[current] = (1, b'{"Feature":false, "Extra":1}', "text")
        self.p4.data[reference] = (1, b'{"Feature":true}', "text")
        plan = self.build()
        self.assertFalse(any(change["path"] == current for change in plan["changes"]))
        self.assertTrue(any(check["rule"] == "csc.features" and check["status"] == "pass" for check in plan["checks"]))

    def test_reference_only_region_is_added_without_replacing_another_region(self):
        ref = self.config["reference"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        data = self.p4.data.pop(ref)
        self.p4.data[ref.replace("/INS/", "/XSG/")] = data
        plan = self.build()
        changes = [c for c in plan["changes"] if "CSC" in c["path"]]
        self.assertEqual(len(changes), 1)
        self.assertIn("/XSG/system/", changes[0]["path"])
        self.assertIsNone(changes[0]["revision"])
        self.assertEqual(base64.b64decode(changes[0]["after"]), data[1])
        reviews = [c for c in plan["checks"] if c["rule"] == "csc.features" and c["status"] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertIn("/INS/system/", reviews[0]["paths"][0])

    def test_existing_carrier_arrays_are_preserved_without_positional_merging(self):
        current = self.config["current"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        reference = self.config["reference"]["csc_path"] + "/INS/system/customer_carrier_feature_plan.json"
        self.p4.data[current] = (1, b'{"carriers":[{"name":"A"},{"name":"B"}]}', "text")
        content = b'{"carriers":[{"name":"B","Other":true},{"name":"A","CarrierFeature_BT_EnableSAP":"TRUE"}],"NewArray":[1,2]}'
        self.p4.data[reference] = (1, content, "text")
        plan = self.build()
        self.assertFalse(any(c["rule"] == "csc.features" and c["status"] == "blocked" for c in plan["checks"]))
        change = next(c for c in plan["changes"] if c["path"] == current)
        self.assertEqual(json.loads(base64.b64decode(change["after"])),
                         {"carriers":[{"name":"A"},{"name":"B"}],"NewArray":[1,2]})

    def test_carrier_discovery_ignores_other_filenames_in_normal_and_blank_plans(self):
        from bt_delta.blank import BlankPlanner
        root = "//BENI_CSC/m36x/OTHER/REGION/system/"
        ignored = ("customer_carrier_feature.json", "customer_carrier_feature_plain.json",
                   "custom_carrier_feature_plan.json", "customer_carrier_feature_plan.josn",
                   "CUSTOMER_CARRIER_FEATURE_PLAN.JSON", "carrier_settings.json",
                   "customer_carrier_feature_plan.json.bak", "notes.json")
        for filename in ignored:
            # Invalid content catches accidental discovery and attempted parsing.
            self.p4.data[root + filename] = (1, b"\x00\xffnot JSON", "binary")
        for plan in (self.build(), BlankPlanner(self.p4, self.config).build()):
            self.assertFalse(any(check["status"] == "blocked" for check in plan["checks"]))
            paths = [item["path"] for item in plan["changes"] if "csc.features" in item["rules"]]
            self.assertTrue(all(path.endswith("/customer_carrier_feature_plan.json") for path in paths))
            self.assertFalse(any(filename in path for path in paths for filename in ignored))
            self.assertFalse(any(snapshot["path"] == root + filename for snapshot in plan["snapshots"] for filename in ignored))

    def test_missing_reference_plan_reports_all_files_without_reading_binary(self):
        from bt_delta.blank import BlankPlanner
        self.config["reference"]["csc_path"] = "//BENI_CSC/Strawberry/EXYNOS/m36x"
        self.config["current"]["csc_path"] = "//COOSA_CSC/Strawberry/EXYNOS/m36x"
        relative = "OMC/ODM/INS/system/customer_carrier_feature_plan.json"
        src = self.config["reference"]["csc_path"] + "/" + relative
        dst = self.config["current"]["csc_path"] + "/" + relative
        self.p4.data[src] = (7, b'{"Existing":false,"Missing":true}', "text")
        self.p4.data[dst] = (4, b'{"Existing":true,"CurrentOnly":1}', "text")
        skipped_directory = self.config["reference"]["csc_path"] + "/OTHER/XSG/system"
        names = ["customer_carrier_feature.json", "customer_carrier_feature_plain.json", "custom_carrier_feature_plan.json"]
        names += [f"file_{index}.bin" for index in range(12)]
        ignored_paths = [skipped_directory + "/" + name for name in names]
        for path in ignored_paths:
            self.p4.data[path] = (1, b'\x00\xffnot JSON', 'binary')
        original_read = self.p4.read_file
        def read_exact_only(path, revision=None):
            self.assertNotIn(path, ignored_paths, "Skipped files must not even be read")
            return original_read(path, revision)
        self.p4.read_file = read_exact_only
        plan = self.build()
        changes = {change["path"]: change for change in plan["changes"]}
        self.assertEqual(json.loads(base64.b64decode(changes[dst]["after"])),
                         {"Existing":True,"CurrentOnly":1,"Missing":True})
        self.assertEqual([path for path in changes if "CSC" in path], [dst])
        blank = BlankPlanner(self.p4, self.config).build()
        self.assertFalse(any(check["status"] == "blocked" for check in blank["checks"]))
        self.assertEqual({change["path"] for change in blank["changes"] if "csc.features" in change["rules"]},
                         {dst})
        for result in (plan, blank):
            skipped = next(check for check in result["checks"] if check["rule"] == "csc.features"
                           and check["status"] == "skipped" and skipped_directory in check["paths"])
            self.assertEqual(set(skipped["paths"]), {skipped_directory, *ignored_paths})
            self.assertIn("No customer_carrier_feature_plan.json", skipped["message"])
            for name in names:
                self.assertIn(name + " (binary)", skipped["message"])
        self.execute(plan)
        self.assertTrue(Path(self.p4.where(dst)).exists())
        target_skipped = skipped_directory.replace("BENI_CSC", "COOSA_CSC")
        self.assertFalse(Path(self.p4.where(target_skipped)).exists())

    def test_empty_carrier_discovery_reports_path_and_filter_failures_separately(self):
        from bt_delta.blank import BlankPlanner
        reference_root = "//BENI_CSC/m36x"
        for path in list(self.p4.data):
            if path.startswith(reference_root + "/"):
                del self.p4.data[path]
        for planner in (Planner, BlankPlanner):
            plan = planner(self.p4, self.config).build()
            check = next(check for check in plan["checks"] if check["rule"] == "csc.features" and check["status"] == "review")
            self.assertIn("Perforce returned no files for " + reference_root + "/...", check["message"])
            self.assertIn("casing", check["message"])
        path = reference_root + "/OMC/ODM/INS/system/carrier_settings.json"
        self.p4.data[path] = (1, b'{}', "text")
        for planner in (Planner, BlankPlanner):
            plan = planner(self.p4, self.config).build()
            check = next(check for check in plan["checks"] if check["rule"] == "csc.features" and check["status"] == "review")
            self.assertIn("returned 1 files", check["message"])
            self.assertIn("none matched the exact filename customer_carrier_feature_plan.json", check["message"])
            skipped = next(check for check in plan["checks"] if check["rule"] == "csc.features" and check["status"] == "skipped")
            self.assertIn(path, skipped["paths"])

    def test_kyc_binary_is_found_and_reported_in_live_log_and_saved_report(self):
        from bt_delta.blank import BlankPlanner
        from bt_delta.planner import summary
        reference_root = "//Beni_csc/strawberry/exynos/m36x"
        self.config["reference"]["csc_path"] = reference_root
        self.config["current"]["csc_path"] = "//Coosa_csc/strawberry/exynos/m36x"
        path = reference_root + "/omc/kyc/kyc/system/customer_carrier_feature.json"
        self.p4.data[path] = (1, b'\x00\xffbinary', 'binary')
        messages = []
        self.p4.progress = messages.append
        original_read = self.p4.read_file
        def never_read_binary(file, revision=None):
            self.assertNotEqual(file, path, "The binary must only be discovered, never read")
            return original_read(file, revision)
        self.p4.read_file = never_read_binary
        for planner in (Planner, BlankPlanner):
            messages.clear()
            plan = planner(self.p4, self.config).build()
            self.assertFalse(any(check["status"] == "blocked" for check in plan["checks"]))
            self.assertFalse(any("csc.features" in item["rules"] for item in plan["changes"]))
            self.assertTrue(any(f"CSC file found and skipped: {path} (binary)" in message for message in messages))
            rendered = summary(plan)
            self.assertIn(path, rendered)
            self.assertIn("customer_carrier_feature.json (binary)", rendered)
            self.assertIn("No customer_carrier_feature_plan.json", rendered)

    def test_every_collection_and_region_uses_the_same_relative_feature_path(self):
        current_root = "//COOSA_CSC/Strawberry/EXYNOS/m36x"
        reference_root = "//BENI_CSC/Strawberry/EXYNOS/m36x"
        # Old pasted inputs may end at different regions; discovery must still
        # start at the model and use the source's full model-relative path.
        self.config["current"]["csc_path"] = current_root + "/OMC/ODM/INS"
        self.config["reference"]["csc_path"] = reference_root + "/OMC/OXM/INS"
        files = {"OMC/OXM/INS/system/customer_carrier_feature_plan.json": b'{"AllFeatures": "INS"}',
                 "OMC/OXM/XSG/system/customer_carrier_feature_plan.json": b'{"AllFeatures": "XSG"}',
                 "OTHER/COLLECTION/ATT/custom/customer_carrier_feature_plan.json": b'{"AllFeatures": "ATT"}',
                 "anything/region/customer_carrier_feature_plan.json": b'{"AllFeatures": "CUSTOM"}',
                 "customer_carrier_feature_plan.json": b'{"RootFeature": true}'}
        for relative, content in files.items():
            self.p4.data[reference_root + "/" + relative] = (1, content, "text")
        # Non-feature files in those region folders must not be copied.
        self.p4.data[reference_root + "/OXM/INS/system/notes.json"] = (1, b'{"notes":true}', "text")
        self.p4.data[reference_root + "/unrelated/carrier_folder/notes.json"] = (1, b'{}', "text")
        self.p4.data[reference_root.replace("m36x", "m35x") + "/OMC/OXM/INS/system/customer_carrier_feature_plan.json"] = (1, b'{}', "text")
        plan = self.build()
        changes = {c["path"]: c for c in plan["changes"]}
        for relative, content in files.items():
            target = current_root + "/" + relative
            self.assertIn(target, changes)
            self.assertEqual(base64.b64decode(changes[target]["after"]), content)
        self.assertFalse(any(path.endswith("notes.json") for path in changes))
        self.assertFalse(any("m35x" in path for path in changes))
        result = execute(self.p4, plan, plan["digest"], acknowledge_reviews=True,
                         journal_path=self.root / "regional-execution.json")
        self.assertEqual(result["status"], "applied_pending_review")
        for relative, content in files.items():
            target = current_root + "/" + relative
            self.assertEqual(Path(self.p4.where(target)).read_bytes(), content)

    def test_ambiguous_current_file_requires_override(self):
        old = next(p for p in self.p4.data if "PROD_COOSA" in p and "m36x_sssi" in p and p.endswith("BoardConfigCommon.mk"))
        new = old.replace("PROD_COOSA", "SECOND_COOSA")
        self.p4.data[new] = self.p4.data[old]
        spec = self.p4.specs[self.config["current"]["system_template"]]
        spec["View5"] = new + " //" + spec["Client"] + "/alternate/BoardConfigCommon.mk"
        plan = Planner(self.p4, self.config).build()
        self.assertTrue(any(c["rule"] == "system.board" and c["status"] == "blocked" for c in plan["checks"]))
        self.config["paths"]["current.system.board_config"] = old
        self.build()

    def test_different_current_branch_does_not_use_sample_names(self):
        for path in list(self.p4.data):
            if "COOSA" in path:
                self.p4.data[path.replace("COOSA", "UNRELATED_FUTURE_BRANCH")] = self.p4.data.pop(path)
        for spec in self.p4.specs.values():
            for key in list(spec):
                if key.startswith("View"):
                    spec[key] = spec[key].replace("COOSA", "UNRELATED_FUTURE_BRANCH")
        self.config["current"]["csc_path"] = self.config["current"]["csc_path"].replace("COOSA", "UNRELATED_FUTURE_BRANCH")
        plan = self.build()
        self.assertTrue(all("UNRELATED_FUTURE_BRANCH" in c["path"] for c in plan["changes"]))

    def test_idempotent_after_submitted_equivalent_content(self):
        first = self.build()
        for change in first["changes"]:
            self.p4.data[change["path"]] = (2, base64.b64decode(change["after"]), change["type"])
        second = self.build()
        self.assertEqual(second["changes"], [])

    def test_demo_plan_cannot_be_applied(self):
        plan = demo_plan(self.root)
        with self.assertRaises(ApprovalError):
            self.execute(plan)
        self.assertFalse(self.p4.calls)

    def test_local_line_endings_and_binary_bytes(self):
        self.assertEqual(local_bytes(b"a\nb\n", "text", "win"), b"a\r\nb\r\n")
        self.assertEqual(local_bytes(b"\x00\r\n", "binary", "unix"), b"\x00\r\n")

    def test_conditional_include_is_not_treated_as_global(self):
        with self.assertRaises(TransformError):
            transform("ifeq ($(X),y)\ninclude bt.mk\nendif\n", {"type": "ensure_lines", "lines": ["include bt.mk"]})

    def test_discovery_does_not_scan_all_board_filenames(self):
        queries = []
        original = self.p4.files
        def track(pattern):
            queries.append(pattern)
            return original(pattern)
        self.p4.files = track
        self.build()
        self.assertFalse(any(q.endswith('/.../BoardConfigCommon.mk') for q in queries))
        self.assertTrue(any(q.endswith('/device/m36x_common/BoardConfigCommon.mk') for q in queries))
        board_queries = [q for q in queries if q.endswith('BoardConfigCommon.mk')]
        self.assertTrue(board_queries)
        self.assertTrue(all('/EXYNOS/' in q and '/device/m36x_common/' in q and '...' not in q for q in board_queries))
        self.assertFalse(any('/SYSTEM/' in q or '/Cinnamon/' in q for q in board_queries))

    def test_board_lookup_queries_only_selected_common_mapping(self):
        from bt_delta.resolver import Resolver
        resolver = Resolver(self.p4, self.config)
        queries = []
        original = self.p4.files
        def track(pattern):
            queries.append(pattern)
            return original(pattern)
        self.p4.files = track
        path = resolver.discover('reference', 'system', 'board_config')
        self.assertEqual(queries, [path])
        self.assertTrue(path.startswith('//MODEL/PROD_BENI/'))

    def test_header_lookup_uses_only_exact_model_common_path(self):
        from bt_delta.resolver import Resolver
        resolver = Resolver(self.p4, self.config)
        queries = []
        original = self.p4.files
        self.p4.files = lambda path: queries.append(path) or original(path)
        path = resolver.discover('reference', 'system', 'bluetooth_header', optional=True)
        self.assertEqual(queries, [path])
        self.assertIn('/EXYNOS/m36x_sssi/device/m36x_common/Bluetooth/bdroid_buildcfg.h', path)

    def test_editable_header_path_with_model_placeholder_copies_new_location(self):
        from bt_delta.catalog import default_catalog
        catalog = default_catalog()
        catalog['model_common_files']['bluetooth_header'] = ['Bluetooth/bdroid_buildcfg.h', 'BT/{model}_buildcfg.h']
        old = next(p for p in self.p4.data if 'PROD_BENI' in p and 'm36x_sssi' in p and p.endswith('bdroid_buildcfg.h'))
        new = old.replace('Bluetooth/bdroid_buildcfg.h', 'BT/m36x_buildcfg.h')
        self.p4.data[new] = self.p4.data.pop(old)
        plan = Planner(self.p4, self.config, catalog).build()
        self.assertFalse([c for c in plan['checks'] if c['status'] == 'blocked'])
        self.assertTrue(any(c['path'].endswith('/BT/m36x_buildcfg.h') and c['revision'] is None for c in plan['changes']))

    def test_absent_reference_header_is_skipped(self):
        old = next(p for p in self.p4.data if 'PROD_BENI' in p and 'm36x_sssi' in p and p.endswith('bdroid_buildcfg.h'))
        del self.p4.data[old]
        plan = self.build()
        self.assertTrue(any(c['rule'] == 'system.header' and c['status'] == 'skipped' for c in plan['checks']))

    def test_missing_common_view_does_not_trigger_other_board_searches(self):
        from bt_delta.resolver import Resolver
        from bt_delta.perforce import MappingError
        spec = self.p4.specs[self.config['reference']['system_template']]
        for key in list(spec):
            if key.startswith('View') and '/device/m36x_common/' in spec[key]:
                del spec[key]
        resolver = Resolver(self.p4, self.config)
        queries = []
        self.p4.files = lambda pattern: queries.append(pattern) or []
        with self.assertRaisesRegex(MappingError, 'no configured anchor route'):
            resolver.discover('reference', 'system', 'board_config')
        self.assertEqual(queries, [])

    def test_timeout_stops_remaining_rules(self):
        original = self.p4.files
        count = []
        def timeout(pattern):
            if 'bdroid_buildcfg.h' in pattern:
                count.append(pattern)
                raise PerforceTimeout('Synthetic stalled request')
            return original(pattern)
        self.p4.files = timeout
        plan = Planner(self.p4, self.config).build()
        self.assertEqual(1, len(count))
        self.assertTrue(any(c['rule'] == 'planning.stopped' for c in plan['checks']))
        self.assertFalse(any(c['rule'] == 'vendor.packages' for c in plan['checks']))


if __name__ == "__main__":
    unittest.main()
