"""The approval view stays concise without losing detailed evidence or edits."""
import copy
import tempfile
import unittest
from pathlib import Path

from bt_delta.demo import fixture
from bt_delta.planner import Planner, compact_summary, save_plan, seal, summary


class CompactReportTests(unittest.TestCase):
    def test_compact_view_keeps_all_statuses_and_exact_diffs(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / 'workspace')
            plan = Planner(p4, config).build()
            original = copy.deepcopy(plan)
            text = compact_summary(plan)
            statuses, edits = text.split('PROPOSED EDITS', 1)
            for check in plan['checks']:
                self.assertIn(f"[{check['status'].upper()}] {check['title']}", statuses)
                self.assertNotIn(check['message'], statuses)
            for change in plan['changes']:
                self.assertIn(change['path'], edits)
                self.assertIn(change['diff'], edits)
            self.assertIn('Reference: BoardConfigCommon.mk', statuses)
            self.assertIn('Current: BoardConfigCommon.mk', statuses)
            self.assertNotIn('//', statuses)
            self.assertEqual(original, plan)

    def test_blocked_reasons_remain_in_saved_detailed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            p4, config = fixture(Path(directory) / 'workspace')
            plan = Planner(p4, config).build()
            reason = 'Cannot select a manifest because the mapped AP is ambiguous.'
            plan['checks'].append({'rule': 'vendor.hals', 'title': 'Verify HALs',
                                   'source': 'SLSI!B30:C30', 'status': 'blocked',
                                   'message': reason, 'paths': []})
            plan['changes'] = []
            seal(plan)
            compact = compact_summary(plan)
            self.assertIn('[BLOCKED] Verify HALs', compact)
            self.assertNotIn(reason, compact)
            self.assertTrue(compact.endswith('PROPOSED EDITS\nNone.\n'))
            self.assertIn(reason, summary(plan))
            output = save_plan(plan, Path(directory) / 'report')
            self.assertIn(reason, output.with_name('plan.txt').read_text())
            self.assertIn(reason, output.read_text())
