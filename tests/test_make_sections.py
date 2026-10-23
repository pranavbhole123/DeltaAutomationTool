"""Whole comment sections across file types, operations and shipped rule packs."""

import base64
import tempfile
import unittest
from pathlib import Path

from bt_delta.blank import BlankPlanner
from bt_delta.demo import fixture as slsi_fixture
from bt_delta.operations import apply_actions, select_actions, selected_make_keys
from bt_delta.planner import Planner
from bt_delta.spec import load_spec
from bt_delta.transforms import TransformError
from test_qualcomm_rules import fixture as qcom_fixture, OFFLOAD


ROOT = Path(__file__).resolve().parents[1]
HEADINGS = [r"(?i)^\s*#+\s*(?:bluetooth|bt)(?:\b|_)"]
BOARD = """#BT
BOARD_HAVE_BLUETOOTH := true
BOARD_HAVE_BLUETOOTH_QCOM := true
BLUETOOTH_QCOM_ENH_LOG := true
BOARD_HAVE_QCOM_BT_XML_NVM_OVERRIDE := true
include vendor/samsung/interfaces/bluetooth/BluetoothBoardConfigCommon.mk
WLAN_VENDOR = 4

# Choose the WLAN chipset
# broadcom: bcm4329, bcm4330, bcm4334, bcm43341, bcm4335, bcm4339, bcm43454
# atheros: ar6003x, ar6004, ar6005, ar603x
WLAN_CHIP := wcn6750
"""


