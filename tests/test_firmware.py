"""Firmware checks use mapped paths, without reading or comparing binaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bt_delta.blank import BlankPlanner
from bt_delta.catalog import CHIPSETS, default_catalog
from bt_delta.demo import fixture
from bt_delta.planner import Planner


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.p4, self.config = fixture(Path(temp.name))
        self.catalog = default_catalog()
        self.catalog['rules'] = [r for r in self.catalog['rules'] if r['id'] == 'vendor.firmware']
        self.current = next(p for p in self.p4.data if 'COOSA' in p and p.endswith('/mx140.bin'))
        self.reference = next(p for p in self.p4.data if 'BENI' in p and p.endswith('/mx140.bin'))

    def build(self, builder):
        with patch.object(self.p4, 'read_file', wraps=self.p4.read_file) as reads, \
                patch.object(self.p4, 'files', wraps=self.p4.files) as files:
            plan = builder(self.p4, self.config, self.catalog).build()
        self.assertFalse(any(call.args[0].endswith('/mx140.bin') for call in reads.call_args_list))
        self.assertFalse(any(call.args[0] == self.reference for call in files.call_args_list))
        self.assertFalse(any(s['path'].endswith('/mx140.bin') for s in plan['snapshots']))
        self.assertEqual([], plan['changes'])
        return next(c for c in plan['checks'] if c['rule'] == 'vendor.firmware')

    def test_each_chipset_checks_its_current_family_without_reference_or_hash(self):
        del self.p4.data[self.reference]
        self.config['firmware_sha256'] = 'obsolete setting is ignored'
        content = self.p4.data.pop(self.current)
        for chipset, family in CHIPSETS.items():
            path = self.current.replace('/quartz_s621p/', f'/{family}/')
            self.p4.data[path] = content
            self.config['chipset'] = chipset
            for builder in (Planner, BlankPlanner):
                with self.subTest(chipset=chipset, builder=builder.__name__):
                    check = self.build(builder)
                    self.assertEqual('pass', check['status'])
                    self.assertIn(family, check['message'])
                    self.assertEqual([path], check['paths'])
                    self.assertNotIn('SHA', check['message'])
            del self.p4.data[path]

    def test_missing_current_firmware_is_review_even_with_reference_present(self):
        del self.p4.data[self.current]
        for builder in (Planner, BlankPlanner):
            check = self.build(builder)
            self.assertEqual('review', check['status'])
            self.assertIn(self.current, check['message'])

    def test_override_cannot_pass_with_the_wrong_family(self):
        wrong = self.current.replace('/quartz_s621p/', '/papaya_s620/')
        self.p4.data[wrong] = self.p4.data.pop(self.current)
        self.config['paths']['current.vendor.firmware'] = wrong
        for builder in (Planner, BlankPlanner):
            check = self.build(builder)
            self.assertEqual('review', check['status'])
            self.assertIn('/firmware/quartz_s621p/', check['message'])


if __name__ == '__main__':
    unittest.main()
