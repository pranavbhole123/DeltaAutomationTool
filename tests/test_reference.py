import base64
import tempfile
import unittest

from bt_delta.demo import fixture
from bt_delta.planner import Planner
from bt_delta.blank import BlankPlanner
from bt_delta.reference import apply_reference_action, augment_actions_from_reference, copy_make_settings
from bt_delta.transforms import TransformError
from bt_delta.operations import select_actions, apply_actions


class ReferenceTests(unittest.TestCase):
    def test_init_selects_commands_across_all_repeated_reference_events(self):
        source = ("on post-fs\n    setprop unrelated keep\n"
                  "on post-fs\n    mkdir /data/misc/bluetooth 0770 bluetooth bluetooth\n"
                  "on post-fs\n    # Bluetooth permissions\n    chmod 0660 /dev/example\n"
                  "on property:sys.boot_completed=1\n    setprop unrelated.one yes\n"
                  "on property:sys.boot_completed=1\n    setprop unrelated.two yes\n")
        actions = select_actions("init.ensure", source, {"command_patterns": ["bluetooth"], "comment_patterns": ["Bluetooth"]})
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["event"], "on post-fs")
        self.assertEqual(actions[0]["commands"], ["mkdir /data/misc/bluetooth 0770 bluetooth bluetooth", "chmod 0660 /dev/example"])
        self.assertNotIn("unrelated", apply_actions("", actions))

    def test_init_comment_anchor_does_not_leak_between_repeated_actions(self):
        source = "on late-fs\n# Bluetooth\non late-fs\n    setprop unrelated keep\n"
        self.assertEqual(select_actions("init.ensure", source, {"comment_patterns": ["Bluetooth"]}), [])

    def test_explicit_init_event_still_limits_reference_selection(self):
        source = "on init\n    setprop bluetooth.init yes\non boot\n    setprop bluetooth.boot yes\non boot\n    setprop bluetooth.ready yes\n"
        actions = select_actions("init.ensure", source, {"event": "on boot", "command_patterns": ["bluetooth"]})
        self.assertNotIn("bluetooth.init", apply_actions("", actions))
        self.assertIn("bluetooth.ready", apply_actions("", actions))

    conditional_block = ('# SS_A2DP_FEATURE\n'
                         'ifeq ($(shell secspfbool SEC_PRODUCT_FEATURE_BLUETOOTH_SUPPORT_A2DP_AUDIO_HAL), y)\n'
                         'PRODUCT_PACKAGES += \\\n    libsehbluetooth_audio_session\n'
                         'endif\n')

    def package_copy(self, current, reference, report=None):
        actions = augment_actions_from_reference(reference, [{'type': 'make_packages',
                    'reference_package_patterns': [r'(?i)bluetooth']}])
        for action in actions:
            current = apply_reference_action(current, action, report=report)
        return current

    def test_conditional_package_copies_its_complete_reference_guard(self):
        reference = 'PRODUCT_PACKAGES += BluetoothAgent\n' + self.conditional_block
        current = '# current section\nPRODUCT_PACKAGES += BluetoothAgent\n'
        notices = []
        result = self.package_copy(current, reference, notices.append)
        self.assertEqual(result, current + self.conditional_block)
        self.assertEqual(self.package_copy(result, reference), result)
        self.assertEqual(result.count('BluetoothAgent'), 1)
        self.assertIn('Copying their reference condition(s)', notices[-1]['message'])

    def test_conditional_packages_preserve_nested_else_and_exclude_unrelated_packages(self):
        reference = ('ifdef OUTER\nifeq ($(MODE),first)\n'
                     'PRODUCT_PACKAGES += OtherPackage\nelse\n'
                     'PRODUCT_PACKAGES += OtherService libbluetooth_feature\nendif\nendif\n')
        expected = ('ifdef OUTER\nifeq ($(MODE),first)\nelse\n'
                    'PRODUCT_PACKAGES += libbluetooth_feature\nendif\nendif\n')
        self.assertEqual(self.package_copy('', reference), expected)
        self.assertEqual(self.package_copy(expected, reference), expected)

    def test_existing_conditional_package_elsewhere_is_not_duplicated(self):
        current = '# other section\n' + self.conditional_block + '# tail\n'
        self.assertEqual(self.package_copy(current, self.conditional_block), current)
        for current in ('PRODUCT_PACKAGES += libsehbluetooth_audio_session\n',
                        'ifdef OTHER\nPRODUCT_PACKAGES += libsehbluetooth_audio_session\nendif\n'):
            notices = []
            self.assertEqual(self.package_copy(current, self.conditional_block, notices.append), current)
            self.assertTrue(notices[-1]['review'])
            self.assertIn('conditions differ', notices[-1]['message'])

    def test_same_package_in_reference_both_branches_preserves_both(self):
        source = ('ifdef FIRST\nPRODUCT_PACKAGES += libbluetooth_feature\nelse\n'
                  'PRODUCT_PACKAGES += libbluetooth_feature\nendif\n')
        result = self.package_copy('', source)
        self.assertEqual(result.count('PRODUCT_PACKAGES += libbluetooth_feature'), 2)
        self.assertIn('else\nPRODUCT_PACKAGES += libbluetooth_feature\nendif', result)
        self.assertEqual(self.package_copy(result, source), result)

    def test_conditional_copy_uses_current_line_endings_and_rejects_malformed_source(self):
        current = '# current\r\n'
        result = self.package_copy(current, self.conditional_block)
        self.assertEqual(result, current + self.conditional_block.replace('\n', '\r\n'))
        with self.assertRaises(TransformError):
            self.package_copy(current, self.conditional_block.removesuffix('endif\n'))

    def test_both_plans_include_conditional_reference_package(self):
        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            source = next(path for path in p4.data if 'PROD_BENI' in path and
                          'm36x_sssi' in path and path.endswith('/device_common.mk'))
            rev, data, kind = p4.data[source]
            p4.data[source] = (rev, data + self.conditional_block.encode(), kind)
            for builder in (Planner, BlankPlanner):
                plan = builder(p4, config).build()
                self.assertFalse([c for c in plan['checks'] if c['rule'] == 'system.packages' and c['status'] == 'blocked'])
                preview = next(p for p in plan['previews'] if p['rule'] == 'system.packages')
                self.assertIn(self.conditional_block, preview['content'])
                change = next(c for c in plan['changes'] if 'system.packages' in c['rules'])
                self.assertIn(self.conditional_block.encode(), base64.b64decode(change['after']))

    def test_slsi_system_and_vendor_copy_comment_blocks_and_keyword_includes(self):
        from test_make_content import MakeContentTests

        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            for path in list(p4.data):
                if 'PROD_BENI' in path and path.endswith('/device_common.mk'):
                    rev, _, kind = p4.data[path]
                    p4.data[path] = (rev, (MakeContentTests.sample +
                        '# other section\ninclude vendor/extra/Bluetooth.mk\nPRODUCT_PACKAGES += DontCopyMe\n').encode(), kind)
            for builder in (Planner, BlankPlanner):
                plan = builder(p4, config).build()
                for rule in ('system.packages', 'vendor.packages'):
                    check = next(c for c in plan['checks'] if c['rule'] == rule)
                    self.assertEqual(check['status'], 'change', check)
                    preview = next(p for p in plan['previews'] if p['rule'] == rule)
                    self.assertIn('msnv12.bin', preview['content'])
                    self.assertIn('include vendor/extra/Bluetooth.mk', preview['content'])
                    self.assertNotIn('DontCopyMe', preview['content'])

    def test_copies_values_operators_comments_and_changed_include_path(self):
        source = 'BT ?= false # reference choice\ninclude vendor/new/BluetoothBoardConfigCommon.mk\n'
        current = 'KEEP := yes\r\nBT := true\r\ninclude vendor/old/BluetoothBoardConfigCommon.mk\r\n'
        result = copy_make_settings(current, source, ['BT'], ['BluetoothBoardConfigCommon.mk'])
        self.assertEqual(result, 'KEEP := yes\r\nBT ?= false # reference choice\r\ninclude vendor/new/BluetoothBoardConfigCommon.mk\r\n')
        self.assertEqual(copy_make_settings(result, source, ['BT'], ['BluetoothBoardConfigCommon.mk']), result)

    def test_missing_or_ambiguous_source_never_uses_defaults(self):
        self.assertEqual(copy_make_settings('KEEP=yes\n', '# BT := true\n', ['BT'], []), 'KEEP=yes\n')
        for source in ('BT := true\nBT := false\n', 'ifdef X\nBT := true\nendif\n'):
            with self.subTest(source=source), self.assertRaises(TransformError):
                copy_make_settings('KEEP=yes\n', source, ['BT'], [])

    def test_multiline_reference_statement_preserved(self):
        source = 'BT := first \\\n    second\n'
        self.assertEqual(copy_make_settings('BT := old\n', source, ['BT'], []), source)

    def test_patterns_copy_reference_named_bluetooth_flags(self):
        source = ('WLAN_VENDOR = 8\n'
                  'BOARD_NEW_BDROID_SWITCH := true\n'
                  'PRODUCT_A2DP_MODE ?= enabled\n'
                  'ROBOT_SETTING = unrelated\n')
        current = ('WLAN_VENDOR = 7\n'
                   'BOARD_NEW_BDROID_SWITCH := false\n'
                   'KEEP = current\n')
        pattern = r'(?i)(?:bluetooth|bluedroid|bdroid|(?:^|_)bt(?:_|$)|(?:^|_)a2dp(?:_|$))'
        result = copy_make_settings(current, source, ['WLAN_VENDOR'], [], [pattern])
        self.assertIn('BOARD_NEW_BDROID_SWITCH := true', result)
        self.assertIn('PRODUCT_A2DP_MODE ?= enabled', result)
        self.assertIn('KEEP = current', result)
        self.assertNotIn('ROBOT_SETTING', result)

    def test_each_scope_reads_its_own_reference_values(self):
        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            source = next(p for p in p4.data if 'PROD_BENI' in p and 'm36x_vendor' in p and p.endswith('BoardConfigCommon.mk'))
            rev, data, file_type = p4.data[source]
            p4.data[source] = (rev, data.replace(b'BOARD_HAVE_BLUETOOTH := true', b'BOARD_HAVE_BLUETOOTH = false'), file_type)
            plan = Planner(p4, config).build()
            self.assertFalse([c for c in plan['checks'] if c['status'] == 'blocked'])
            changes = {c['rules'][0]: base64.b64decode(c['after']) for c in plan['changes'] if c['path'].endswith('BoardConfigCommon.mk')}
            self.assertIn(b'BOARD_HAVE_BLUETOOTH = false', changes['vendor.board'])
            self.assertIn(b'BOARD_HAVE_BLUETOOTH := true', changes['system.board'])
            self.assertIn(b'hardware/demo/reference/include', changes['system.board'])
            self.assertIn(b'include vendor/demo/bluetooth/BluetoothBoardConfigCommon.mk', changes['system.board'])
            self.assertTrue(any(s['path'] == source for s in plan['snapshots']))

    def test_system_board_discovers_new_reference_bluetooth_key_by_name(self):
        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            source = next(p for p in p4.data if 'PROD_BENI' in p and 'm36x_sssi' in p and p.endswith('BoardConfigCommon.mk'))
            revision, data, file_type = p4.data[source]
            p4.data[source] = (revision, data + b'BOARD_FUTURE_BDROID_MODE := reference\n', file_type)
            plan = Planner(p4, config).build()
            change = next(c for c in plan['changes'] if c['rules'][0] == 'system.board')
            self.assertIn(b'BOARD_FUTURE_BDROID_MODE := reference', base64.b64decode(change['after']))

    def test_system_board_skips_absent_optional_keys_alongside_patterns(self):
        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            source = next(p for p in p4.data if 'PROD_BENI' in p and 'm36x_sssi' in p and p.endswith('BoardConfigCommon.mk'))
            revision, data, file_type = p4.data[source]
            p4.data[source] = (revision, data.replace(b'BOARD_HAVE_BLUETOOTH_SLSI := true\n', b''), file_type)
            plan = Planner(p4, config).build()
            check = next(c for c in plan['checks'] if c['rule'] == 'system.board')
            self.assertEqual(check['status'], 'change')
            preview = next(item for item in plan['previews'] if item['rule'] == 'system.board')
            self.assertNotIn('BOARD_HAVE_BLUETOOTH_SLSI', preview['content'])
            self.assertIn('BOARD_HAVE_BLUETOOTH := true', preview['content'])


if __name__ == '__main__':
    unittest.main()