class SharedSectionTests(unittest.TestCase):
    def test_every_make_copy_operation_uses_same_section_selection(self):
        for op, selector in (
            ("make.ensure", {"assignment_names": ["FALLBACK"]}),
            ("make.values", {"prefix": "FALLBACK"}),
            ("make.packages", {"variable": "MODULES", "names": ["fallback"]}),
            ("make.includes", {"lines": ["include fallback.mk"]}),
            ("make.content", {"assignments": {"assignment_names": ["FALLBACK"]}}),
        ):
            for heading in ("#BT", "#bluetooth", "#bt", "#Bluetooth", "  ## bLuEtOoTh config"):
                with self.subTest(op=op, heading=heading):
                    source = (
                        heading + "\nNEW_FLAG := yes # inline\n\n"
                        "include arbitrary.mk\n$(call configure,unknown)\n"
                        "ifdef FEATURE\nANOTHER_FLAG := $(VALUE)\nelse\nANOTHER_FLAG := no\nendif\n"
                        "# other section\nDONT_COPY := yes\n"
                        "FALLBACK := value\nMODULES += fallback\ninclude fallback.mk\n"
                    )
                    actions = select_actions(op, source, dict(selector, comment_patterns=HEADINGS))
                    current = "# elsewhere\nNEW_FLAG := yes # inline\nKEEP := yes\n"
                    after = apply_actions(current, actions)
                    self.assertIn("include arbitrary.mk", after)
                    self.assertIn("$(call configure,unknown)", after)
                    self.assertIn("ifdef FEATURE\nANOTHER_FLAG := $(VALUE)\nelse\nANOTHER_FLAG := no\nendif", after)
                    self.assertNotIn("DONT_COPY", after)
                    self.assertEqual(after.count("NEW_FLAG"), 1)
                    self.assertIn("KEEP := yes", after)
                    fallback = {"make.packages": "MODULES += fallback", "make.includes": "include fallback.mk"}.get(op, "FALLBACK := value")
                    self.assertIn(fallback, after)
                    self.assertEqual(apply_actions(after, actions), after)

    def test_next_comment_ends_selection_even_inside_condition(self):
        source = "#BT\nifdef ENABLED\nA := yes\n# Other\nB := no\nendif\n# Bluetooth\nC := yes\n"
        actions = select_actions("make.ensure", source, {"comment_patterns": HEADINGS})
        self.assertEqual(apply_actions("", actions), "#BT\nifdef ENABLED\nA := yes\nendif\n# Bluetooth\nC := yes\n")

    def test_shell_conditions_in_feature_files_stay_balanced(self):
        source = '#Bluetooth\nif [[ "$MODE" == "x" ]]; then\nSOME_FLAG=TRUE\nelse\nSOME_FLAG=FALSE\nfi\n# Next\nOMIT=TRUE\n'
        actions = select_actions("make.values", source, {"comment_patterns": HEADINGS})
        after = apply_actions("", actions)
        self.assertEqual(after, source.split("# Next")[0])
        self.assertEqual(apply_actions(after, actions), after)

    def test_no_matching_heading_retains_original_selection_and_guards(self):
        for op, selector in (("make.ensure", {"assignment_names": ["WANTED"]}),
                             ("make.values", {"prefix": "WANTED"})):
            with self.subTest(op=op):
                with self.assertRaises(TransformError):
                    select_actions(op, "ifdef FEATURE\nWANTED := yes\nendif\n", dict(selector, comment_patterns=HEADINGS))
                actions = select_actions(op, "# Other\nUNRELATED=yes\nWANTED := yes\n", dict(selector, comment_patterns=HEADINGS))
                self.assertNotIn("UNRELATED", apply_actions("", actions))

    def test_include_basename_replacement_and_policy_survive_sections(self):
        for policy in ("replace", "review", "preserve"):
            with self.subTest(policy=policy):
                actions = select_actions("make.ensure", "#BT\ninclude new/common.mk\nA := yes\n", {
                    "comment_patterns": HEADINGS, "include_basenames": ["common.mk"]})
                for action in actions:
                    action["on_value_difference"] = policy
                notices = []
                after = apply_actions("include old/common.mk # keep\n", actions, notices.append)
                self.assertEqual(after.count("common.mk"), 1)
                self.assertIn("include new/common.mk" if policy == "replace" else "include old/common.mk # keep", after)
                self.assertIn("A := yes", after)
                self.assertEqual(any(n["review"] for n in notices), policy == "review")

    def test_exclusions_filter_source_without_removing_current_content(self):
        actions = select_actions("make.ensure", "#BT\nALLOW=yes\nDENY=yes\n", {
            "comment_patterns": HEADINGS, "exclude": [{"assignment_names": ["DENY"]}]})
        self.assertNotIn("DENY", apply_actions("", actions))
        self.assertIn("DENY=keep", apply_actions("DENY=keep\n", actions))

    def test_duplicate_scalar_reference_is_not_silently_lost(self):
        with self.assertRaisesRegex(TransformError, "Duplicate selected reference"):
            select_actions("make.ensure", "#BT\nFLAG=yes\nFLAG=no\n", {"comment_patterns": HEADINGS})

    def test_duplicate_current_scalar_does_not_report_false_match(self):
        actions = select_actions("make.ensure", "#BT\nFLAG=yes\n", {"comment_patterns": HEADINGS})
        with self.assertRaisesRegex(TransformError, "Duplicate current assignments"):
            apply_actions("FLAG=yes\nFLAG=no\n", actions)

    def test_ambiguous_include_basename_is_not_silently_dropped(self):
        select = {"comment_patterns": HEADINGS, "include_basenames": ["common.mk"]}
        with self.assertRaisesRegex(TransformError, "Conflicting selected reference includes"):
            select_actions("make.ensure", "#BT\ninclude one/common.mk\ninclude two/common.mk\n", select)
        actions = select_actions("make.ensure", "#BT\ninclude one/common.mk\n", select)
        with self.assertRaisesRegex(TransformError, "Duplicate current includes"):
            apply_actions("include one/common.mk\ninclude two/common.mk\n", actions)

    def test_explicit_operator_keeps_comments_guards_and_additive_statements(self):
        actions = select_actions("make.values", "#BT\nifdef MODE\nA := yes # keep\nB += added\nendif\n", {"comment_patterns": HEADINGS})
        actions[0]["operator"] = "="
        after = apply_actions("", actions)
        self.assertEqual(after, "#BT\nifdef MODE\nA = yes # keep\nB += added\nendif\n")
        self.assertEqual(apply_actions(after, actions), after)

    def test_initializer_and_appends_remain_idempotent(self):
        reference = "#BT\nFLAGS := initial\nFLAGS += extra\n"
        actions = select_actions("make.ensure", reference, {"comment_patterns": HEADINGS})
        after = apply_actions("", actions)
        self.assertEqual(after, reference)
        self.assertEqual(apply_actions(after, actions), after)
        self.assertEqual(apply_actions("FLAGS := old\nFLAGS += extra\n", actions),
                         "FLAGS := initial\nFLAGS += extra\n")
        notices = []
        after = apply_actions("FLAGS += extra\n", actions, notices.append)
        self.assertEqual(after, "FLAGS += extra\n")
        self.assertTrue(any(n["review"] for n in notices))

    def test_current_only_keys_allow_conditional_and_arbitrary_names(self):
        text = "#BT\nif [[ 1 ]]; then\nARBITRARY=$(value)\nfi\n# Other\nUNRELATED=yes\nSEC_PRODUCT_FEATURE_BLUETOOTH_OTHER=yes\n"
        self.assertEqual(selected_make_keys(text, {"comment_patterns": HEADINGS, "prefix": "SEC_PRODUCT_FEATURE_BLUETOOTH_"}),
                         {"ARBITRARY", "SEC_PRODUCT_FEATURE_BLUETOOTH_OTHER"})


class PackSectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def setup_pack(self, name, **kw):
        if name != "slsi":
            self.p4, self.config, self.spec, self.paths, _ = qcom_fixture(self.root, name, **kw)
            return
        self.p4, self.config = slsi_fixture(self.root)
        self.spec = load_spec(ROOT / "checklist/slsi.json")
        self.paths = {}
        for role, branch in (("reference", "BENI"), ("current", "COOSA")):
            for scope, folder in (("system", "m36x_sssi"), ("vendor", "m36x_vendor")):
                for key, basename in (("board", "BoardConfigCommon.mk"), ("features", "SecProductFeature.common")):
                    self.paths[f"{role}.{scope}.{key}"] = next(
                        p for p in self.p4.data if branch in p and folder in p and p.endswith("/" + basename))

    def put(self, key, value):
        self.p4.data[self.paths[key]] = (1, value.encode(), "text")

    def build(self, blank=False):
        return (BlankPlanner if blank else Planner)(self.p4, self.config, self.spec).build()

    def test_reported_board_example_in_every_pack_and_plan_mode(self):
        for pack in ("slsi", "qcom-wcn", "qcom-qca"):
            self.setup_pack(pack)
            scopes = ("vendor",) if pack == "qcom-qca" else ("system", "vendor")
            board = BOARD.replace("wcn6750", "s5e8835").replace("WLAN_VENDOR = 4", "WLAN_VENDOR = 8") if pack == "slsi" else BOARD
            for scope in scopes:
                self.put(f"reference.{scope}.board", board + "# Unrelated\nDO_NOT_COPY := true\n")
                self.put(f"current.{scope}.board", "# elsewhere\nBLUETOOTH_QCOM_ENH_LOG := true\nKEEP := true\n")
            for blank in (False, True):
                with self.subTest(pack=pack, blank=blank):
                    plan = self.build(blank)
                    for scope in scopes:
                        check = next(c for c in plan["checks"] if c["rule"] == scope + ".board")
                        self.assertEqual(check["status"], "change", check)
                        after = base64.b64decode(next(c["after"] for c in plan["changes"] if c["path"] == self.paths[f"current.{scope}.board"])).decode()
                        for statement in board.splitlines()[1:7]:
                            self.assertIn(statement, after)
                        self.assertEqual(after.count("BLUETOOTH_QCOM_ENH_LOG"), 1)
                        # SLSI vendor explicitly selects only BT statements;
                        # system and QCOM keep their existing WLAN selector.
                        if pack != "slsi" or scope == "system":
                            self.assertIn("WLAN_CHIP := " + ("s5e8835" if pack == "slsi" else "wcn6750"), after)
                        self.assertNotIn("DO_NOT_COPY", after)
                    self.assertFalse(self.p4.calls)
            for change in plan["changes"]:
                if change["path"] in [self.paths[f"current.{scope}.board"] for scope in scopes]:
                    self.p4.data[change["path"]] = (2, base64.b64decode(change["after"]), "text")
            repeat = self.build()
            self.assertFalse([c for c in repeat["changes"] if c["path"] in [self.paths[f"current.{scope}.board"] for scope in scopes]])

    def test_feature_files_copy_unknown_flags_and_preserve_current_only_report(self):
        for pack in ("slsi", "qcom-wcn", "qcom-qca"):
            self.setup_pack(pack)
            scopes = ("vendor",) if pack == "qcom-qca" else ("system", "vendor")
            for scope in scopes:
                self.put(f"reference.{scope}.features", "#bluetooth\nARBITRARY_FEATURE=TRUE\nifdef MODE\nNEW_FEATURE=$(VALUE)\nendif\n# Other\nUNRELATED=yes\nSEC_PRODUCT_FEATURE_BLUETOOTH_FALLBACK=TRUE\n")
                self.put(f"current.{scope}.features", "#BT\nCURRENT_ONLY=TRUE\n# Other\nKEEP=yes\n")
            for blank in (False, True):
                with self.subTest(pack=pack, blank=blank):
                    plan = self.build(blank)
                    for scope in scopes:
                        check = next(c for c in plan["checks"] if c["rule"] == scope + ".features")
                        self.assertEqual(check["status"], "review" if pack == "slsi" and not blank else "change", check)
                        if pack == "slsi" and not blank:
                            self.assertIn("Current-only flags preserved for review: CURRENT_ONLY", check["message"])
                        after = base64.b64decode(next(c["after"] for c in plan["changes"] if c["path"] == self.paths[f"current.{scope}.features"])).decode()
                        self.assertIn("ARBITRARY_FEATURE=TRUE", after)
                        self.assertIn("ifdef MODE\nNEW_FEATURE=$(VALUE)\nendif", after)
                        self.assertIn("SEC_PRODUCT_FEATURE_BLUETOOTH_FALLBACK=TRUE", after)
                        self.assertNotIn("UNRELATED", after)
                        self.assertEqual(after.count("ARBITRARY_FEATURE"), 1)

    def test_qca_section_copy_keeps_removals_and_ant_gating_in_both_modes(self):
        for ant in ("disabled", "enabled"):
            self.setup_pack("qcom-qca", ant_support=ant)
            board = BOARD + "#BT\n" + OFFLOAD + 'BOARD_HAVE_ANT_WIRELESS := true\nBOARD_ANT_WIRELESS_DEVICE := "qualcomm-uart"\n'
            legacy = f"include device/samsung/{self.config['model']}_common/Bluetooth/Bluetooth.mk\n"
            packages = ("#BT\nPRODUCT_PACKAGES += arbitrary_firmware AntHalService\n" + legacy +
                        "include vendor/samsung/bluetooth_device.mk\n"
                        "ifeq ($(shell secspfbool SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD), y)\n"
                        "PRODUCT_PACKAGES += libbthost_if vendor.samsung.hardware.bluetooth.a2dp@1.0-impl KeepModule\nendif\n")
            self.put("reference.vendor.board", board)
            self.put("reference.vendor.packages", packages)
            for blank in (False, True):
                with self.subTest(ant=ant, blank=blank):
                    plan = self.build(blank)
                    # Check additions as well as final file bytes: preview must
                    # not propose adding what a later rule requires absent.
                    for kind in ("board", "packages"):
                        check = next(c for c in plan["checks"] if c["rule"] == "vendor." + kind)
                        self.assertEqual(check["status"], "change", check)
                        preview = "\n".join(p["content"] for p in plan["previews"] if p["rule"] == "vendor." + kind)
                        for forbidden in ("TARGET_HAS_SPLIT_A2DP_FEATURE", "libbthost_if", legacy.strip(), "AntHalService", "BOARD_HAVE_ANT_WIRELESS"):
                            self.assertNotIn(forbidden, preview)
                        after = base64.b64decode(next(c["after"] for c in plan["changes"] if c["path"] == self.paths["current.vendor." + kind])).decode()
                        for forbidden in ("TARGET_HAS_SPLIT_A2DP_FEATURE", "libbthost_if", legacy.strip()):
                            self.assertNotIn(forbidden, after)
                        ant_member = "BOARD_HAVE_ANT_WIRELESS" if kind == "board" else "AntHalService"
                        self.assertEqual(ant_member in after, ant == "enabled")
                    preview = next(p["content"] for p in plan["previews"] if p["rule"] == "vendor.packages")
                    self.assertIn("arbitrary_firmware", preview)
                    self.assertIn("KeepModule", preview)
                    self.assertIn("include vendor/samsung/bluetooth_device.mk", preview)


if __name__ == "__main__":
    unittest.main()
