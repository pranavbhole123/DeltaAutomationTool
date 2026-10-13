"""Optional build-log inputs drive current discovery without changing reference."""
from pathlib import Path
import tempfile
import unittest

from bt_delta.blank import BlankPlanner
from bt_delta.build_log import read_build_log
from bt_delta.catalog import default_catalog
from bt_delta.config import role_settings
from bt_delta.demo import fixture
from bt_delta.planner import Planner, summary


class BuildLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.p4, self.config = fixture(self.root / 'workspace')
        self.messages = []
        self.p4.progress = self.messages.append
        self.log = self.root / 'current build.log'

    def write_log(self, text):
        self.log.write_text(text, encoding='utf-8-sig')
        self.config['current_log_path'] = str(self.log)

    def test_extracts_identifiers_with_spacing_and_repeated_identical_values(self):
        self.write_log('ro.product.board=s5e8825\nTARGET:        m34x\nMODEL:         erd8825\nTARGET: m34x\n')
        self.assertEqual({'chipset': 's5e8825', 'model': 'm34x', 'ap': 'erd8825'},
                         read_build_log(self.log, self.messages.append))
        self.assertTrue(any('ro.product.board=s5e8825' in m for m in self.messages))

    def test_partial_log_does_not_fill_missing_fields_or_pick_conflicting_values(self):
        self.write_log('TARGET: m34x\nMODEL: erd8825\nMODEL: universal8825\nro.product.board=../../bad\n')
        self.assertEqual({'model': 'm34x'}, read_build_log(self.log, self.messages.append))
        self.assertTrue(any('conflicting MODEL' in m for m in self.messages))
        self.assertTrue(any('ignored invalid ro.product.board' in m for m in self.messages))

    def test_missing_log_and_blank_option_preserve_original_planning(self):
        for builder in (Planner, BlankPlanner):
            baseline = builder(self.p4, self.config).build()
            self.config['current_log_path'] = str(self.root / 'absent.log')
            fallback = builder(self.p4, self.config).build()
            self.assertEqual(baseline['checks'], fallback['checks'])
            self.assertEqual(baseline['changes'], fallback['changes'])
            self.config['current_log_path'] = ''
        self.assertTrue(any('could not be read' in m for m in self.messages))

    def test_partial_log_uses_existing_chipset_and_ap(self):
        self.write_log('TARGET: m36x\n')
        planner = Planner(self.p4, self.config)
        plan = planner.build()
        current = role_settings(plan['config'], 'current')
        self.assertEqual('m36x', current['model'])
        self.assertEqual('s5e8835', current['chipset'])
        self.assertEqual('erd8835', current['ap'])
        self.assertFalse(any(c['rule'] == 'setup' and c['status'] == 'blocked' for c in plan['checks']))

    def test_log_values_drive_current_paths_and_keep_reference_paths_independent(self):
        def new_path(value):
            return value.replace('m36x', 'm34x').replace('8835', '8825').replace('quartz_s621p', 'papaya_s620')
        self.p4.data = {new_path(p) if 'COOSA' in p else p: v for p, v in self.p4.data.items()}
        for key in ('system_template', 'vendor_template'):
            spec = self.p4.specs[self.config['current'][key]]
            for name, value in spec.items():
                if name.startswith('View'):
                    spec[name] = new_path(value)
        self.write_log('ro.product.board=s5e8825\nTARGET: m34x\nMODEL: erd8825\n')
        catalog = default_catalog()
        catalog['rules'] = [r for r in catalog['rules'] if r['id'] in ('vendor.firmware', 'vendor.hals')]
        for builder in (Planner, BlankPlanner):
            with self.subTest(builder=builder.__name__):
                planner = builder(self.p4, self.config, catalog)
                plan = planner.build()
                self.assertFalse(any(c['status'] == 'blocked' for c in plan['checks']))
                self.assertEqual('pass', next(c for c in plan['checks'] if c['rule'] == 'vendor.firmware')['status'])
                resolver = planner.resolver
                self.assertIn('/m34x_common/', resolver.discover('current', 'vendor', 'board_config'))
                self.assertIn('/m36x_common/', resolver.discover('reference', 'vendor', 'board_config'))
                self.assertIn('/erd8825/', resolver.discover('current', 'vendor', 'manifest'))
                self.assertIn('/erd8835/', resolver.discover('reference', 'vendor', 'manifest'))
                self.assertIn('/s5e8825/', resolver.discover('current', 'vendor', 'hcf_makefile'))
                self.assertIn('/s5e8835/', resolver.discover('reference', 'vendor', 'hcf_makefile'))
                self.assertIn('/m34xxx/', resolver.discover('current', 'vendor', 'hcf')[0]['depotFile'])
                header = resolver.discover('reference', 'system', 'bluetooth_header')
                self.assertIn('/m34x_common/', resolver.counterpart(header, 'system'))
                self.assertEqual('s5e8825', planner.variables['chipset'])
                self.assertEqual('papaya_s620', planner.variables['firmware'])
                self.assertIn('"model": "m34x"', summary(plan))
        self.assertTrue(any('TARGET=m34x → current model' in m for m in self.messages))
        self.assertTrue(any('MODEL=erd8825 → current ap' in m for m in self.messages))

    def test_removing_log_option_does_not_reuse_previous_extraction(self):
        self.write_log('MODEL: universal8835\n')
        previous = Planner(self.p4, self.config).build()['config']
        self.assertEqual('universal8835', role_settings(previous, 'current')['ap'])
        previous['current_log_path'] = ''
        next_plan = Planner(self.p4, previous).build()
        self.assertNotIn('build_log_inputs', next_plan['config']['current'])
        self.assertEqual('erd8835', role_settings(next_plan['config'], 'current')['ap'])


if __name__ == '__main__':
    unittest.main()
