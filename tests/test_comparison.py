"""Blank-plan comparison and multi-changelist regressions without a server."""
import base64
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.blank import BlankPlanner
from bt_delta.cli import main
from bt_delta.comparison import compare_changelist, comparison_summary, content_units, parse_changelists
from bt_delta.comparison import save_comparison
from bt_delta.demo import DemoP4, fixture
from bt_delta.executor import ApprovalError, execute


class HistoryP4(DemoP4):
    def read_file(self, path, revision=None):
        self.reads.append((path, int(revision)))
        if path in self.unreadable:
            raise PermissionError("cannot read file")
        return self.history[path, int(revision)]

    def describe_change(self, number, shelved=False):
        value = copy.deepcopy(self.descriptions[str(number)])
        value['files'] = copy.deepcopy(self.shelves.get(str(number), []) if shelved else self.change_files[str(number)])
        return value

    def read_shelved_file(self, path, number):
        return self.shelf_content[str(number), path]

    def fstat(self, path):
        return self.states[path] if path in self.states else super().fstat(path)

    def files_at_change(self, *args):
        raise AssertionError('Historical template planning must never be used')


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p4, self.config = fixture(self.temp.name)
        self.p4.__class__ = HistoryP4
        self.p4.history = {(path, row[0]): row[1] for path, row in self.p4.data.items()}
        self.p4.reads, self.p4.unreadable = [], set()
        self.p4.descriptions, self.p4.change_files = {}, {}
        self.p4.shelves, self.p4.shelf_content, self.p4.states = {}, {}, {}
        self.register('100')
        self.blank = BlankPlanner(self.p4, self.config).build()
        self.p4.reads.clear()

    def register(self, number, status='submitted', client='developer_workspace'):
        self.p4.descriptions[number] = {'number': number, 'status': status, 'user': 'developer',
                                      'client': client, 'description': 'Bluetooth bring-up ' + number}
        self.p4.change_files[number] = []

    def wanted(self, rule):
        return next(item for item in self.blank['changes'] if rule in item['rules'])

    def submitted(self, path, content, number='100', action=None, file_type='text'):
        if number not in self.p4.descriptions:
            self.register(number)
        existing = self.p4.data.get(path)
        revision = existing[0] + 1 if existing else 1
        action = action or ('edit' if existing else 'add')
        entry = {'path': path, 'revision': revision, 'type': file_type, 'action': action}
        self.p4.change_files[number].append(entry)
        self.p4.history[path, revision] = content
        if action in ('delete', 'move/delete'):
            self.p4.data.pop(path, None)
        else:
            self.p4.data[path] = (revision, content, file_type)
        return entry

    def report(self, numbers='100', **kwargs):
        return compare_changelist(self.p4, self.config, numbers, **kwargs)

    def file(self, report, path):
        return next(item for item in report['files'] if item['path'] == path)

    def test_blank_plan_independent_of_current_content_and_reads_reference_only(self):
        for path, row in list(self.p4.data.items()):
            if 'COOSA' in path:
                self.p4.data[path] = (2, b'broken current file without valid syntax', row[2])
                self.p4.history[path, 2] = self.p4.data[path][1]
        plan = BlankPlanner(self.p4, self.config).build()
        self.assertEqual(plan['changes'], self.blank['changes'])
        self.assertFalse(any('COOSA' in path for path, rev in self.p4.reads))
        self.assertFalse(any(check['status'] == 'blocked' for check in plan['checks']))

    def test_matching_existing_current_content_does_not_hide_blank_plan(self):
        item = self.wanted('system.board')
        content = base64.b64decode(item['after'])
        self.submitted(item['path'], content)
        report = self.report()
        result = self.file(report, item['path'])
        self.assertTrue(any('BOARD_HAVE_BLUETOOTH' in unit['text'] for unit in result['matched']))
        self.assertIn(item['path'], [entry['path'] for entry in report['plan']['changes']])

    def test_unchanged_statements_in_developer_file_do_not_satisfy_blank_plan(self):
        item = self.wanted('system.board')
        before = self.p4.data[item['path']][1]
        self.submitted(item['path'], before + b'# unrelated developer comment\n')
        result = self.file(self.report(), item['path'])
        self.assertFalse(result['matched'])
        self.assertTrue(any('WLAN_CHIP' in unit['text'] for unit in result['missing']))
        self.assertTrue(any('unrelated developer comment' in unit['text'] for unit in result['extra']))

    def test_multiple_changelists_fill_different_parts_of_one_blank_file(self):
        item = self.wanted('system.board')
        path = item['path']
        before = self.p4.data[path][1]
        first = b'BOARD_HAVE_BLUETOOTH := true\n'
        second = b'BOARD_HAVE_BLUETOOTH_SLSI := true\n'
        self.submitted(path, before + first, '100')
        self.submitted(path, before + first + second, '101')
        report = self.report('101, 100, 101')
        result = self.file(report, path)
        self.assertEqual(report['counts']['changelists'], 2)
        self.assertEqual(result['changelists'], ['100', '101'])
        found = {unit['text']: unit['changelists'] for unit in result['matched']}
        self.assertEqual(found['BOARD_HAVE_BLUETOOTH := true'], ['100'])
        self.assertEqual(found['BOARD_HAVE_BLUETOOTH_SLSI := true'], ['101'])
        self.assertFalse(result['extra'])

    def test_later_selected_changelist_overrides_an_earlier_value(self):
        item = self.wanted('system.board')
        path, before = item['path'], self.p4.data[item['path']][1]
        self.submitted(path, before + b'BOARD_HAVE_BLUETOOTH := false\n', '100')
        self.submitted(path, before + b'BOARD_HAVE_BLUETOOTH := true\n', '101')
        result = self.file(self.report(['100', '101']), path)
        self.assertFalse(any('BOARD_HAVE_BLUETOOTH := false' in unit['text'] for unit in result['extra']))
        self.assertTrue(any('BOARD_HAVE_BLUETOOTH := true' in unit['text'] for unit in result['matched']))

    def test_later_selected_changelist_removes_previously_added_content(self):
        item = self.wanted('system.board')
        path, before = item['path'], self.p4.data[item['path']][1]
        self.submitted(path, before + b'BOARD_HAVE_BLUETOOTH := true\n', '100')
        self.submitted(path, before, '101')
        result = self.file(self.report(['100', '101']), path)
        self.assertFalse(result['matched'])
        self.assertTrue(any('BOARD_HAVE_BLUETOOTH := true' in unit['text'] for unit in result['missing']))

    def test_unselected_changelist_edits_do_not_leak_into_comparison(self):
        item = self.wanted('system.board')
        path, before = item['path'], self.p4.data[item['path']][1]
        self.submitted(path, before + b'UNSELECTED = yes\n', '999')
        self.submitted(path, before + b'UNSELECTED = yes\nBOARD_HAVE_BLUETOOTH := true\n', '100')
        result = self.file(self.report(), path)
        self.assertFalse(any('UNSELECTED' in unit['text'] for unit in result['extra']))
        self.assertTrue(result['matched'])

    def test_extra_files_from_multiple_changelists_include_filenames_and_origins(self):
        for path, number in (('//OTHER/a.txt', '100'), ('//OTHER/b.txt', '101')):
            self.submitted(path, b'additional\n', number)
        report = self.report('100 101')
        self.assertEqual(report['counts']['extra_files'], 2)
        self.assertEqual(self.file(report, '//OTHER/a.txt')['changelists'], ['100'])
        summary = comparison_summary(report)
        self.assertIn('Filename: a.txt', summary)
        self.assertIn('Filename: b.txt', summary)

    def test_different_value_has_missing_blank_value_and_extra_developer_value(self):
        item = self.wanted('system.board')
        self.submitted(item['path'], self.p4.data[item['path']][1] + b'BOARD_HAVE_BLUETOOTH := false\n')
        result = self.file(self.report(), item['path'])
        self.assertTrue(any('BOARD_HAVE_BLUETOOTH := true' in unit['text'] for unit in result['missing']))
        self.assertTrue(any('BOARD_HAVE_BLUETOOTH := false' in unit['text'] for unit in result['extra']))

    def test_make_package_added_to_multiline_list_matches_blank_single_line(self):
        item = self.wanted('system.packages')
        path = item['path']
        before = b'PRODUCT_PACKAGES += \\\n    ExistingPackage\n'
        self.p4.data[path] = (1, before, 'text')
        self.p4.history[path, 1] = before
        self.submitted(path, b'PRODUCT_PACKAGES += \\\n    ExistingPackage \\\n    BluetoothAgent\n')
        result = self.file(self.report(), path)
        self.assertFalse(result['missing'])
        self.assertFalse(result['extra'])
        self.assertTrue(result['matched'])

    def test_init_command_in_wrong_event_does_not_match_blank_plan(self):
        item = self.wanted('system.postfs')
        self.submitted(item['path'], b'on boot\n    mkdir /data/misc/bluetooth/logs 0770 bluetooth bluetooth\n')
        result = self.file(self.report(), item['path'])
        self.assertTrue(any('on post-fs-data' in unit['text'] and '/logs' in unit['text'] for unit in result['missing']))
        self.assertTrue(any('on boot' in unit['text'] and '/logs' in unit['text'] for unit in result['extra']))

    def test_json_compares_changed_keys_and_does_not_include_unchanged_non_bt_keys(self):
        item = self.wanted('csc.features')
        self.submitted(item['path'], b'{"CarrierFeature_BT_EnableSAP": "TRUE", "Keep": true}\n')
        result = self.file(self.report(), item['path'])
        self.assertTrue(any('FALSE' in unit['text'] for unit in result['missing']))
        self.assertTrue(any('TRUE' in unit['text'] for unit in result['extra']))
        self.assertFalse(any('/Keep' in unit['text'] for unit in result['extra']))

    def test_floating_feature_is_absent_from_default_and_blank_plans(self):
        from bt_delta.catalog import default_catalog
        self.assertNotIn('floating_feature', default_catalog()['path_rules']['system'])
        self.assertFalse(any(rule['id'] == 'system.floating' for rule in default_catalog()['rules']))
        self.assertFalse(any('floating' in entry['path'].lower() for entry in self.blank['changes']))
        self.assertFalse(any(check['rule'] == 'system.floating' for check in self.blank['checks']))

    def test_blank_carrier_includes_reference_keys_missing_from_an_empty_target(self):
        item = self.wanted('csc.features')
        reference = self.config['reference']['csc_path'] + '/INS/system/custom_carrier_feature_plan.json'
        self.assertEqual(base64.b64decode(item['after']), self.p4.data[reference][1])
        self.assertIn(b'"Keep": true', base64.b64decode(item['after']))
        self.submitted(item['path'], b'{"CarrierFeature_BT_EnableSAP": "FALSE", "Keep": false}\n')
        result = self.file(self.report(), item['path'])
        self.assertTrue(any('/Keep = true' in unit['text'] for unit in result['missing']))
        self.assertTrue(any('/Keep = false' in unit['text'] for unit in result['extra']))

    def test_carrier_comparison_covers_regions_outside_the_pasted_region_path(self):
        current_root = '//COOSA_CSC/m36x'
        reference_root = '//BENI_CSC/m36x'
        relative = 'OTHER/NEW_REGION/custom/custom_carrier_feature_plan.json'
        content = b'{"MissingFeature":true}\n'
        self.p4.data[reference_root + '/' + relative] = (1, content, 'text')
        self.p4.history[reference_root + '/' + relative, 1] = content
        path = current_root + '/' + relative
        self.submitted(path, content)
        report = self.report()
        item = self.file(report, path)
        self.assertTrue(item['planned'])
        self.assertFalse(item['outside_templates'])
        self.assertIn('current.csc: ' + relative, item['template_paths'])
        self.assertTrue(item['matched'])
        self.assertFalse(item['missing'])
        self.assertFalse(item['extra'])

    def test_verification_only_rule_does_not_create_blank_write_content(self):
        self.assertFalse(any('vendor.hals' in item['rules'] for item in self.blank['changes']))
        check = next(check for check in self.blank['checks'] if check['rule'] == 'vendor.hals')
        path = check['paths'][0]
        self.submitted(path, self.p4.data[path][1] + b'<!-- new developer work -->\n')
        result = self.file(self.report(), path)
        self.assertTrue(result['extra_file'])
        self.assertTrue(any('developer work' in unit['text'] for unit in result['extra']))

    def test_unreadable_file_retains_filename_and_incomplete_status(self):
        path = '//OTHER/a.txt'
        self.submitted(path, b'new\n')
        self.p4.unreadable.add(path)
        report = self.report()
        self.assertTrue(report['incomplete'])
        self.assertEqual(report['counts']['unreadable_files'], 1)
        self.assertTrue(self.file(report, path)['extra_file'])

    def test_blocked_blank_rule_does_not_hide_extra_files(self):
        self.config['chipset'] = 'unknown'
        self.submitted('//OTHER/a.txt', b'new\n')
        report = self.report()
        self.assertTrue(report['incomplete'])
        self.assertEqual(report['counts']['extra_files'], 1)

    def test_pending_foreign_unshelved_workspace_is_rejected(self):
        self.p4.descriptions['100']['status'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'unshelved'):
            self.report(source='workspace')

    def test_auto_supports_mixed_submitted_and_shelved_changelists(self):
        item = self.wanted('system.board')
        path, before = item['path'], self.p4.data[item['path']][1]
        self.submitted(path, before + b'BOARD_HAVE_BLUETOOTH := true\n')
        self.register('101', status='pending')
        self.p4.shelves['101'] = [{'path': path, 'revision': 2, 'type': 'text', 'action': 'edit'}]
        self.p4.shelf_content['101', path] = before + b'BOARD_HAVE_BLUETOOTH := true\nBOARD_HAVE_BLUETOOTH_SLSI := true\n'
        report = self.report(['100', '101'])
        self.assertEqual([change['content_source'] for change in report['changelists']], ['submitted', 'shelved'])
        self.assertEqual(len(self.file(report, path)['matched']), 2)

    def test_local_pending_file_uses_own_have_revision_to_extract_edits(self):
        item = self.wanted('system.board')
        path = item['path']
        self.p4.descriptions['100'].update(status='pending', client=self.p4.client)
        self.p4.change_files['100'] = [{'path': path, 'revision': 1, 'type': 'text', 'action': 'edit'}]
        self.p4.states[path] = {'haveRev': '1', 'change': '100', 'action': 'edit', 'type': 'text'}
        local = Path(self.p4.where(path))
        local.parent.mkdir(parents=True)
        local.write_bytes(self.p4.data[path][1] + b'BOARD_HAVE_BLUETOOTH := true\n')
        self.assertTrue(self.file(self.report(source='workspace'), path)['matched'])
        self.assertFalse(any(call[0] == 'sync' for call in self.p4.calls))

    def test_shelf_content_mutation_is_rejected(self):
        path = self.wanted('system.board')['path']
        self.p4.descriptions['100']['status'] = 'pending'
        self.p4.shelves['100'] = [{'path': path, 'revision': 1, 'type': 'text', 'action': 'edit'}]
        reads = 0
        def content(path, number):
            nonlocal reads
            reads += 1
            return b'NEW = first\n' if reads == 1 else b'NEW = changed\n'
        self.p4.read_shelved_file = content
        with self.assertRaisesRegex(Exception, 'content changed during comparison'):
            self.report(source='shelved')

    def test_metadata_mutation_is_rejected(self):
        original = self.p4.describe_change
        reads = 0
        def changed(number, shelved=False):
            nonlocal reads
            reads += 1
            value = original(number, shelved)
            if reads > 1:
                value['description'] = 'changed'
            return value
        self.p4.describe_change = changed
        with self.assertRaisesRegex(Exception, 'changed during comparison'):
            self.report()

    def test_binary_reference_file_matches_by_bytes(self):
        reference = next(path for path in self.p4.data if 'BENI' in path and path.endswith('/Bluetooth/bdroid_buildcfg.h') and '_sssi/' in path)
        self.p4.data[reference] = (1, b'\x00\xff\r\n', 'binary')
        self.p4.history[reference, 1] = b'\x00\xff\r\n'
        self.blank = BlankPlanner(self.p4, self.config).build()
        item = self.wanted('system.header')
        self.submitted(item['path'], b'\x00\xff\r\n', file_type='binary')
        result = self.file(self.report(), item['path'])
        self.assertFalse(result['missing'])
        self.assertFalse(result['extra'])
        self.assertTrue(result['matched'])

    def test_deletions_remain_visible(self):
        item = self.wanted('system.board')
        self.submitted(item['path'], b'', action='delete')
        result = self.file(self.report(), item['path'])
        self.assertTrue(result['missing'])
        self.assertTrue(result['extra'])
        self.assertTrue(result['developer_removals'])

    def test_save_writes_blank_plan_and_it_cannot_be_applied(self):
        report = self.report()
        output = save_comparison(report, Path(self.temp.name) / 'report')
        self.assertTrue(output.exists())
        self.assertTrue((output.parent / 'blank-plan' / 'plan.txt').exists())
        self.assertEqual(json.loads(output.with_suffix('.json').read_text())['counts'], report['counts'])
        with self.assertRaises(ApprovalError):
            execute(self.p4, report['plan'], report['plan']['digest'], acknowledge_reviews=True)
        self.assertFalse(any(call[0] in ('sync', 'edit', 'add', 'enable_writes', 'change') for call in self.p4.calls))

    def test_cli_accepts_multiple_changelists_and_generates_report(self):
        self.register('101')
        config_file = Path(self.temp.name) / 'config.json'
        config_file.write_text(json.dumps(self.config))
        output = Path(self.temp.name) / 'cli-report'
        with patch('bt_delta.cli.P4CLI', return_value=self.p4), patch('bt_delta.cli.logging.basicConfig'), \
                patch('bt_delta.cli.logging.FileHandler'), contextlib.redirect_stdout(io.StringIO()):
            status = main(['compare', str(config_file), '100', '101', '--out', str(output)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads((output / 'comparison.json').read_text())['counts']['changelists'], 2)


class UnitTests(unittest.TestCase):
    def test_parse_accepts_commas_spaces_newlines_and_removes_duplicates(self):
        self.assertEqual(parse_changelists('100, 101\n102 100'), ['100', '101', '102'])
        self.assertEqual(parse_changelists([100, '101', 100]), ['100', '101'])

    def test_invalid_input_rejected_before_perforce_access(self):
        for value in ('', 'default', '0', '100 -f', '100@now', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_changelists(value)

    def test_line_endings_and_make_indentation_do_not_change_units(self):
        first = content_units('//d/test.mk', b'BT := true\n')
        second = content_units('//d/test.mk', b'  BT  :=  true\r\n')
        self.assertEqual([(u['identity'], u['value']) for u in first], [(u['identity'], u['value']) for u in second])

    def test_json_keys_and_xml_comments_are_preserved(self):
        self.assertEqual(content_units('//d/file.json', b'{"nested":{"key":true}}')[0]['text'], '/nested/key = true')
        self.assertTrue(any('comment' in unit['text'] for unit in content_units('//d/file.xml', b'<root><!--comment--></root>')))


if __name__ == '__main__':
    unittest.main()
