"""Regression: Direct OCR recognizes comma and period after confirmation target."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_internal'))
from process_latest import parse_attack_crit_events, summarize_attack_crit


class ConfirmationTests(unittest.TestCase):
    def test_both_target_separators(self):
        for sep in ('.', ','):
            with self.subTest(sep=sep):
                rows = [
                    '(Combat): You attack Karliath. You roll a 13 (+181): you critical hit!',
                    '(Combat): You roll to confirm a critical on Karliath'
                    + sep + ' You roll a 14 (+206): Critical Hit!',
                ]
                attacks, confirmations = parse_attack_crit_events(rows)
                self.assertEqual(len(attacks), 1)
                self.assertEqual(len(confirmations), 1)
                self.assertEqual(confirmations[0]['target'], 'Karliath')
                self.assertEqual(confirmations[0]['linked_attack_line'], attacks[0]['line'])
                self.assertEqual(confirmations[0]['roll'], 14)
                self.assertEqual(confirmations[0]['outcome'], 'confirmed')

    def test_natural_one_failure(self):
        rows = [
            '(Combat): You attack Karliath. You roll a 20 (+151): you critical hit!',
            '(Combat): You roll to confirm a critical on Karliath, You roll a 1 (+206): Failure!',
        ]
        attacks, confirmations = parse_attack_crit_events(rows)
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(confirmations[0]['confirmed'], 0)
        self.assertEqual(summarize_attack_crit(attacks, confirmations)['Karliath']['all_failed'], 1)

    def test_no_missing_separator(self):
        rows = [
            '(Combat): You roll to confirm a critical on Karliath You roll a 14 (+206): Critical Hit!',
        ]
        _, confirmations = parse_attack_crit_events(rows)
        self.assertEqual(len(confirmations), 0)


if __name__ == '__main__':
    unittest.main()
