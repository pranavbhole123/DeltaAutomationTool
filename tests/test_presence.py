"""Existing content anywhere in a current file must not be added twice."""
import tempfile
import unittest
from pathlib import Path

from bt_delta.blank import BlankPlanner
from bt_delta.csc import add_missing_features
from bt_delta.demo import fixture
from bt_delta.planner import Planner
from bt_delta.reference import copy_make_settings
from bt_delta.transforms import transform


class PresenceTests(unittest.TestCase):
    def test_init_exact_command_elsewhere_preserves_file_and_reports_location(self):
        source = 'on init\r\n    # other action\r\n    chmod    0660   /dev/btpower # present\r\n\r\non boot\r\n    setprop keep yes\r\n'
        notices = []
        result = transform(source, {'type': 'init_commands', 'event': 'boot',
                                   'commands': ['chmod 0660 /dev/btpower']}, report=notices.append)
        self.assertEqual(result, source)
        self.assertIn('same command is already present', notices[0]['message'])
        self.assertIn('line 3 in [on init]', notices[0]['message'])
        self.assertIn('Review event timing', notices[0]['message'])
        self.assertTrue(notices[0]['review'])

    def test_init_does_not_create_missing_event_when_all_commands_exist_elsewhere(self):
        source = 'on property:sys.ready=1\n    chown bluetooth system /dev/btpower\n'
        action = {'type': 'init_commands', 'event': 'boot', 'commands': ['chown bluetooth system /dev/btpower']}
        self.assertEqual(transform(source, action), source)

    def test_init_adds_only_absent_commands_and_ignores_commented_copies(self):
        source = ('on init\n    chmod 0660 /dev/btpower\n'
                  '    # chown bluetooth system /dev/btpower\n'
                  'on boot\n    setprop keep yes\n')
        action = {'type': 'init_commands', 'event': 'boot',
                  'commands': ['chmod 0660 /dev/btpower', 'chown bluetooth system /dev/btpower']}
        result = transform(source, action)
        self.assertEqual(result.count('chmod 0660 /dev/btpower'), 1)
        self.assertIn('on boot\n    setprop keep yes\n    chown bluetooth system /dev/btpower\n', result)
        self.assertEqual(transform(result, action), result)

    def test_init_same_event_commands_split_between_two_sections_are_not_duplicated(self):
        source = ('on boot\n    chmod 0660 /dev/btpower\n    setprop bluetooth.enabled true\n'
                  'on boot\n    chown bluetooth system /dev/btpower\n')
        action = {'type': 'init_commands', 'event': 'boot',
                  'commands': ['chmod 0660 /dev/btpower', 'chown bluetooth system /dev/btpower']}
        self.assertEqual(transform(source, action), source)

    def test_init_replacement_does_not_create_exact_copy_of_command_in_another_action(self):
        source = 'on init\n    chmod 0660 /dev/btpower\non boot\n    chmod 0600 /dev/btpower\n'
        notices = []
        result = transform(source, {'type': 'init_commands', 'event': 'boot',
                                   'commands': ['chmod 0660 /dev/btpower']}, report=notices.append)
        self.assertEqual(result, source)
        self.assertTrue(notices[0]['review'])
        self.assertIn('line 2 in [on init]', notices[0]['message'])
        self.assertIn('line 4 in [on boot]', notices[0]['message'])

    def test_init_does_not_confuse_other_command_type_or_similar_target_with_presence(self):
        source = 'on init\n    chown bluetooth system /dev/btpower\n    chmod 0660 /dev/btpower2\n'
        result = transform(source, {'type': 'init_commands', 'event': 'boot',
                                   'commands': ['chmod 0660 /dev/btpower']})
        self.assertTrue(result.endswith('on boot\n    chmod 0660 /dev/btpower\n'))

    def test_init_normalizes_quoted_arguments_without_changing_existing_text(self):
        source = 'on init\n    setprop bluetooth.name "my device"\n'
        action = {'type': 'init_commands', 'event': 'boot', 'commands': ["setprop bluetooth.name 'my device'"]}
        self.assertEqual(transform(source, action), source)

    def test_make_includes_normalize_whitespace_comments_and_multiple_paths(self):
        source = '# include a.mk\ninclude   other.mk   a.mk  # already included\n'
        action = {'type': 'ensure_lines', 'lines': ['include a.mk', 'include absent.mk', 'include   absent.mk']}
        result = transform(source, action)
        self.assertEqual(result, source + 'include absent.mk\n')
        self.assertEqual(transform(result, action), result)

    def test_optional_include_exists_without_creating_required_duplicate(self):
        source = '-include some/Bluetooth.mk\n'
        notices = []
        result = transform(source, {'type': 'ensure_lines', 'lines': ['include some/Bluetooth.mk']}, report=notices.append)
        self.assertEqual(result, source)
        self.assertTrue(notices[0]['review'])
        self.assertIn('line 1', notices[0]['message'])

    def test_packages_are_checked_in_all_lists_but_similar_names_are_distinct(self):
        source = '# PRODUCT_PACKAGES += BluetoothAgent\nPRODUCT_PACKAGES += BluetoothAgent2\nPRODUCT_PACKAGES += \\\n    BluetoothAgent\n'
        action = {'type': 'make_packages', 'packages': ['BluetoothAgent', 'NewBluetoothService']}
        result = transform(source, action)
        self.assertEqual(result, source + 'PRODUCT_PACKAGES += NewBluetoothService\n')

    def test_board_and_feature_assignments_reuse_existing_locations(self):
        source = '# unrelated\nOTHER := yes\n\nBT_FLAG = false\n'
        result = copy_make_settings(source, 'BT_FLAG := true\n', ['BT_FLAG'], [])
        self.assertEqual(result, '# unrelated\nOTHER := yes\n\nBT_FLAG := true\n')
        self.assertEqual(result.count('BT_FLAG'), 1)
        self.assertEqual(transform(result, {'type': 'assignments', 'values': {'BT_FLAG': 'true'}}), result)

    def test_xml_scans_nested_elements_and_json_keeps_key_paths_distinct(self):
        source = '<permissions><Nested><BluetoothFeature>true</BluetoothFeature></Nested></permissions>\n'
        self.assertEqual(transform(source, {'type': 'xml_elements', 'elements': {'BluetoothFeature': 'true'}}), source)
        reference = {'Region': {'BluetoothFeature': True}}
        current = {'Region': {'BluetoothFeature': False}, 'Other': {'BluetoothFeature': True}}
        merged, added = add_missing_features(reference, current)
        self.assertEqual(merged, current)
        self.assertEqual(added, [])
        merged, added = add_missing_features(reference, {'Other': {'BluetoothFeature': True}})
        self.assertEqual(added, ['/Region'])
        self.assertIn('Region', merged)

    def test_planner_reports_elsewhere_and_preserves_tab4_reference_suggestions(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory))
            initial = Planner(p4, config).build()
            preview = next(item for item in initial['previews'] if item['rule'] == 'system.postfs')
            target = preview['target_path']
            commands = [line.strip() for line in preview['content'].splitlines() if line.startswith('    ')]
            self.assertTrue(commands)
            moved = 'on init\n    ' + '\n    '.join(commands) + '\n'
            revision, _, kind = p4.data[target]
            p4.data[target] = revision, moved.encode(), kind
            messages = []
            p4.progress = messages.append
            plan = Planner(p4, config).build()
            checks = [item for item in plan['checks'] if item['rule'] == 'system.postfs']
            reviews = [item for item in checks if item['status'] == 'review' and 'already present elsewhere' in item['message']]
            self.assertTrue(reviews)
            self.assertIn(target, reviews[0]['paths'])
            change = next((item for item in plan['changes'] if item['path'] == target), None)
            self.assertFalse(change and 'system.postfs' in change['rules'])
            current_preview = next(item for item in plan['previews'] if item['rule'] == 'system.postfs')
            self.assertEqual(current_preview['content'], preview['content'])
            self.assertTrue(any('file-wide presence check: ' + target in line and 'line 2 in [on init]' in line for line in messages))
            blank = BlankPlanner(p4, config).build()
            self.assertEqual(next(item for item in blank['previews'] if item['rule'] == 'system.postfs')['content'], preview['content'])


if __name__ == '__main__':
    unittest.main()
