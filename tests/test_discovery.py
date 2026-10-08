"""Versioned anchors, moved/renamed files, and bounded nearby searches."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.blank import BlankPlanner
from bt_delta.config import ConfigError, validate
from bt_delta.demo import fixture
from bt_delta.discovery import anchor_matches
from bt_delta.perforce import MappingError, PerforceError, PerforceSearchLimit
from bt_delta.planner import Planner
from bt_delta.resolver import DiscoveryMiss, Resolver


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.p4, self.config = fixture(Path(temporary.name))
        self.config['firmware'] = 'quartz_s621p'
        self.messages = []
        self.p4.progress = self.messages.append

    def path(self, role, suffix, vendor_model=False):
        branch = 'COOSA' if role == 'current' else 'BENI'
        return next(path for path in self.p4.data if branch in path and path.endswith(suffix)
                    and (not vendor_model or '_vendor/' in path))

    def move(self, old, new):
        self.p4.data[new] = self.p4.data.pop(old)
        return new

    def version_platform(self, role, version):
        spec = self.p4.specs[self.config[role]['vendor_template']]
        for key, value in list(spec.items()):
            if key.startswith('View'):
                spec[key] = value.replace('/EXYNOS/android/', '/' + version + '/android/')
        branch = 'COOSA' if role == 'current' else 'BENI'
        for path in list(self.p4.data):
            if branch in path and '/EXYNOS/android/' in path:
                self.move(path, path.replace('/EXYNOS/android/', '/' + version + '/android/'))

    def test_anchor_accepts_any_numeric_version_and_numeric_version_components(self):
        original_data = copy.deepcopy(self.p4.data)
        original_specs = copy.deepcopy(self.p4.specs)
        for version in ('EXYNOS2', 'EXYNOS3', 'EXYNOS10', 'EXYNOS8825', 'EXYNOS202601', 'EXYNOS12_3', 'EXYNOS9.0', 'EXYNOS_12-3'):
            with self.subTest(version=version):
                self.p4.data = copy.deepcopy(original_data)
                self.p4.specs = copy.deepcopy(original_specs)
                self.version_platform('current', version)
                resolver = Resolver(self.p4, self.config)
                found = resolver.discover('current', 'vendor', 'manifest')
                self.assertIn('/' + version + '/android/', found)
                self.assertEqual(resolver.attempts['current.vendor.manifest'], [found])
        self.assertTrue(any('Anchor variant accepted' in line for line in self.messages))

    def test_numeric_versions_only_apply_to_platform_anchors(self):
        self.assertTrue(anchor_matches('/EXYNOS/', '//depot/EXYNOS8825/android/'))
        self.assertFalse(anchor_matches('/VENDOR/Cinnamon/vendor/', '//depot/VENDOR2/Cinnamon3/vendor4/'))

    def test_planning_works_when_server_rejects_combined_wildcards(self):
        original = self.p4.bounded_files
        queries = []
        def simple_queries_only(pattern, **limits):
            queries.append(pattern)
            if pattern.count('...') + pattern.count('*') > 1:
                raise PerforceError('p4 files failed: Excessive combinations of wildcards in path and maps.')
            return original(pattern, **limits)
        self.p4.bounded_files = simple_queries_only
        board = self.path('reference', '/BoardConfigCommon.mk', True)
        renamed = self.move(board, board.replace('BoardConfigCommon.mk', 'BoardVendorConfigCommon.mk'))
        plan = Planner(self.p4, self.config).build()
        self.assertTrue(queries)
        self.assertTrue(any(renamed in check['paths'] for check in plan['checks']))
        self.assertFalse(any(check['status'] == 'blocked' and 'Excessive combinations' in check['message'] for check in plan['checks']))
        self.assertTrue(all('*' not in query and query.count('...') <= 1 for query in queries))

    def test_manifest_renamed_and_moved_below_exact_ap_mapping(self):
        self.version_platform('current', 'EXYNOS2')
        old = self.path('current', '/manifest.xml')
        new = self.move(old, old.replace('/manifest.xml', '/vintf/Device_Manifest.XML'))
        spec = self.p4.specs[self.config['current']['vendor_template']]
        spec['View1'] = new.split('/vintf/')[0] + '/... //' + spec['Client'] + '/android/vendor_platform/device/samsung/erd8835/...'
        found = Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')
        self.assertEqual(found, new)
        self.assertTrue(any('Fallback selected' in line and new in line for line in self.messages))

    def test_manifest_other_ap_is_rejected_even_if_its_filename_is_canonical(self):
        old = self.path('current', '/manifest.xml')
        new = self.move(old, old.replace('/manifest.xml', '/vintf/vendor_manifest.xml'))
        self.p4.data[old.replace('erd8835', 'erd8825')] = self.p4.data[new]
        self.assertEqual(Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest'), new)
        self.assertTrue(any('Fallback rejected' in line and 'erd8825' in line for line in self.messages))

    def test_manifest_resolves_erd_and_universal_independently_on_each_side(self):
        self.config['ap'] = 'universal8835'
        reference = self.path('reference', '/manifest.xml')
        reference = self.move(reference, reference.replace('/erd8835/', '/universal8835/'))
        self.version_platform('current', 'EXYNOS12_3')
        current = self.path('current', '/manifest.xml')
        spec = self.p4.specs[self.config['current']['vendor_template']]
        spec['View1'] = current.rsplit('/', 1)[0] + '/... //' + spec['Client'] + '/android/vendor_platform/device/samsung/erd8835/...'
        resolver = Resolver(self.p4, self.config)
        self.assertEqual(resolver.discover('reference', 'vendor', 'manifest'), reference)
        self.assertEqual(resolver.discover('current', 'vendor', 'manifest'), current)
        self.assertTrue(any('Fallback selected' in line and current in line for line in self.messages))

    def test_manifest_different_universal_ap_is_rejected(self):
        old = self.path('current', '/manifest.xml')
        wrong = self.move(old, old.replace('/erd8835/', '/universal8825/'))
        with self.assertRaises(DiscoveryMiss):
            Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')
        self.assertTrue(any('Fallback rejected' in line and wrong in line for line in self.messages))

    def test_reference_hcf_search_uses_common_hardware_mapping(self):
        original_data = copy.deepcopy(self.p4.data)
        original_specs = copy.deepcopy(self.p4.specs)
        for release_tree in ('Common', 'Common2026', 'OtherRelease'):
            with self.subTest(release_tree=release_tree):
                self.p4.data = copy.deepcopy(original_data)
                self.p4.specs = copy.deepcopy(original_specs)
                spec = self.p4.specs[self.config['reference']['vendor_template']]
                old_mk = self.path('reference', '/bluetooth.mk')
                old_hcf = self.path('reference', '/bt.hcf')
                new_mk = self.move(old_mk, old_mk.replace('/VENDOR/Cinnamon/', '/VENDOR/' + release_tree + '/'))
                new_hcf = self.move(old_hcf, old_hcf.replace('/VENDOR/Cinnamon/', '/VENDOR/' + release_tree + '/'))
                prefix = new_mk.split('/bluetooth/')[0]
                spec['View2'] = prefix + '/... //' + spec['Client'] + '/android/vendor/samsung/hardware/vendor/...'
                resolver = Resolver(self.p4, self.config)
                self.assertEqual(resolver.discover('reference', 'vendor', 'hcf_makefile'), new_mk)
                self.assertEqual([row['depotFile'] for row in resolver.discover('reference', 'vendor', 'hcf')], [new_hcf])
                self.assertTrue(all(query.startswith(prefix + '/') for query in resolver.attempts['reference.vendor.hcf_makefile']))

    def test_equal_manifest_candidates_require_override(self):
        old = self.path('current', '/manifest.xml')
        first = self.move(old, old.replace('manifest.xml', 'vendor_manifest.xml'))
        second = old.replace('manifest.xml', 'device_manifest.xml')
        self.p4.data[second] = self.p4.data[first]
        with self.assertRaisesRegex(MappingError, 'ambiguous equally relevant') as error:
            Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')
        self.assertIn(first, str(error.exception))
        self.assertIn(second, str(error.exception))
        self.config['paths']['current.vendor.manifest'] = second
        resolver = Resolver(self.p4, self.config)
        self.assertEqual(resolver.discover('current', 'vendor', 'manifest'), second)
        self.assertEqual(resolver.attempts['current.vendor.manifest'], [second])

    def test_missing_explicit_override_is_not_silently_replaced(self):
        actual = self.path('current', '/manifest.xml')
        missing = actual.replace('manifest.xml', 'absent.xml')
        self.config['paths']['current.vendor.manifest'] = missing
        resolver = Resolver(self.p4, self.config)
        with self.assertRaises(DiscoveryMiss):
            resolver.discover('current', 'vendor', 'manifest')
        self.assertEqual(resolver.attempts['current.vendor.manifest'], [missing])

    def test_all_file_roles_have_keyword_fallback_for_changed_names(self):
        scenarios = (
            ('board_config', '/BoardConfigCommon.mk', '/BoardVendorConfigCommon.mk', True),
            ('device_common', '/device_common.mk', '/device_model_common.mk', True),
            ('model_init', '/init.m36x.rc', '/init.device.rc', True),
            ('sec_product', '/SecProductFeature.common', '/ProductFeature.vendor.conf', True),
            ('bluetooth_header', '/Bluetooth/bdroid_buildcfg.h', '/BT/bdroid_new_buildcfg.h', False),
            ('root_init', '/system/core/rootdir/init.rc', '/system/core/rootdir/init.platform.rc', False),
            ('hcf_makefile', '/bluetooth.mk', '/wlbt_device.mk', False),
            ('firmware', '/mx140.bin', '/mx140_bt.bin', False),
        )
        original = copy.deepcopy(self.p4.data)
        for target, ending, replacement, model in scenarios:
            with self.subTest(target=target):
                self.p4.data = copy.deepcopy(original)
                path = self.path('reference', ending, model)
                scope = 'system' if target in ('bluetooth_header', 'root_init') else 'vendor'
                new = self.move(path, path[:-len(ending)] + replacement)
                self.assertEqual(Resolver(self.p4, self.config).discover('reference', scope, target), new)

    def test_common_directory_name_can_change_without_searching_other_models(self):
        old = self.path('current', '/BoardConfigCommon.mk', True)
        new = old.replace('/device/m36x_common/', '/device/m36x_bt_common/')
        self.move(old, new)
        spec = self.p4.specs[self.config['current']['vendor_template']]
        spec['View3'] = spec['View3'].replace('/device/m36x_common/', '/device/m36x_bt_common/')
        self.assertEqual(Resolver(self.p4, self.config).discover('current', 'vendor', 'board_config'), new)

    def test_bluetooth_folder_can_have_versioned_name(self):
        old = self.path('reference', '/Bluetooth/bdroid_buildcfg.h', True)
        new = self.move(old, old.replace('/Bluetooth/', '/Bluetooth2/'))
        records = Resolver(self.p4, self.config).discover('reference', 'vendor', 'bluetooth_folder')
        self.assertEqual([record['depotFile'] for record in records], [new])

    def test_hcf_filename_is_not_assumed_to_be_bt_hcf(self):
        old = self.path('current', '/bt.hcf')
        new = self.move(old, old.replace('bt.hcf', 'mx140_bt.hcf'))
        found = Resolver(self.p4, self.config).discover('current', 'vendor', 'hcf')
        self.assertEqual([record['depotFile'] for record in found], [new])

    def test_hcf_other_model_or_chipset_is_not_selected(self):
        old = self.path('current', '/bt.hcf')
        del self.p4.data[old]
        for wrong in (old.replace('m36xxx', 'm34xnsxx'), old.replace('s5e8835', 's5e8825')):
            self.p4.data[wrong] = (1, b'wrong chipset/model', 'binary')
        with self.assertRaises(DiscoveryMiss):
            Resolver(self.p4, self.config).discover('current', 'vendor', 'hcf')

    def test_firmware_moved_under_scsc_tree_is_found_and_hcf_is_separate(self):
        old = self.path('current', '/mx140.bin')
        new = old.replace('/vendor/samsung_slsi/mx140/', '/hardware/samsung_slsi/scsc_wifibt/').replace('mx140.bin', 'mx140_bt.bin')
        self.move(old, new)
        self.p4.data[new.replace('.bin', '.hcf')] = (1, b'not firmware', 'binary')
        wrong = new.replace('quartz_s621p', 'papaya_s620')
        self.p4.data[wrong] = (1, b'wrong family', 'binary')
        self.assertEqual(Resolver(self.p4, self.config).discover('current', 'vendor', 'firmware'), new)

    def test_excluded_file_is_never_selected_by_fallback(self):
        old = self.path('current', '/manifest.xml')
        new = self.move(old, old.replace('/manifest.xml', '/vintf/vendor_manifest.xml'))
        spec = self.p4.specs[self.config['current']['vendor_template']]
        spec['View99'] = '-' + new + ' //' + spec['Client'] + '/android/vendor_platform/device/samsung/erd8835/vintf/vendor_manifest.xml'
        with self.assertRaises(DiscoveryMiss):
            Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')
        self.assertTrue(any('Discovery file rejected: ' + new in line for line in self.messages))

    def test_time_budget_rejects_results_returned_after_deadline(self):
        self.config['discovery']['timeout_seconds'] = 1
        now = [0]
        original = self.p4.files
        def delayed(query):
            now[0] += 2
            return original(query)
        self.p4.files = delayed
        with patch('bt_delta.resolver.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(PerforceSearchLimit, 'time/result limit'):
                Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')

    def test_query_cap_does_not_treat_unfinished_search_as_absence(self):
        old = self.path('current', '/manifest.xml')
        self.move(old, old.replace('manifest.xml', 'vendor_manifest.xml'))
        self.config['discovery']['max_queries'] = 1
        with self.assertRaisesRegex(PerforceSearchLimit, 'budget exhausted'):
            Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest', optional=True)

    def test_result_cap_does_not_use_first_partial_match(self):
        old = self.path('current', '/manifest.xml')
        first = self.move(old, old.replace('manifest.xml', 'vendor_manifest.xml'))
        self.p4.data[old.replace('manifest.xml', 'device_manifest.xml')] = self.p4.data[first]
        self.config['discovery']['max_records'] = 1
        with self.assertRaisesRegex(PerforceSearchLimit, 'result limit'):
            Resolver(self.p4, self.config).discover('current', 'vendor', 'manifest')

    def test_fallback_cache_is_reused_and_queries_stay_in_included_subtrees(self):
        old = self.path('current', '/manifest.xml')
        new = self.move(old, old.replace('manifest.xml', 'vendor_manifest.xml'))
        resolver = Resolver(self.p4, self.config)
        self.assertEqual(resolver.discover('current', 'vendor', 'manifest'), new)
        queries = resolver.attempts['current.vendor.manifest'][:]
        self.assertTrue(all(query.startswith(old.split('/device/')[0] + '/') for query in queries))
        self.p4.files = lambda query: self.fail('Cached discovery should not query again')
        self.assertEqual(resolver.discover('current', 'vendor', 'manifest'), new)

    def test_renamed_current_manifest_has_same_verification_in_both_planners(self):
        self.version_platform('current', 'EXYNOS12')
        old = self.path('current', '/manifest.xml')
        new = self.move(old, old.replace('manifest.xml', 'vendor_manifest.xml'))
        for builder in (Planner, BlankPlanner):
            plan = builder(self.p4, self.config).build()
            checks = [item for item in plan['checks'] if item['rule'] == 'vendor.hals']
            self.assertTrue(checks)
            self.assertTrue(all(item['status'] in ('pass', 'manual') for item in checks))
            self.assertTrue(any(new in item['paths'] for item in checks))

    def test_renamed_current_header_is_compared_instead_of_added_again(self):
        source = self.path('reference', '/Bluetooth/bdroid_buildcfg.h')
        current = source.replace('PROD_BENI/ONEUI_8_5/ONEUI_8_5_MR202601', 'PROD_COOSA/ONEUI_9_0/FLUMEN')
        renamed = current.replace('/Bluetooth/bdroid_buildcfg.h', '/BT/bdroid_device_buildcfg.h')
        self.p4.data[renamed] = (1, b'current header to preserve', 'text')
        plan = Planner(self.p4, self.config).build()
        self.assertNotIn(current, {item['path'] for item in plan['changes']})
        check = next(item for item in plan['checks'] if item['rule'] == 'system.header')
        self.assertEqual(check['status'], 'review')
        self.assertEqual(check['paths'], [source, renamed])

    def test_current_bluetooth_folder_version_keeps_existing_file_location(self):
        source = self.path('reference', '/Bluetooth/bdroid_buildcfg.h', True)
        current = source.replace('PROD_BENI/ONEUI_8_5/ONEUI_8_5_MR202601', 'PROD_COOSA/ONEUI_9_0/FLUMEN')
        renamed = current.replace('/Bluetooth/', '/Bluetooth12/')
        self.p4.data[renamed] = (1, b'current header to preserve', 'text')
        plan = Planner(self.p4, self.config).build()
        self.assertNotIn(current, {item['path'] for item in plan['changes']})
        check = next(item for item in plan['checks'] if item['rule'] == 'vendor.bluetooth')
        self.assertEqual(check['status'], 'review')
        self.assertEqual(check['paths'], [source, renamed])

    def test_given_m34x_paths_under_exynos2_and_model_variant_are_discovered(self):
        replacements = {'m36x': 'm34x', 's5e8835': 's5e8825', 'erd8835': 'erd8825', 'quartz_s621p': 'papaya_s620'}
        def replace(value):
            for old, new in replacements.items():
                value = value.replace(old, new)
            return value
        for key, value in list(self.config.items()):
            if isinstance(value, str):
                self.config[key] = replace(value)
            elif isinstance(value, list):
                self.config[key] = [replace(item) for item in value]
        for spec in self.p4.specs.values():
            for key, value in list(spec.items()):
                if isinstance(value, str):
                    spec[key] = replace(value)
        self.p4.data = {replace(path): (rev, replace(data.decode()).encode() if kind == 'text' else data, kind)
                        for path, (rev, data, kind) in self.p4.data.items()}
        self.version_platform('current', 'EXYNOS2')
        manifest = self.path('current', '/manifest.xml')
        self.assertIn('/EXYNOS2/android/device/samsung/erd8825/', manifest)
        self.config['ap'] = 'universal8825'
        reference_manifest = self.path('reference', '/manifest.xml')
        reference_manifest = self.move(reference_manifest, reference_manifest.replace('/erd8825/', '/universal8825/'))
        reference_mk = self.path('reference', '/bluetooth.mk')
        reference_mk = self.move(reference_mk, reference_mk.replace('/VENDOR/Cinnamon/', '/VENDOR/Common/'))
        reference_hcf = self.path('reference', '/bt.hcf')
        self.move(reference_hcf, reference_hcf.replace('/VENDOR/Cinnamon/', '/VENDOR/Common/'))
        spec = self.p4.specs[self.config['reference']['vendor_template']]
        spec['View2'] = reference_mk.split('/bluetooth/')[0] + '/... //' + spec['Client'] + '/android/vendor/samsung/hardware/vendor/...'
        hcf = self.path('current', '/bt.hcf')
        actual_hcf = self.move(hcf, hcf.replace('/m34xxx/bt.hcf', '/m34xnsxx/mx140_bt.hcf'))
        resolver = Resolver(self.p4, self.config)
        self.assertEqual(resolver.discover('reference', 'vendor', 'manifest'), reference_manifest)
        self.assertEqual(resolver.discover('current', 'vendor', 'manifest'), manifest)
        self.assertEqual(resolver.discover('reference', 'vendor', 'hcf_makefile'), reference_mk)
        self.assertEqual([row['depotFile'] for row in resolver.discover('current', 'vendor', 'hcf')], [actual_hcf])
        self.assertEqual(resolver.discover('current', 'vendor', 'firmware'), self.path('current', '/mx140.bin'))

    def test_found_hcf_at_different_variant_does_not_add_broken_reference_filter(self):
        old = self.path('current', '/bt.hcf')
        new = self.move(old, old.replace('/m36xxx/bt.hcf', '/m36xnsxx/mx140_bt.hcf'))
        mk = self.path('current', '/bluetooth.mk')
        self.p4.data[mk] = (1, b'# filter absent\n', 'text')
        plan = Planner(self.p4, self.config).build()
        check = next(item for item in plan['checks'] if item['rule'] == 'vendor.hcf')
        self.assertEqual(check['status'], 'review')
        self.assertIn(new, check['paths'])
        self.assertIn('another model folder', check['message'])
        self.assertNotIn(mk, {item['path'] for item in plan['changes']})

    def test_discovery_limits_are_validated(self):
        for name, value in (('timeout_seconds', 0), ('timeout_seconds', float('inf')), ('max_queries', 1.5), ('max_records', True)):
            with self.subTest(name=name, value=value):
                config = copy.deepcopy(self.config)
                config['discovery'][name] = value
                with self.assertRaises(ConfigError):
                    validate(config)


if __name__ == '__main__':
    unittest.main()
