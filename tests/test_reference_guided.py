"""Reference-led planning, alternative init names, and diagnostic evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.blank import BlankPlanner
from bt_delta.demo import fixture
from bt_delta.perforce import PerforceError
from bt_delta.planner import Planner
from bt_delta.reference import copy_make_settings


class ReferenceGuidedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.p4, self.config = fixture(Path(temp.name))
        self.messages = []
        self.p4.progress = self.messages.append

    def path(self, role, ending, vendor_model=False):
        branch = 'BENI' if role == 'reference' else 'COOSA'
        return next(path for path in self.p4.data if branch in path and path.endswith(ending)
                    and (not vendor_model or 'm36x_vendor/' in path))

    def put(self, path, text):
        revision, _, kind = self.p4.data[path]
        self.p4.data[path] = (revision, text.encode() if isinstance(text, str) else text, kind)

    def checks(self, plan, rule):
        return [item for item in plan['checks'] if item['rule'] == rule]

    def test_missing_board_include_and_keys_are_optional_in_both_previews(self):
        src = self.path('reference', 'BoardConfigCommon.mk', True)
        self.put(src, 'BOARD_HAVE_BLUETOOTH = false\n')
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            self.assertFalse(any(item['status'] == 'blocked' for item in self.checks(plan, 'vendor.board')))
            preview = next(item for item in plan['previews'] if item['rule'] == 'vendor.board')
            self.assertEqual(preview['content'], 'BOARD_HAVE_BLUETOOTH = false\n')
        self.assertTrue(any('skipped optional setting: include:BluetoothBoardConfigCommon.mk' in message for message in self.messages))

    def test_missing_reference_system_board_does_not_block_other_rules_with_explicit_chipset(self):
        del self.p4.data[self.path('reference', '/BoardConfigCommon.mk')]
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            self.assertEqual(self.checks(plan, 'system.board')[0]['status'], 'skipped')
            self.assertTrue(any(item['rule'] == 'vendor.board' for item in plan['previews']))
            self.assertFalse(any(item['rule'] == 'setup' and item['status'] == 'blocked' for item in plan['checks']))

    def test_missing_optional_wlan_declarations_use_explicit_chipset_without_inserting_defaults(self):
        self.put(self.path('reference', '/BoardConfigCommon.mk'), 'BOARD_HAVE_BLUETOOTH := false\n')
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            self.assertFalse(any(item['status'] == 'blocked' for item in plan['checks']))
            preview = next(item for item in plan['previews'] if item['rule'] == 'system.board')
            self.assertEqual(preview['content'], 'BOARD_HAVE_BLUETOOTH := false\n')

    def test_missing_source_setting_preserves_even_conditional_current_only_values(self):
        current = 'ifdef KEEP\nBT := true\nendif\ninclude old/BluetoothBoardConfigCommon.mk\n'
        self.assertEqual(copy_make_settings(current, '# no Bluetooth configuration\n', ['BT'], ['BluetoothBoardConfigCommon.mk']), current)

    def test_init_model_filename_is_found_for_current_and_reference(self):
        for role in ('current', 'reference'):
            old = self.path(role, '/init.m36x.rc', True)
            self.p4.data[old.replace('/init.m36x.rc', '/init.model.rc')] = self.p4.data.pop(old)
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            preview = next(item for item in plan['previews'] if item['rule'] == 'vendor.init')
            self.assertTrue(preview['target_path'].endswith('/init.model.rc'))
            self.assertTrue(preview['reference_path'].endswith('/init.model.rc'))
            self.assertIn('bluetooth_address', preview['content'])
        self.assertTrue(any('init.m36x.rc; 0 live' in message or '0 live file(s)' in message and 'init.m36x.rc' in message for message in self.messages))
        self.assertTrue(any('Discovery matched reference.vendor.model_init' in message and 'init.model.rc' in message for message in self.messages))

    def test_two_init_candidates_are_not_selected_arbitrarily(self):
        path = self.path('reference', '/init.m36x.rc', True)
        self.p4.data[path.replace('/init.m36x.rc', '/init.model.rc')] = self.p4.data[path]
        plan = Planner(self.p4, self.config).build()
        check = self.checks(plan, 'vendor.init')[0]
        self.assertEqual(check['status'], 'blocked')
        self.assertIn('init.model.rc', check['message'])
        self.assertIn('init.m36x.rc', check['message'])

    def test_absent_reference_verification_files_report_exact_paths(self):
        removed = []
        for suffix in ('/manifest.xml', '/bluetooth.mk', '/mx140.bin'):
            path = self.path('reference', suffix)
            removed.append(path)
            del self.p4.data[path]
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            for rule, path in zip(('vendor.hals', 'vendor.hcf', 'vendor.firmware'), removed):
                check = self.checks(plan, rule)[0]
                self.assertEqual(check['status'], 'skipped')
                self.assertIn(path, check['message'])
                self.assertIn('Exact path(s) searched', check['message'])

    def test_missing_current_manifest_is_a_review_with_path_and_reference_evidence(self):
        path = self.path('current', '/manifest.xml')
        reference = self.path('reference', '/manifest.xml')
        del self.p4.data[path]
        plan = Planner(self.p4, self.config).build()
        check = self.checks(plan, 'vendor.hals')[0]
        self.assertEqual(check['status'], 'review')
        self.assertIn(path, check['message'])
        self.assertIn(reference, check['paths'])

    def test_unmapped_route_reports_template_and_attempt_without_claiming_file_absence(self):
        spec = self.p4.specs[self.config['reference']['vendor_template']]
        for key in list(spec):
            if key.startswith('View') and '/EXYNOS/android/' in spec[key]:
                del spec[key]
        plan = Planner(self.p4, self.config).build()
        check = self.checks(plan, 'vendor.hals')[0]
        self.assertEqual(check['status'], 'skipped')
        self.assertIn(self.config['reference']['vendor_template'], check['message'])
        self.assertIn('/EXYNOS/android/device/samsung/erd8835/manifest.xml', check['message'])
        self.assertIn('No p4 files query was made', check['message'])

    def test_reference_aidl_only_does_not_require_checklist_hidl_entries(self):
        manifest = ('<manifest><hal format="aidl"><name>android.hardware.bluetooth</name>'
                    '<version>2</version><fqname>IBluetoothHci/default</fqname></hal></manifest>')
        for role in ('reference', 'current'):
            self.put(self.path(role, '/manifest.xml'), manifest)
        plan = Planner(self.p4, self.config).build()
        checks = self.checks(plan, 'vendor.hals')
        self.assertTrue(checks)
        self.assertTrue(all(item['status'] == 'pass' for item in checks))
        self.assertTrue(any('aidl' in item['message'] for item in checks))
        current = self.path('current', '/manifest.xml')
        self.put(current, manifest.replace('IBluetoothHci/default', 'IBluetoothHci/other'))
        check = self.checks(Planner(self.p4, self.config).build(), 'vendor.hals')[0]
        self.assertEqual(check['status'], 'review')
        self.assertIn('IBluetoothHci/default', check['message'])

    def test_empty_reference_manifest_does_not_require_any_hals(self):
        self.put(self.path('reference', '/manifest.xml'), '<manifest/>')
        del self.p4.data[self.path('current', '/manifest.xml')]
        check = self.checks(Planner(self.p4, self.config).build(), 'vendor.hals')[0]
        self.assertEqual(check['status'], 'skipped')
        self.assertIn('no selected Bluetooth', check['message'])

    def test_missing_hcf_files_report_folder_and_leave_makefile_unchanged(self):
        hcf = self.path('reference', '/bt.hcf')
        directory = hcf.rsplit('/', 1)[0]
        del self.p4.data[hcf]
        self.p4.data[directory + '/README.txt'] = (1, b'no hcf here', 'text')
        current = self.path('current', '/bluetooth.mk')
        self.put(current, '# no filter in current\n')
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            check = self.checks(plan, 'vendor.hcf')[0]
            self.assertEqual(check['status'], 'skipped')
            self.assertIn(directory.rsplit('/', 1)[0] + '/...', check['message'])
            self.assertIn('README.txt', check['message'])
            self.assertNotIn(current, {item['path'] for item in plan['changes']})

    def test_absent_reference_make_actions_are_not_created_from_checklist(self):
        src = self.path('reference', '/device_common.mk', True)
        self.put(src, 'PRODUCT_PACKAGES += FutureBluetoothService\nSLSI_WLBT_UNIFIED_FIRMWARE ?= reference_family\n')
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            preview = next(item for item in plan['previews'] if item['rule'] == 'vendor.packages')
            self.assertIn('FutureBluetoothService', preview['content'])
            self.assertIn('SLSI_WLBT_UNIFIED_FIRMWARE ?= reference_family', preview['content'])
            self.assertNotIn('libbt-vendor', preview['content'])
            self.assertNotIn('include ', preview['content'])
            self.assertNotIn('quartz_s621p', preview['content'])
        self.put(src, '# no Bluetooth content\n')
        plan = Planner(self.p4, self.config).build()
        self.assertEqual(self.checks(plan, 'vendor.packages')[0]['status'], 'skipped')
        self.assertFalse(any(item['rule'] == 'vendor.packages' for item in plan['previews']))

    def test_reference_init_permissions_win_and_missing_examples_are_not_inserted(self):
        src = self.path('reference', '/init.m36x.rc', True)
        self.put(src, 'on init\n    chmod 0640 /dev/btpower\n')
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            preview = next(item for item in plan['previews'] if item['rule'] == 'vendor.init')
            self.assertIn('chmod 0640 /dev/btpower', preview['content'])
            self.assertNotIn('bluetooth_address', preview['content'])
            self.assertNotIn('on post-fs-data', preview['content'])

    def test_server_read_errors_are_not_hidden_as_missing_optional_files(self):
        original = self.p4.files
        def files(path):
            if path.endswith('/manifest.xml'):
                raise PerforceError('Access denied reading reference manifest')
            return original(path)
        with patch.object(self.p4, 'files', side_effect=files):
            plan = Planner(self.p4, self.config).build()
        check = self.checks(plan, 'vendor.hals')[0]
        self.assertEqual(check['status'], 'blocked')
        self.assertIn('Access denied', check['message'])

    def test_log_explains_cached_reference_board_and_identifies_run(self):
        self.config['ap'] = ''
        self.config['chipset'] = 's5e8825'
        plan = Planner(self.p4, self.config).build()
        reference = self.path('reference', '/BoardConfigCommon.mk', True)
        log = '\n'.join(self.messages)
        self.assertIn('START normal plan: model=m36x', log)
        self.assertIn('reference.vendor template=DEMO_VENDOR_REFERENCE', log)
        self.assertIn('Content cache reused: ' + reference + '#1', log)
        self.assertIn('vendor.board: reference source=' + reference, log)
        self.assertIn('END normal plan', log)
        self.assertTrue(plan['checks'])


if __name__ == '__main__':
    unittest.main()
