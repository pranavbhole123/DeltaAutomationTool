import base64
import tempfile
import unittest

from bt_delta.demo import fixture
from bt_delta.planner import Planner
from bt_delta.reference import copy_make_settings
from bt_delta.transforms import TransformError


class ReferenceTests(unittest.TestCase):
    def test_copies_values_operators_comments_and_changed_include_path(self):
        source = 'BT ?= false # reference choice\ninclude vendor/new/BluetoothBoardConfigCommon.mk\n'
        current = 'KEEP := yes\r\nBT := true\r\ninclude vendor/old/BluetoothBoardConfigCommon.mk\r\n'
        result = copy_make_settings(current, source, ['BT'], ['BluetoothBoardConfigCommon.mk'])
        self.assertEqual(result, 'KEEP := yes\r\nBT ?= false # reference choice\r\ninclude vendor/new/BluetoothBoardConfigCommon.mk\r\n')
        self.assertEqual(copy_make_settings(result, source, ['BT'], ['BluetoothBoardConfigCommon.mk']), result)

    def test_missing_or_ambiguous_source_never_uses_defaults(self):
        for source in ('# BT := true\n', 'BT := true\nBT := false\n', 'ifdef X\nBT := true\nendif\n'):
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

    def test_system_board_keeps_static_keys_required_alongside_patterns(self):
        with tempfile.TemporaryDirectory() as root:
            p4, config = fixture(root)
            source = next(p for p in p4.data if 'PROD_BENI' in p and 'm36x_sssi' in p and p.endswith('BoardConfigCommon.mk'))
            revision, data, file_type = p4.data[source]
            p4.data[source] = (revision, data.replace(b'BOARD_HAVE_BLUETOOTH_SLSI := true\n', b''), file_type)
            plan = Planner(p4, config).build()
            check = next(c for c in plan['checks'] if c['rule'] == 'system.board')
            self.assertEqual(check['status'], 'blocked')
            self.assertIn('BOARD_HAVE_BLUETOOTH_SLSI', check['message'])


if __name__ == '__main__':
    unittest.main()
