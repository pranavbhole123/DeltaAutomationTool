"""Review choices preserve unchecked content and apply exactly the reviewed subset."""

import base64
import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from bt_delta.demo import fixture
from bt_delta.executor import ApprovalError, ExecutionError, execute
from bt_delta.planner import Planner, digest, seal, summary, verify_seal
from bt_delta.selection import ReviewSelection


def plan_for_bytes(before, after, format="make", file_type="text", revision=1):
    path = "//depot/settings"
    change = {
        "path": path, "local_path": "/workspace/settings", "revision": revision,
        "type": file_type, "before": base64.b64encode(before).decode(),
        "before_sha256": digest(before) if revision else None,
        "after": base64.b64encode(after).decode(), "after_sha256": digest(after),
        "diff": "", "rules": ["settings"], "sources": ["Test!A1"],
    }
    return seal({"mode": "live", "changes": [change], "requirements": [{"target_path": path, "format": format}]})


def selected_bytes(selection, ids):
    plan = selection.plan_for({"//depot/settings": set(ids)})
    verify_seal(plan)
    if not plan["changes"]:
        return base64.b64decode(selection.original["changes"][0]["before"])
    return base64.b64decode(plan["changes"][0]["after"])


class SelectionTests(unittest.TestCase):
    def test_adjacent_flags_individually_selectable_with_added_flag(self):
        before = b"KEEP=original\nFLAG_A=old\nFLAG_B=old\n"
        after = b"KEEP=original\nFLAG_A=new\nFLAG_B=new\nFLAG_C=added\n"
        selection = ReviewSelection(plan_for_bytes(before, after))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(3, len(choices))
        self.assertEqual(b"KEEP=original\nFLAG_A=new\nFLAG_B=old\nFLAG_C=added\n", selected_bytes(selection, {choices[0].id, choices[2].id}))
        self.assertEqual(before, selected_bytes(selection, set()))
        self.assertEqual(after, selected_bytes(selection, {c.id for c in choices}))

    def test_insertions_removals_replacements_and_all_subsets_preserve_other_flags(self):
        before = b"A=old\nB=remove\nKEEP=yes\n"
        after = b"A=new\nKEEP=yes\nC=add\nD=add\n"
        selection = ReviewSelection(plan_for_bytes(before, after))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(4, len(choices))
        for enabled in itertools.product([False, True], repeat=4):
            selected = {c.id for c, yes in zip(choices, enabled) if yes}
            expected = b"A=new\n" if enabled[0] else b"A=old\n"
            expected += b"" if enabled[1] else b"B=remove\n"
            expected += b"KEEP=yes\n"
            expected += b"C=add\n" if enabled[2] else b""
            expected += b"D=add\n" if enabled[3] else b""
            self.assertEqual(expected, selected_bytes(selection, selected))

    def test_nested_conditionals_and_continuations_are_indivisible(self):
        block = (b"# Feature gate\nifeq ($(ENABLED),true)\n"
                 b"ifdef INNER\nPRODUCT_PACKAGES += \\\n    First \\\n    Second\n"
                 b"else\nPRODUCT_PACKAGES += Third\nendif\nendif\n")
        after = b"FLAG=yes\n" + block + b"OTHER=yes\n"
        selection = ReviewSelection(plan_for_bytes(b"", after, revision=None))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(3, len(choices))
        self.assertEqual(block, selected_bytes(selection, {choices[1].id}))
        self.assertEqual(b"FLAG=yes\nOTHER=yes\n", selected_bytes(selection, {choices[0].id, choices[2].id}))

    def test_conditional_changes_retain_old_block_when_unchecked(self):
        before = b"ifeq ($(X),y)\nA=old\nelse\nA=else\nendif\nB=old\n"
        after = before.replace(b"A=old", b"A=new").replace(b"B=old", b"B=new")
        selection = ReviewSelection(plan_for_bytes(before, after))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(2, len(choices))
        self.assertEqual(before.replace(b"B=old", b"B=new"), selected_bytes(selection, {choices[1].id}))

    def test_top_level_make_call_does_not_prevent_individual_flag_selection(self):
        before = b"$(call soong_config_set,feature,enabled,true)\nA=old\nB=old\n"
        after = before.replace(b"old", b"new")
        selection = ReviewSelection(plan_for_bytes(before, after))
        self.assertEqual(2, len(selection.choices["//depot/settings"]))
        self.assertEqual(before.replace(b"B=old", b"B=new"), selected_bytes(selection, {"2"}))

    def test_multiline_assignment_and_its_replacement_stay_together(self):
        before = b"PRODUCT_PACKAGES += \\\n  Old \\\n  Keep\nFLAG=no\n"
        after = before.replace(b"Old", b"New").replace(b"FLAG=no", b"FLAG=yes")
        selection = ReviewSelection(plan_for_bytes(before, after))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(2, len(choices))
        self.assertEqual(before.replace(b"FLAG=no", b"FLAG=yes"), selected_bytes(selection, {choices[1].id}))

    def test_init_sections_keep_event_header_and_commands_together(self):
        before = b"on boot\n    setprop keep yes\non post-fs-data\n    mkdir /data/keep 0770 system system\n"
        after = before.replace(b"setprop keep yes", b"setprop keep yes\n    setprop added yes") + b"on init\n    setprop new yes\n"
        selection = ReviewSelection(plan_for_bytes(before, after, "init"))
        choices = selection.choices["//depot/settings"]
        self.assertEqual(2, len(choices))
        self.assertEqual(before + b"on init\n    setprop new yes\n", selected_bytes(selection, {choices[1].id}))
        self.assertEqual(after.rsplit(b"on init", 1)[0], selected_bytes(selection, {choices[0].id}))

    def test_crlf_and_unicode_are_preserved(self):
        before = "# café\r\nA=old\r\nB=old\r\n".encode()
        after = before.replace(b"old", b"new")
        selection = ReviewSelection(plan_for_bytes(before, after))
        self.assertEqual(before.replace(b"B=old", b"B=new"), selected_bytes(selection, {"2"}))

    def test_structured_binary_unknown_and_unfinished_files_are_whole_choices(self):
        cases = [
            (b'<root><a x="1"/></root>', b'<root><a x="2"/><b/></root>', "xml", "text"),
            (b'{"a":1}', b'{"a":2,"b":3}', "json", "text"),
            (b"#if ENABLED\nold\n#endif\n", b"#if ENABLED\nnew\n#endif\n", "text", "text"),
            (b"old", b"new", "make", "binary"),
            (b"\x00\xff", b"\x00\xfe", "make", "text"),
            (b"A=old", b"A=old\nB=new\n", "make", "text"),
            (b"\xef\xbb\xbfA=old\n", b"\xef\xbb\xbfA=new\n", "make", "text"),
            (b"define macro\nA=old\nendef\n", b"define macro\nA=new\nendef\n", "make", "text"),
        ]
        for before, after, format, file_type in cases:
            with self.subTest(format=format, before=before):
                selection = ReviewSelection(plan_for_bytes(before, after, format, file_type))
                self.assertEqual(1, len(selection.choices["//depot/settings"]))
                self.assertEqual(before, selected_bytes(selection, set()))
                self.assertEqual(after, selected_bytes(selection, {"1"}))

    def test_empty_added_file_can_be_included_or_excluded(self):
        selection = ReviewSelection(plan_for_bytes(b"", b"", revision=None))
        self.assertEqual(1, len(selection.plan_for(selection.all_selected())["changes"]))
        self.assertEqual([], selection.plan_for({"//depot/settings": set()})["changes"])

    def test_moved_flag_is_not_split_into_independent_remove_and_add(self):
        before = b"A=yes\nB=yes\nC=yes\nD=yes\n"
        after = b"B=yes\nC=yes\nD=yes\nA=yes\n"
        selection = ReviewSelection(plan_for_bytes(before, after))
        self.assertEqual(1, len(selection.choices["//depot/settings"]))
        self.assertEqual(before, selected_bytes(selection, set()))
        self.assertEqual(after, selected_bytes(selection, {"1"}))

    def test_unknown_choices_corrupt_plan_and_second_selection_rejected(self):
        plan = plan_for_bytes(b"A=old\n", b"A=new\n")
        selection = ReviewSelection(plan)
        for mapping in ({}, {"//depot/settings": {"999"}}, {"//wrong": {"1"}}):
            with self.assertRaises(ValueError):
                selection.plan_for(mapping)
        reviewed = selection.plan_for(selection.all_selected())
        with self.assertRaises(ValueError):
            ReviewSelection(reviewed)
        plan["changes"][0]["after"] = ""
        with self.assertRaises(ValueError):
            ReviewSelection(plan)

    def test_all_selected_slsi_files_identical_to_original_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(directory)
            plan = Planner(p4, config).build()
            original = copy.deepcopy(plan)
            selection = ReviewSelection(plan)
            reviewed = selection.plan_for(selection.all_selected())
            self.assertEqual(plan, original)
            for before, after in zip(plan["changes"], reviewed["changes"]):
                for key in ("path", "before", "after", "revision", "after_sha256"):
                    self.assertEqual(before[key], after[key])
            self.assertEqual(plan["snapshots"], reviewed["snapshots"])
            self.assertIn("Check results below describe the original", summary(reviewed))
            self.assertFalse(p4.calls)

    def test_apply_selected_flag_only_skips_other_files_and_saves_selection_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / "workspace")
            plan = Planner(p4, config).build()
            selection = ReviewSelection(plan)
            selected = {path: set() for path in selection.choices}
            path = next(c["path"] for c in plan["changes"] if "system.board" in c["rules"])
            choice = next(c for c in selection.choices[path] if "BOARD_HAVE_BLUETOOTH :=" in c.label)
            selected[path] = {choice.id}
            reviewed = selection.plan_for(selected)
            self.assertNotEqual(plan["digest"], reviewed["digest"])
            with self.assertRaises(ApprovalError):
                execute(p4, reviewed, plan["digest"])
            self.assertFalse(p4.calls)
            journal = Path(directory) / "execution.json"
            result = execute(p4, reviewed, reviewed["digest"], acknowledge_reviews=True, acknowledge_blocked=True, journal_path=journal)
            self.assertEqual(1, len(result["files"]))
            text = Path(p4.where(path)).read_text()
            self.assertIn("BOARD_HAVE_BLUETOOTH := true", text)
            self.assertNotIn("BOARD_HAVE_BLUETOOTH_SLSI", text)
            self.assertIn("WLAN_VENDOR = 8", text)
            for other in plan["changes"]:
                if other["path"] != path:
                    self.assertFalse(Path(other["local_path"]).exists())
            saved = json.loads(journal.read_text())
            self.assertEqual(plan["digest"], saved["review_selection"]["source_plan_digest"])
            self.assertTrue(any(f["excluded"] for f in saved["review_selection"]["files"]))

    def test_selected_plan_retains_stale_revision_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(directory)
            plan = Planner(p4, config).build()
            selection = ReviewSelection(plan)
            reviewed = selection.plan_for(selection.all_selected())
            changed = next(c for c in reviewed["changes"] if c["revision"] is not None)
            rev, content, file_type = p4.data[changed["path"]]
            p4.data[changed["path"]] = (rev + 1, content, file_type)
            with self.assertRaisesRegex(ExecutionError, "revision changed"):
                execute(p4, reviewed, reviewed["digest"], acknowledge_reviews=True, acknowledge_blocked=True)
            self.assertFalse(p4.calls)

    def test_empty_selection_creates_no_changelist(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(directory)
            selection = ReviewSelection(Planner(p4, config).build())
            reviewed = selection.plan_for({path: set() for path in selection.choices})
            result = execute(p4, reviewed, reviewed["digest"], acknowledge_reviews=True, acknowledge_blocked=True)
            self.assertEqual("no_changes", result["status"])
            self.assertFalse(p4.calls)
