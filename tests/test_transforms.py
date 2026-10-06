"""Behavioral coverage of the pure edits, independent of a Perforce server."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bt_delta.transforms import TransformError, transform


class TransformTests(unittest.TestCase):
    def assert_idempotent(self, source, action):
        result = transform(source, action)
        self.assertEqual(transform(result, action), result)
        return result

    def test_assignments_preserve_comments_unrelated_content_and_crlf(self):
        source = "# FLAG := commented\r\nFLAG := false  # reason\r\nOTHER = unchanged\r\n"
        result = self.assert_idempotent(source, {"type": "assignments", "values": {"FLAG": "true", "NEW": "yes"}})
        self.assertEqual(result, "# FLAG := commented\r\nFLAG := true  # reason\r\nOTHER = unchanged\r\nNEW := yes\r\n")
        self.assertNotIn("\n", result.replace("\r\n", ""))

    def test_assignments_accept_shell_conditionals_elsewhere(self):
        source = ('SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD=TRUE\n'
                  'if [[ "$SEC_FACTORY_BUILD" == true ]]; then\n'
                  'SEC_PRODUCT_FEATURE_BIOAUTH_CONFIG_FINGERPRINT_TZ="false"\n'
                  'else\n'
                  'SEC_PRODUCT_FEATURE_BIOAUTH_CONFIG_FINGERPRINT_TZ="sensor"\n'
                  'fi\n')
        action = {"type": "assignments", "operator": "=", "values": {
            "SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_OFFLOAD": "TRUE",
            "SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_DUAL_PLAY": "TRUE"}}
        result = self.assert_idempotent(source, action)
        self.assertIn('if [[ "$SEC_FACTORY_BUILD" == true ]]; then\n', result)
        self.assertIn("else\n", result)
        self.assertIn("fi\n", result)
        self.assertTrue(result.endswith("SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_DUAL_PLAY = TRUE\n"))

    def test_assignment_inside_shell_conditional_is_not_rewritten(self):
        source = "if [ \"$X\" = yes ]; then\nFLAG=false\nelse\nFLAG=true\nfi\n"
        with self.assertRaises(TransformError):
            transform(source, {"type": "assignments", "values": {"FLAG": "new"}})

    def test_assignment_identical_conditional_is_unchanged(self):
        source = "ifdef BOARD\nFLAG := true\nendif\n"
        self.assertEqual(transform(source, {"type": "assignments", "values": {"FLAG": "true"}}), source)

    def test_assignment_conditional_change_is_blocked(self):
        source = "ifeq ($(BOARD),yes)\nFLAG := false\nendif\n"
        with self.assertRaisesRegex(TransformError, "conditional"):
            transform(source, {"type": "assignments", "values": {"FLAG": "true"}})

    def test_assignment_duplicate_active_values_are_blocked(self):
        for source in ("FLAG := true\nFLAG := true\n", "ifdef A\nFLAG := true\nelse\nFLAG := false\nendif\n"):
            with self.subTest(source=source), self.assertRaisesRegex(TransformError, "Duplicate"):
                transform(source, {"type": "assignments", "values": {"FLAG": "true"}})

    def test_assignment_multiline_rewrite_is_blocked(self):
        source = "FLAG := one \\\n    two\n"
        with self.assertRaisesRegex(TransformError, "multiline"):
            transform(source, {"type": "assignments", "values": {"FLAG": "three"}})

    def test_assignment_operator_and_absent_last_newline(self):
        result = self.assert_idempotent("FLAG = false", {"type": "assignments", "operator": "=", "values": {"FLAG": "true"}})
        self.assertEqual(result, "FLAG = true")

    def test_packages_ignore_comments_and_understand_both_layouts(self):
        source = "# PRODUCT_PACKAGES += commented\nPRODUCT_PACKAGES += existing \\\n    multiline\nPRODUCT_PACKAGES += single # commented\nOTHER := keep\n"
        result = self.assert_idempotent(source, {"type": "make_packages", "packages": ["existing", "multiline", "single", "commented", "new"]})
        self.assertEqual(result, source + "PRODUCT_PACKAGES += \\\n    commented \\\n    new\n")

    def test_packages_follow_unconditional_resets(self):
        source = "PRODUCT_PACKAGES += old\nPRODUCT_PACKAGES := newer\n"
        result = self.assert_idempotent(source, {"type": "make_packages", "packages": ["old", "newer"]})
        self.assertEqual(result, source + "PRODUCT_PACKAGES += old\n")

    def test_packages_conditional_only_match_is_blocked(self):
        source = "ifdef BOARD\nPRODUCT_PACKAGES += libfeature\nendif\n"
        with self.assertRaisesRegex(TransformError, "conditionally"):
            transform(source, {"type": "make_packages", "packages": ["libfeature"]})

    def test_packages_unfinished_continuation_or_conditional_is_blocked(self):
        for source in ("PRODUCT_PACKAGES += \\" , "PRODUCT_PACKAGES += \\\n", "ifdef BOARD\nPRODUCT_PACKAGES += foo\n"):
            with self.subTest(source=source), self.assertRaises(TransformError):
                transform(source, {"type": "make_packages", "packages": ["new"]})

    def test_packages_preserve_crlf(self):
        result = self.assert_idempotent("# packages\r\n", {"type": "make_packages", "packages": ["one", "two"]})
        self.assertEqual(result, "# packages\r\nPRODUCT_PACKAGES += \\\r\n    one \\\r\n    two\r\n")

    def test_init_edits_only_requested_event_and_target(self):
        source = "on init\n    chmod 0600 /data/example\n\non post-fs-data\n    chmod 0644 /data/example # keep\n    setprop unrelated value\n\non boot\n    setprop next untouched\n"
        result = self.assert_idempotent(source, {"type": "init_commands", "event": "on post-fs-data", "commands": ["chmod 0660 /data/example", "chown system bluetooth /data/example"]})
        self.assertIn("on init\n    chmod 0600 /data/example\n", result)
        self.assertIn("chmod 0660 /data/example # keep", result)
        self.assertIn("    chown system bluetooth /data/example\non boot", result)
        self.assertIn("setprop unrelated value", result)
        self.assertEqual(result.count("chmod 0660 /data/example"), 1)

    def test_init_does_not_count_command_in_other_event(self):
        source = "on init\n    mkdir /data/example 0700 root root\n"
        result = self.assert_idempotent(source, {"type": "init_commands", "event": "post-fs-data", "commands": ["mkdir /data/example 0770 system bluetooth"]})
        self.assertEqual(result, source + "on post-fs-data\n    mkdir /data/example 0770 system bluetooth\n")

    def test_init_conflicting_command_targets_are_replaced(self):
        source = "on post-fs-data\r\n    mkdir /data/example 0700 root root\r\n    chown root root /data/example\r\n    setprop example.value old\r\n"
        result = self.assert_idempotent(source, {"type": "init_commands", "event": "on post-fs-data", "commands": ["mkdir /data/example 0770 system bluetooth", "chown system bluetooth /data/example", 'setprop example.value "new value"']})
        self.assertEqual(result.count("mkdir "), 1)
        self.assertEqual(result.count("chown "), 1)
        self.assertEqual(result.count("setprop "), 1)
        self.assertNotIn("\n", result.replace("\r\n", ""))

    def test_init_duplicate_command_is_blocked(self):
        source = "on init\n    chmod 0600 /x\n    chmod 0644 /x\n"
        with self.assertRaises(TransformError):
            transform(source, {"type": "init_commands", "event": "on init", "commands": ["chmod 0660 /x"]})

    def test_init_repeated_event_selects_bluetooth_commands(self):
        source = ("on boot\n    setprop unrelated.keep yes\n\n"
                  "on boot\n    # BT: create data/log/bt for snoop log\n"
                  "    mkdir /data/log/bt 0770 bluetooth bluetooth\n"
                  "    chmod 0600 /dev/btpower\n\n"
                  "on property:sys.ready=1\n    setprop after.keep yes\n")
        action = {"type": "init_commands", "event": "on boot", "commands": [
            "mkdir /data/log/bt 0770 bluetooth bluetooth",
            "chmod 0660 /dev/btpower",
            "chown bluetooth system /dev/btpower"]}
        result = self.assert_idempotent(source, action)
        self.assertIn("on boot\n    setprop unrelated.keep yes\n", result)
        self.assertIn("# BT: create data/log/bt for snoop log", result)
        self.assertIn("chmod 0660 /dev/btpower", result)
        self.assertIn("chown bluetooth system /dev/btpower\non property:", result)

    def test_init_repeated_event_scans_past_unindented_command(self):
        source = ("on boot\n    setprop unrelated.keep yes\n\n"
                  "on boot\n    setprop second.keep yes\n"
                  "chown system system /sys/class/power_supply/battery/nozx_ctrl\n"
                  "    # BT: create data/log/bt for snoop log\n"
                  "    mkdir /data/log/bt 0770 bluetooth bluetooth\n"
                  "    chmod 0600 /dev/btpower\n"
                  "on property:sys.ready=1\n    setprop after.keep yes\n")
        action = {"type": "init_commands", "event": "on boot", "commands": [
            "mkdir /data/log/bt 0770 bluetooth bluetooth",
            "chmod 0660 /dev/btpower"]}
        result = self.assert_idempotent(source, action)
        self.assertIn("chown system system /sys/class/power_supply/battery/nozx_ctrl", result)
        self.assertIn("chmod 0660 /dev/btpower", result)

    def test_init_repeated_event_can_use_unique_bluetooth_comment(self):
        source = ("on post-fs-data\n    mkdir /data/unrelated 0755 root root\n\n"
                  "on post-fs-data\n    # Fix bluetooth configuration permissions\n")
        action = {"type": "init_commands", "event": "on post-fs-data",
                  "commands": ["mkdir /data/misc/bluetooth 0770 bluetooth bluetooth"]}
        result = self.assert_idempotent(source, action)
        self.assertIn("# Fix bluetooth configuration permissions\n    mkdir /data/misc/bluetooth", result)

    def test_init_repeated_event_without_bt_evidence_is_blocked(self):
        source = "on boot\n    setprop first yes\non boot\n    setprop second yes\n"
        with self.assertRaisesRegex(TransformError, "remain ambiguous"):
            transform(source, {"type": "init_commands", "event": "on boot",
                               "commands": ["chmod 0660 /dev/btpower"]})

    def test_init_stanza_ends_before_service(self):
        source = "on init\n    setprop first yes\nservice demo /system/bin/demo\n    class core\n"
        result = self.assert_idempotent(source, {"type": "init_commands", "event": "on init", "commands": ["setprop second yes"]})
        self.assertIn("    setprop second yes\nservice demo", result)
        self.assertTrue(result.endswith("    class core\n"))

    def test_xml_preserves_layout_comments_attributes_and_escapes_values(self):
        source = '<?xml version="1.0"?>\r\n<permissions>\r\n    <!-- <Feature>comment</Feature> -->\r\n    <Feature mode="keep">old</Feature>\r\n    <Other>untouched</Other>\r\n</permissions>\r\n'
        result = self.assert_idempotent(source, {"type": "xml_elements", "elements": {"Feature": "A&B <C>", "Added": "true"}})
        self.assertIn('<Feature mode="keep">A&amp;B &lt;C&gt;</Feature>', result)
        self.assertIn("<!-- <Feature>comment</Feature> -->", result)
        self.assertIn("    <Other>untouched</Other>\r\n    <Added>true</Added>\r\n</permissions>", result)
        self.assertEqual(ET.fromstring(result).find("Feature").text, "A&B <C>")
        self.assertNotIn("\n", result.replace("\r\n", ""))

    def test_xml_expands_self_closing_leaf_and_parent(self):
        leaf = self.assert_idempotent('<permissions><Feature flag="yes"/></permissions>', {"type": "xml_elements", "elements": {"Feature": "true"}})
        self.assertEqual(leaf, '<permissions><Feature flag="yes">true</Feature></permissions>')
        parent = self.assert_idempotent("<permissions/>", {"type": "xml_elements", "elements": {"Feature": "true"}})
        self.assertEqual(parent, "<permissions>\n    <Feature>true</Feature>\n</permissions>")

    def test_xml_uses_explicit_parent(self):
        source = "<root>\n  <FeatureSet>\n  </FeatureSet>\n</root>"
        result = self.assert_idempotent(source, {"type": "xml_elements", "closing_parent": "</FeatureSet>", "elements": {"Feature": "yes"}})
        self.assertIsNotNone(ET.fromstring(result).find("FeatureSet/Feature"))

    def test_xml_ambiguity_and_invalid_source_are_blocked(self):
        for source in ("<permissions><Feature>one</Feature><Feature>two</Feature></permissions>", "<permissions><Feature><Child/></Feature></permissions>", "<permissions>"):
            with self.subTest(source=source), self.assertRaises(TransformError):
                transform(source, {"type": "xml_elements", "elements": {"Feature": "true"}})

    def test_replace_block_is_literal_unique_and_idempotent(self):
        result = self.assert_idempotent("before\r\nold\r\nblock\r\nafter\r\n", {"type": "replace_block", "before": "old\nblock", "after": "new\nblock"})
        self.assertEqual(result, "before\r\nnew\r\nblock\r\nafter\r\n")
        with self.assertRaises(TransformError):
            transform("old old", {"type": "replace_block", "before": "old", "after": "new"})
        with self.assertRaises(TransformError):
            transform("unrelated", {"type": "replace_block", "before": "old", "after": "new"})

    def test_ensure_lines_exact_and_idempotent(self):
        result = self.assert_idempotent("keep\r\n# requested\r\n", {"type": "ensure_lines", "lines": ["requested", "requested"]})
        self.assertEqual(result, "keep\r\n# requested\r\nrequested\r\n")

    def test_copy_reference_is_not_a_text_transform(self):
        with self.assertRaisesRegex(TransformError, "planner"):
            transform("text", {"type": "copy_reference"})


if __name__ == "__main__":
    unittest.main()
