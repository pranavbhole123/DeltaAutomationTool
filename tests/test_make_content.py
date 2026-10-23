import unittest

from bt_delta.operations import select_actions, apply_actions
from bt_delta.transforms import TransformError


class MakeContentTests(unittest.TestCase):
    selectors = {
        "comment_patterns": [r"(?i)^\s*#+\s*(?:bluetooth|bt)\b"],
        "packages": {"variable": "PRODUCT_PACKAGES", "patterns": ["(?i)bluetooth"]},
        "includes": {"patterns": ["(?i)bluetooth"]},
    }
    sample = (
        "#Bluetooth\n"
        "include device/samsung/xcover7pro_common/Bluetooth/Bluetooth.mk\n"
        "PRODUCT_PACKAGES += \\\n    msbtfw12.mbn \\\n    msbtfw12.tlv \\\n    msnv12.bin \\\n    btcoex_cont_config\n"
        "include vendor/samsung/hardware/vendor/bluetooth/bluetooth_device.mk\n"
    )

    def copy(self, reference, current="", select=None, policy="replace"):
        actions = select_actions("make.content", reference, select or self.selectors)
        for action in actions:
            action["on_value_difference"] = policy
        notices = []
        result = apply_actions(current, actions, notices.append)
        self.assertEqual(result, apply_actions(result, actions))
        return result, notices

    def test_arbitrary_members_and_both_includes_until_next_comment(self):
        source = self.sample + "\n# Camera\nPRODUCT_PACKAGES += CameraOnly\ninclude camera.mk\n"
        result, _ = self.copy(source)
        self.assertEqual(result, self.sample)
        self.assertNotIn("CameraOnly", result)
        self.assertNotIn("camera.mk", result)

    def test_heading_variants_and_inline_comments_and_blank_lines(self):
        for heading in ("#BT", "# Bluetooth", "  ## bLuEtOoTh packages"):
            with self.subTest(heading=heading):
                source = heading + "\nPRODUCT_PACKAGES += arbitrary # inline\n\nNEW_FLAG := yes\n# End\nOTHER := no\n"
                result, _ = self.copy(source)
                self.assertIn("arbitrary # inline", result)
                self.assertIn("NEW_FLAG := yes", result)
                self.assertNotIn("OTHER", result)

    def test_includes_and_package_patterns_still_work_without_heading(self):
        source = ("PRODUCT_PACKAGES += Keep BluetoothAgent\n"
                  "include device/example/Bluetooth/Bluetooth.mk\n"
                  "include vendor/example/bluetooth_device.mk\n")
        result, _ = self.copy(source)
        self.assertNotIn("Keep", result)
        self.assertIn("BluetoothAgent", result)
        self.assertEqual(result.count("include "), 2)

    def test_partial_existing_content_anywhere_does_not_get_duplicated(self):
        current = ("# elsewhere\nPRODUCT_PACKAGES += Keep msnv12.bin\n"
                   "include device/samsung/xcover7pro_common/Bluetooth/Bluetooth.mk\n")
        result, _ = self.copy(self.sample, current)
        self.assertTrue(result.startswith(current))
        self.assertEqual(result.count("msnv12.bin"), 1)
        self.assertEqual(result.count("include device/"), 1)
        self.assertIn("msbtfw12.mbn", result)

    def test_include_spacing_is_not_a_change_or_review(self):
        current = "include    vendor/Bluetooth.mk # keep comment\n"
        result, notices = self.copy("include vendor/Bluetooth.mk\n", current)
        self.assertEqual(result, current)
        self.assertFalse(any(n["review"] for n in notices))
        result, notices = self.copy("include vendor/Bluetooth.mk\n", "-include vendor/Bluetooth.mk\n")
        self.assertEqual(result, "-include vendor/Bluetooth.mk\n")
        self.assertTrue(any(n["review"] for n in notices))

    def test_selected_conditions_calls_and_assignments_stay_together(self):
        source = ("ifdef OUTER\n# BT\nifeq ($(MODE),yes)\nENABLE := false\n"
                  "$(call configure-radio,example)\ninclude arbitrary.mk\n"
                  "PRODUCT_PACKAGES += arbitrary\nelse\nPRODUCT_PACKAGES += alternate\nendif\n"
                  "# Camera\nPRODUCT_PACKAGES += CameraOnly\nendif\n")
        result, _ = self.copy(source)
        self.assertNotIn("CameraOnly", result)
        self.assertEqual(result.count("ifdef OUTER"), 1)
        self.assertEqual(result.count("ifeq ($(MODE),yes)"), 1)
        self.assertIn("ENABLE := false\n$(call configure-radio,example)\ninclude arbitrary.mk", result)
        self.assertIn("else\nPRODUCT_PACKAGES += alternate\nendif\nendif", result)

    def test_condition_outside_heading_is_preserved_without_other_members(self):
        source = "ifdef FEATURE\nPRODUCT_PACKAGES += Unrelated\n# BT\nPRODUCT_PACKAGES += arbitrary\nendif\n"
        result, _ = self.copy(source)
        self.assertEqual(result, "ifdef FEATURE\n# BT\nPRODUCT_PACKAGES += arbitrary\nendif\n")

    def test_conditional_include_without_heading_and_branch_chain(self):
        source = ("ifdef FIRST\ninclude camera.mk\nelse ifeq ($(MODE),radio)\n"
                  "include Bluetooth.mk\nendif\n")
        result, _ = self.copy(source)
        self.assertEqual(result, "ifdef FIRST\nelse ifeq ($(MODE),radio)\ninclude Bluetooth.mk\nendif\n")

    def test_all_list_members_added_without_resetting_current_list(self):
        result, _ = self.copy("# BT\nPRODUCT_PACKAGES := arbitrary another\n", "PRODUCT_PACKAGES += Keep\n")
        self.assertIn("PRODUCT_PACKAGES += Keep", result)
        self.assertIn("PRODUCT_PACKAGES += arbitrary another", result)
        self.assertNotIn(":=", result)

    def test_computed_member_is_not_split_and_unrelated_settings_preserved(self):
        source = "# BT\nPRODUCT_PACKAGES += $(call radio-module, one) arbitrary\nMODE := new\n"
        current = "MODE := old\nKEEP := yes\nPRODUCT_PACKAGES += arbitrary\n"
        result, _ = self.copy(source, current)
        self.assertIn("MODE := new\nKEEP := yes", result)
        self.assertIn("PRODUCT_PACKAGES += $(call radio-module, one)\n", result)
        self.assertEqual(result.count("arbitrary"), 1)

    def test_scalar_difference_policy_and_other_guard_are_respected(self):
        source = "#BT\nMODE := new\n"
        for policy in ("preserve", "review"):
            result, notices = self.copy(source, "MODE := old\n", policy=policy)
            self.assertEqual(result, "MODE := old\n")
            self.assertEqual(any(n["review"] for n in notices), policy == "review")
        result, notices = self.copy("#BT\ninclude arbitrary.mk\n", "ifdef OTHER\ninclude arbitrary.mk\nendif\n")
        self.assertEqual(result.count("include arbitrary.mk"), 1)
        self.assertTrue(any(n["review"] for n in notices))

    def test_later_list_reset_is_reported_without_duplicate_addition(self):
        current = "PRODUCT_PACKAGES += arbitrary\nPRODUCT_PACKAGES := Keep\n"
        result, notices = self.copy("#BT\nPRODUCT_PACKAGES += arbitrary\n", current)
        self.assertEqual(result, current)
        self.assertTrue(any(n["review"] and "reset" in n["message"] for n in notices))

    def test_crlf_and_malformed_conditions(self):
        result, _ = self.copy(self.sample, "# current\r\n")
        self.assertNotIn("\n", result.replace("\r\n", ""))
        with self.assertRaises(TransformError):
            self.copy("#BT\nifdef FEATURE\nPRODUCT_PACKAGES += arbitrary\n")

    def test_conditional_resets_are_not_silently_changed_to_additions(self):
        for reference in ("#BT\nifdef FLAG\nPRODUCT_PACKAGES := arbitrary\nendif\n",
                          "ifdef FLAG\nPRODUCT_PACKAGES := Other BluetoothAgent\nendif\n"):
            with self.subTest(reference=reference), self.assertRaisesRegex(TransformError, "must use"):
                self.copy(reference)

    def test_duplicate_members_in_source_are_only_added_once(self):
        source = "# BT\nPRODUCT_PACKAGES += arbitrary arbitrary\nPRODUCT_PACKAGES += arbitrary\n"
        result, _ = self.copy(source)
        self.assertEqual(result.count("arbitrary"), 1)

    def test_both_reference_branches_survive_without_duplicate_guards(self):
        source = "#BT\nifdef MODE\nPRODUCT_PACKAGES += arbitrary\nelse\nPRODUCT_PACKAGES += arbitrary\nendif\n"
        result, _ = self.copy(source)
        self.assertEqual(result, source)

    def test_json_configurable_for_non_bluetooth_content(self):
        source = "# Camera\nCAMERA_MODULES += arbitrary-driver\ninclude camera.mk\n# Audio\nOTHER=yes\n"
        result, _ = self.copy(source, select={"comment_patterns": ["^# Camera$"], "packages": {"variable": "CAMERA_MODULES"}})
        self.assertIn("arbitrary-driver", result)
        self.assertIn("include camera.mk", result)
        self.assertNotIn("OTHER", result)


if __name__ == "__main__":
    unittest.main()
