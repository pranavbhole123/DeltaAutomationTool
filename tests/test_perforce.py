"""Server-free regression tests for the Perforce protocol and write boundary."""

import marshal
import subprocess
import unittest
from unittest.mock import patch

from bt_delta.perforce import (
    MappingError, P4CLI, PerforceError, PerforceSearchLimit, WriteDisabledError,
    depot_roots, parse_view, translate_path, validate_depot_path,
)


CONFIG = {"port": "ssl:example.invalid:1666", "user": "tester", "client": "workspace"}


def result(*records, returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode,
        stdout=b"".join(marshal.dumps(record, 0) for record in records), stderr=stderr)


def stat(**values):
    return {b"code": b"stat", **{key.encode(): value.encode() if isinstance(value, str) else value
                               for key, value in values.items()}}


def error(generic, message="No such file(s)."):
    return {b"code": b"error", b"generic": generic, b"severity": 3, b"data": message.encode()}


class P4CLITests(unittest.TestCase):
    def setUp(self):
        self.p4 = P4CLI(CONFIG)
        self.run_patcher = patch("bt_delta.perforce.subprocess.run")
        self.run = self.run_patcher.start()
        self.addCleanup(self.run_patcher.stop)

    def test_read_file_pins_revision_and_preserves_binary(self):
        self.run.return_value = result({b"code": b"binary", b"data": b"\x00\xff\r\n"},
                                       {b"code": b"binary", b"data": b"tail"})
        self.assertEqual(self.p4.read_file("//depot/file.bin", 12), b"\x00\xff\r\ntail")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["p4", "-G", "-p", CONFIG["port"], "-u", "tester", "-c",
                                   "workspace", "print", "-q", "//depot/file.bin#12"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_missing_revision_is_resolved_then_pinned(self):
        self.run.side_effect = [result(stat(depotFile="//depot/file", rev="7", action="edit")),
                                result({b"code": b"text", b"data": b"content"})]
        self.assertEqual(self.p4.read_file("//depot/file"), b"content")
        self.assertEqual(self.run.call_args.args[0][-1], "//depot/file#7")

    def test_print_missing_file_is_an_error(self):
        self.run.return_value = result(error(17), returncode=1)
        with self.assertRaises(PerforceError):
            self.p4.read_file("//depot/file", 1)

    def test_print_empty_file_is_valid(self):
        self.run.return_value = result()
        self.assertEqual(self.p4.read_file("//depot/empty", 1), b"")

    def test_files_filters_deleted_heads(self):
        self.run.return_value = result(stat(depotFile="//depot/a", rev="1", action="add"),
                                       stat(depotFile="//depot/b", rev="2", action="delete"),
                                       stat(depotFile="//depot/c", rev="2", action="move/delete"))
        self.assertEqual([record["depotFile"] for record in self.p4.files("//depot/...")],
                         ["//depot/a"])

    def test_bounded_discovery_uses_case_insensitive_live_records_limit_and_remaining_timeout(self):
        self.run.return_value = result(stat(depotFile='//depot/EXYNOS12/manifest.xml', rev='2', action='edit'))
        found = self.p4.bounded_files('//depot/EXYNOS12/*manifest*.xml', timeout_seconds=2.5, max_records=10)
        self.assertEqual(found[0]['rev'], '2')
        self.assertEqual(self.run.call_args.args[0][-6:], ['files', '-i', '-e', '-m', '11', '//depot/EXYNOS12/*manifest*.xml'])
        self.assertEqual(self.run.call_args.kwargs['timeout'], 2.5)
        self.assertFalse(self.p4.writes_enabled)

    def test_bounded_discovery_rejects_truncated_candidates(self):
        self.run.return_value = result(stat(depotFile='//depot/a', rev='1', action='add'),
                                       stat(depotFile='//depot/b', rev='1', action='add'))
        with self.assertRaisesRegex(PerforceSearchLimit, 'Partial results were not used'):
            self.p4.bounded_files('//depot/...', timeout_seconds=3, max_records=1)

    def test_bounded_discovery_cannot_extend_configured_transport_timeout(self):
        self.run.return_value = result(error(17))
        self.assertEqual(self.p4.bounded_files('//depot/...', timeout_seconds=50, max_records=10), [])
        self.assertEqual(self.run.call_args.kwargs['timeout'], 30)

    def test_ev_empty_is_recognized_for_discovery_only(self):
        self.run.return_value = result(error(17), returncode=1)
        self.assertEqual(self.p4.files("//depot/..."), [])
        self.assertEqual(self.p4.fstat("//depot/new"), {})

    def test_empty_success_is_not_confirmed_absence(self):
        self.run.return_value = result()
        for operation in (lambda: self.p4.files("//depot/..."), lambda: self.p4.fstat("//depot/new")):
            with self.subTest(operation=operation), self.assertRaises(PerforceError):
                operation()

    def test_auth_and_permission_errors_do_not_look_like_missing_files(self):
        for generic in (6, 3, 0):
            self.run.return_value = result(error(generic, "Authentication failed"), returncode=1)
            for operation in (lambda: self.p4.files("//depot/..."),
                              lambda: self.p4.fstat("//depot/file")):
                with self.subTest(generic=generic), self.assertRaisesRegex(PerforceError, "Authentication"):
                    operation()

    def test_partial_result_plus_error_is_rejected(self):
        self.run.return_value = result(stat(depotFile="//depot/file", rev="1"), error(6, "Permission denied"))
        with self.assertRaisesRegex(PerforceError, "Permission"):
            self.p4.files("//depot/...")

    def test_outage_without_marshal_errors_is_reported(self):
        self.run.return_value = result(returncode=1, stderr=b"Connect to server failed")
        with self.assertRaisesRegex(PerforceError, "Connect to server failed"):
            self.p4.fstat("//depot/file")

    def test_bad_marshal_is_rejected(self):
        for output in (b"not marshal", marshal.dumps(["bad"]), b"{"):
            self.run.return_value = subprocess.CompletedProcess([], 0, output, b"")
            with self.subTest(output=output), self.assertRaisesRegex(PerforceError, "marshal"):
                self.p4.identity()

    def test_timeout_and_missing_executable_are_reported(self):
        for exception in (subprocess.TimeoutExpired("p4", 120), FileNotFoundError("p4 missing")):
            self.run.side_effect = exception
            with self.subTest(exception=exception), self.assertRaises(PerforceError):
                self.p4.identity()

    def test_client_spec_rejects_nonexistent_generated_spec(self):
        self.run.return_value = result(stat(Client="template", Root="C:/work", View0="//depot/... //template/..."))
        with self.assertRaisesRegex(PerforceError, "does not exist"):
            self.p4.client_spec("template")

    def test_client_spec_and_workspace_are_read_only(self):
        self.run.return_value = result(stat(Client="workspace", Update="2026/09/28 12:00:00", Root="C:/work"))
        self.assertEqual(self.p4.workspace_spec()["Root"], "C:/work")
        self.assertEqual(self.run.call_args.args[0][-3:], ["client", "-o", "workspace"])
        self.assertFalse(self.p4.writes_enabled)

    def test_identity_is_decoded(self):
        self.run.return_value = result(stat(serverAddress="example.invalid:1666", userName="tester"))
        self.assertEqual(self.p4.identity()["userName"], "tester")

    def test_fstat_keeps_opened_file_metadata(self):
        self.run.return_value = result(stat(depotFile="//depot/file", action="edit", otherOpen="1", otherOpen0="other@client"))
        self.assertEqual(self.p4.fstat("//depot/file")["otherOpen0"], "other@client")

    def test_where_requires_unique_absolute_mapping(self):
        self.run.return_value = result(stat(depotFile="//depot/file", path="C:/workspace/file"))
        self.assertEqual(self.p4.where("//depot/file"), "C:/workspace/file")
        for records in ((stat(depotFile="-//depot/file", path="C:/workspace/file"),),
                        (stat(depotFile="//depot/file", unmap="", path="C:/workspace/file"),),
                        (stat(path="C:/workspace/file"), stat(path="D:/workspace/file")),
                        (stat(path="relative/file"),), ()):
            self.run.return_value = result(*records)
            with self.subTest(records=records), self.assertRaises(MappingError):
                self.p4.where("//depot/file")

    def test_writes_disabled_by_default_for_every_mutation(self):
        operations = [lambda: self.p4.create_change("Approved BT delta"),
                      lambda: self.p4.sync("//depot/file", 1),
                      lambda: self.p4.edit("//depot/file", 123),
                      lambda: self.p4.add("C:/workspace/file", 123),
                      lambda: self.p4.shelve(123)]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(WriteDisabledError):
                operation()
        self.run.assert_not_called()

    def test_enable_writes_is_instance_local(self):
        self.p4.enable_writes()
        self.assertTrue(self.p4.writes_enabled)
        self.assertFalse(P4CLI(CONFIG).writes_enabled)

    def test_change_form_is_marshaled_and_has_no_existing_files(self):
        self.p4.enable_writes()
        self.run.return_value = result({b"code": b"info", b"data": b"Change 54321 created."})
        description = "BT delta approved\nExact reviewed plan: abc123\n"
        self.assertEqual(self.p4.create_change(description), "54321")
        self.assertEqual(self.run.call_args.args[0][-2:], ["change", "-i"])
        form = marshal.loads(self.run.call_args.kwargs["input"])
        self.assertEqual(form, {b"Change": b"new", b"Client": b"workspace", b"User": b"tester",
                                b"Status": b"pending", b"Description": description.encode()})
        self.assertNotIn("stdin", self.run.call_args.kwargs)

    def test_unrecognized_changelist_result_stops_execution(self):
        self.p4.enable_writes()
        self.run.return_value = result({b"code": b"info", b"data": b"Unexpected reply"})
        with self.assertRaisesRegex(PerforceError, "change number"):
            self.p4.create_change("Approved plan")

    def test_mutations_use_exact_paths_no_force_or_submit(self):
        self.p4.enable_writes()
        self.run.return_value = result(stat(action="edit"))
        self.p4.sync("//depot/file", 7)
        self.assertEqual(self.run.call_args.args[0][-2:], ["sync", "//depot/file#7"])
        self.p4.edit("//depot/file", 123)
        self.assertEqual(self.run.call_args.args[0][-4:], ["edit", "-c", "123", "//depot/file"])
        self.p4.add("C:/workspace/new file", 123)
        self.assertEqual(self.run.call_args.args[0][-4:], ["add", "-c", "123", "C:/workspace/new file"])
        self.p4.shelve(123)
        self.assertEqual(self.run.call_args.args[0][-3:], ["shelve", "-c", "123"])
        self.assertFalse(any("-f" in call.args[0] or "submit" in call.args[0] for call in self.run.call_args_list))

    def test_sync_up_to_date_accepted_but_missing_revision_rejected(self):
        self.p4.enable_writes()
        self.run.return_value = result(error(17, "//depot/file#7 - file(s) up-to-date."))
        self.p4.sync("//depot/file", 7)
        self.run.return_value = result(error(17, "//depot/file#7 - no such file(s)."))
        with self.assertRaises(PerforceError):
            self.p4.sync("//depot/file", 7)

    def test_literal_paths_reject_selectors_wildcards_and_option_injection(self):
        for path in ("//depot/file#head", "//depot/file@now", "//depot/*", "//depot/...",
                     "-x", "//depot/../file", "//depot/file\nother", "//depot//file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.p4.read_file(path, 1)
        self.run.assert_not_called()

    def test_revision_validation_prevents_ranges_or_deletions(self):
        self.p4.enable_writes()
        for rev in (0, -1, True, "head", "1,2", "1@now", "1\n", None):
            with self.subTest(rev=rev), self.assertRaises(ValueError):
                self.p4.sync("//depot/file", rev)
        self.run.assert_not_called()

    def test_files_accepts_globs_but_not_revision_selectors(self):
        self.run.return_value = result(error(17))
        self.p4.files("//depot/.../bt*.xml")
        with self.assertRaises(ValueError):
            self.p4.files("//depot/...@all")

    def test_describe_reads_every_indexed_file_with_no_mutation(self):
        self.run.return_value = result(stat(change="100", status="submitted", user="developer", client="dev",
                                            depotFile0="//depot/a", rev0="2", action0="edit", type0="text",
                                            depotFile1="//depot/b", rev1="1", action1="add", type1="binary"))
        value = self.p4.describe_change(100)
        self.assertEqual(len(value["files"]), 2)
        self.assertEqual(self.run.call_args.args[0][-3:], ["describe", "-s", "100"])
        self.p4.describe_change(100, shelved=True)
        self.assertEqual(self.run.call_args.args[0][-4:], ["describe", "-s", "-S", "100"])
        self.assertFalse(self.p4.writes_enabled)

    def test_describe_rejects_incomplete_records_and_invalid_numbers(self):
        for record in (stat(change="100", status="submitted", depotFile1="//depot/a", rev1="1", action1="edit"),
                       stat(change="100", status="submitted", depotFile0="//depot/a"),
                       stat(change="99", status="submitted"), stat(change="100")):
            self.run.return_value = result(record)
            with self.assertRaises(PerforceError):
                self.p4.describe_change(100)
        for number in ("-f", "100@now", "default", True, 0):
            with self.assertRaises(ValueError):
                self.p4.describe_change(number)

    def test_shelved_print_reads_exact_change_and_preserves_binary(self):
        self.run.return_value = result({b"code": b"binary", b"data": b"\x00\xff\r\n"})
        self.assertEqual(self.p4.read_shelved_file("//depot/file", 100), b"\x00\xff\r\n")
        self.assertEqual(self.run.call_args.args[0][-3:], ["print", "-q", "//depot/file@=100"])
        with self.assertRaises(ValueError):
            self.p4.read_shelved_file("//depot/file@=200", 100)

    def test_add_rejects_nonliteral_local_paths(self):
        self.p4.enable_writes()
        for path in ("relative.txt", "C:/workspace/*", "C:/workspace/file@now", "C:/workspace/.../file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.p4.add(path, 123)
        self.run.assert_not_called()


class ViewTests(unittest.TestCase):
    def test_view_fields_sort_numerically(self):
        spec = {"View10": "//ten/... //client/ten/...", "View2": "//two/... //client/two/..."}
        self.assertEqual([mapping.depot for mapping in parse_view(spec)], ["//two/...", "//ten/..."])

    def test_quoted_paths_and_recursive_translation(self):
        view = parse_view({'View': ['"//depot/platform source/..." "//template/source tree/..."']})
        self.assertEqual(translate_path(view, "source tree/framework/a.xml"),
                         "//depot/platform source/framework/a.xml")

    def test_star_is_one_path_segment_and_preserves_literal_extension(self):
        view = parse_view(["//depot/versions/*/file.xml //template/vendor/*/config.xml"])
        self.assertEqual(translate_path(view, "vendor/slsi/config.xml"), "//depot/versions/slsi/file.xml")
        with self.assertRaises(MappingError):
            translate_path(view, "vendor/slsi/subdir/config.xml")

    def test_ordered_specific_mapping_overrides_broad_mapping(self):
        view = parse_view(["//base/... //template/...", "//new/vendor/... //template/vendor/..."])
        self.assertEqual(translate_path(view, "vendor/config.xml"), "//new/vendor/config.xml")
        self.assertEqual(translate_path(view, "system/config.xml"), "//base/system/config.xml")

    def test_remapped_depot_file_no_longer_appears_at_old_location(self):
        view = parse_view(["//base/... //template/...", "//base/foo/... //template/bar/..."])
        with self.assertRaises(MappingError):
            translate_path(view, "foo/config.xml")
        self.assertEqual(translate_path(view, "bar/config.xml"), "//base/foo/config.xml")

    def test_exclusion_and_later_reinclusion(self):
        lines = ["//depot/... //template/...", "-//depot/private/... //template/private/..."]
        with self.assertRaises(MappingError):
            translate_path(parse_view(lines), "private/a.xml")
        lines.append("//depot/private/a.xml //template/private/a.xml")
        self.assertEqual(translate_path(parse_view(lines), "private/a.xml"), "//depot/private/a.xml")

    def test_ambiguous_overlay_fails_with_candidate_details(self):
        view = parse_view(["//base/... //template/...", "+//overlay/... //template/..."])
        with self.assertRaisesRegex(MappingError, "Ambiguous.*//base/file.*//overlay/file"):
            translate_path(view, "file")

    def test_exclusion_can_remove_one_overlay_candidate(self):
        view = parse_view(["//base/... //template/...", "+//overlay/... //template/...",
                           "-//base/file //template/file"])
        self.assertEqual(translate_path(view, "file"), "//overlay/file")

    def test_duplicate_overlay_same_path_is_unambiguous(self):
        view = parse_view(["//base/... //template/...", "+//base/... //template/..."])
        self.assertEqual(translate_path(view, "file"), "//base/file")

    def test_unsupported_or_malformed_mapping_fails_clearly(self):
        for lines in ([], ["&//depot/... //template/..."], ["//depot/* //template/..."],
                      ["//depot/%%1 //template/*"], ['"//depot/... //template/...'],
                      ["//depot/..."], ["//depot/...@now //template/..."]):
            with self.subTest(lines=lines), self.assertRaises(MappingError):
                parse_view(lines)

    def test_relative_path_validation(self):
        view = parse_view(["//depot/... //template/..."])
        for path in ("../file", "/file", "C:/file", "file#2", ".../file", "file@now", ""):
            with self.subTest(path=path), self.assertRaises((MappingError, ValueError)):
                translate_path(view, path)

    def test_depot_roots_only_include_static_prefixes(self):
        view = parse_view(["//depot/main/... //template/...", "//depot/main/file //template/file",
                           "+//other/vendor/*/a.xml //template/vendor/*/a.xml",
                           "-//depot/main/private/... //template/private/..."])
        self.assertEqual(depot_roots(view), ["//depot/main/", "//other/vendor/"])


if __name__ == "__main__":
    unittest.main()
