"""Only numeric EXYNOS suffixes, mapped hardware roots, and HCF model folders."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.demo import fixture
from bt_delta.perforce import MappingError
from bt_delta.resolver import Resolver


class TargetedRouteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.p4, self.config = fixture(Path(temp.name))

    def replace_paths(self, old, new):
        self.p4.data = {path.replace(old, new): value for path, value in self.p4.data.items()}
        for spec in self.p4.specs.values():
            for key, value in spec.items():
                if key.startswith('View'):
                    spec[key] = value.replace(old, new)

    def test_exynos_numeric_versions_use_literal_queries_for_all_routes(self):
        for version in ('', '2', '12', '202601'):
            with self.subTest(version=version):
                resolver = Resolver(self.p4, self.config)
                expected = resolver.discover('current', 'vendor', 'manifest')
                self.replace_paths('/EXYNOS/', '/EXYNOS' + version + '/')
                resolver = Resolver(self.p4, self.config)
                with patch.object(self.p4, 'files', wraps=self.p4.files) as files:
                    actual = resolver.discover('current', 'vendor', 'manifest')
                    resolver.discover('current', 'vendor', 'board_config')
                    self.config['firmware'] = 'quartz_s621p'
                    firmware = resolver.discover('current', 'vendor', 'firmware')
                    self.assertIn('/EXYNOS' + version + '/android/', firmware)
                self.assertEqual(actual, expected.replace('/EXYNOS/', '/EXYNOS' + version + '/'))
                self.assertEqual(files.call_count, 3)
                for call in files.call_args_list:
                    self.assertNotIn('*', call.args[0])
                    self.assertNotIn('...', call.args[0])
                self.replace_paths('/EXYNOS' + version + '/', '/EXYNOS/')

    def test_non_numeric_exynos_suffix_is_not_generalized(self):
        self.replace_paths('/EXYNOS/', '/EXYNOS_DEV/')
        resolver = Resolver(self.p4, self.config)
        with patch.object(self.p4, 'files', wraps=self.p4.files) as files:
            with self.assertRaises(MappingError):
                resolver.discover('current', 'vendor', 'manifest')
        files.assert_not_called()

    def hardware_view(self, role, prefix):
        template = self.config[role]['vendor_template']
        self.p4.specs[template] = {
            'Client': template, 'Update': '1',
            'View0': prefix + '/... //' + template + '/android/vendor/samsung/hardware/vendor/...',
        }

    def test_common_and_cinnamon_share_hardware_anchor_for_both_targets(self):
        self.config.update(model='m34x', chipset='s5e8825', hcf_variant='m34xxx')
        roots = {
            'current': '//COOSA/VENDOR_VINCE_ONEUI_7_0/VENDOR/Cinnamon/vendor/samsung/hardware/vendor',
            'reference': '//PROD_BENI/ONEUI_8_5/SM-A266M_A536_A336_M336_M346_A256_P62X_ALL_MR202601/VENDOR_SOLO_ONEUI_4_1/VENDOR/Common/vendor/samsung/hardware/vendor',
        }
        for role, prefix in roots.items():
            self.hardware_view(role, prefix)
            root = prefix + '/bluetooth/slsi/s5e8825'
            mk, hcf = root + '/bluetooth.mk', root + '/m34xnsxx/mx140_bt.hcf'
            self.p4.data[mk] = (1, b'# makefile', 'text')
            self.p4.data[hcf] = (1, b'hcf', 'binary')
            self.p4.data[root + '/m35xnsxx/mx140_bt.hcf'] = (1, b'other model', 'binary')
            self.p4.data[root + '/notm34x/mx140_bt.hcf'] = (1, b'other model', 'binary')
            resolver = Resolver(self.p4, self.config)
            with patch.object(self.p4, 'files', wraps=self.p4.files) as files:
                self.assertEqual(resolver.discover(role, 'vendor', 'hcf_makefile'), mk)
                self.assertEqual([r['depotFile'] for r in resolver.discover(role, 'vendor', 'hcf')], [hcf])
            self.assertEqual([c.args[0] for c in files.call_args_list], [mk, root + '/...'])

    def test_broader_hardware_view_is_translated_without_cinnamon_assumption(self):
        self.replace_paths('/VENDOR/Cinnamon/', '/VENDOR/Common/')
        resolver = Resolver(self.p4, self.config)
        for role in ('reference', 'current'):
            self.assertIn('/VENDOR/Common/', resolver.discover(role, 'vendor', 'hcf_makefile'))
            records = resolver.discover(role, 'vendor', 'hcf')
            self.assertEqual(len(records), 1)
            self.assertIn('/VENDOR/Common/', records[0]['depotFile'])

    def test_hardware_exclusions_are_still_respected(self):
        prefix = '//COOSA/VENDOR/Cinnamon/vendor/samsung/hardware/vendor'
        self.hardware_view('current', prefix)
        template = self.config['current']['vendor_template']
        root = prefix + '/bluetooth/slsi/s5e8835'
        self.p4.specs[template]['View1'] = '-' + root + '/... //' + template + '/android/vendor/samsung/hardware/vendor/bluetooth/slsi/s5e8835/...'
        self.p4.data[root + '/m36xxx/bt.hcf'] = (1, b'hcf', 'binary')
        self.assertEqual(Resolver(self.p4, self.config).discover('current', 'vendor', 'hcf', optional=True), [])

    def test_original_exact_file_lookup_still_makes_one_query(self):
        resolver = Resolver(self.p4, self.config)
        with patch.object(self.p4, 'files', wraps=self.p4.files) as files:
            path = resolver.discover('current', 'vendor', 'board_config')
        files.assert_called_once_with(path)


if __name__ == '__main__':
    unittest.main()
