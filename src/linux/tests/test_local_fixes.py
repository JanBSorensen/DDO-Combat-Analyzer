"""Tests for the local Windows/Linux attack+critical fix (no game required)."""
import csv
import os
import sys
import unittest
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[1] / '_internal'
sys.path.insert(0, str(INTERNAL))

from attack_crit_integrity import (
    live_attack_start, confirmation_result, parse_and_link_attack_crit_events,
    relink_attack_confirmations, assess_attack_integrity,
)
from crit_damage_metrics import analyze_crit_damage


class AttackIntegrityTests(unittest.TestCase):
    def test_live_wrapped_attack_without_roll_counts_once(self):
        lines = [
            '(Combat): You attack Experiment Y.',
            'You roll a 15 (+155): you critical hit!',
            '(Combat): You roll to confirm a critical on Experiment Y.',
            'You roll a 15 (+206): Critical Hit!',
            '(Combat): You hit Experiment Y for 1,234 points of pierce damage.',
            '(Combat): You sneak attack Experiment Y for 4,000 points of pierce damage.',
        ]
        self.assertEqual(sum(map(live_attack_start, lines)), 1)
        self.assertFalse(live_attack_start('(Combat): You sneak attack Experiment Y for 4,000 points of pierce damage.'))
        self.assertFalse(live_attack_start('(Combat): You roll to confirm a critical on Experiment Y.'))

    def test_resisted_is_not_confirmed_or_failed(self):
        self.assertEqual(confirmation_result('Critical hit resisted!'), 'resisted')
        self.assertEqual(confirmation_result('Critical Hit!'), 'confirmed')
        self.assertEqual(confirmation_result('Failure!'), 'failed')
        self.assertEqual(confirmation_result('unknown'), 'unknown')

    def test_wrapped_rolls_are_reconstructed_and_confirmed_once(self):
        import re
        digits = r'\d+'
        attack_re = re.compile(
            rf'\bYou\s+attack\s+(?P<target>.+?)\.\s+You\s+roll\s+a\s+'
            rf'(?P<roll>{digits})(?:\s*\(\+(?P<bonus>{digits})\))?\s*:\s*(?P<result>.+)$', re.I,
        )
        confirm_re = re.compile(
            rf'\bYou\s+roll\s+to\s+confirm\s+a\s+critical\s+on\s+'
            rf'(?P<target>.+?)[.,]\s+You\s+roll\s+a\s+'
            rf'(?P<roll>{digits})(?:\s*\(\+(?P<bonus>{digits})\))?\s*:\s*(?P<result>.+)$', re.I,
        )
        def parse_num(t):
            return int(t) if t is not None else None, None, None, None
        lines = [
            '(Combat): You attack Experiment Y. You roll a 15',
            '|(+155): you critical hit!',
            '(Combat): You roll to confirm a critical on Experiment Y.',
            '|You roll a 13 (+206): Critical Hit!',
            '(Combat): You hit Experiment Y for 1,234 points of pierce damage.',
        ]
        attacks, confirms = parse_and_link_attack_crit_events(
            lines, [0.0, 0.0, 0.0, 0.0, 0.1], list(range(1, 6)),
            attack_re=attack_re, confirm_re=confirm_re,
            parse_number=parse_num, normalize_text=str.strip,
            canonical_target=str.strip,
        )
        self.assertEqual(len(attacks), 1)
        self.assertEqual(len(confirms), 1)
        self.assertEqual(attacks[0]['outcome'], 'critical_threat')
        self.assertEqual(confirms[0]['outcome'], 'confirmed')
        self.assertEqual(confirms[0]['linked_attack_line'], 1)
        self.assertEqual(assess_attack_integrity(attacks, confirms)['Experiment Y']['status'], 'OK')

    def test_wrap_never_crosses_next_combat_message(self):
        import re
        attack_re = re.compile(r'\bYou attack (?P<target>.+?)\. You roll a (?P<roll>\d+): (?P<result>.+)')
        confirm_re = re.compile(r'\bYou roll to confirm a critical on (?P<target>.+?)\. You roll a (?P<roll>\d+): (?P<result>.+)')
        a, c = parse_and_link_attack_crit_events(
            ['(Combat): You attack X.', '(Combat): You hit X for 100 points of fire damage.'],
            [0.0, 0.1], [1, 2], attack_re=attack_re, confirm_re=confirm_re,
            parse_number=lambda t: (int(t), None, None, None),
            normalize_text=str.strip, canonical_target=str.strip,
        )
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]['outcome'], 'other')
        self.assertEqual(len(c), 0)

    def test_no_stale_or_reused_links(self):
        attacks = [dict(line='1', elapsed_seconds='0.0', target='X', event='attack', outcome='hit',
                        text='(Combat): You attack X. You roll a 10 (+155): you hit!')]
        confirms = [dict(line='2', elapsed_seconds='0.01', target='X', event='confirm',
                         outcome='confirmed', text='(Combat): You roll to confirm a critical on X. You roll a 12 (+206): Critical Hit!'),
                    dict(line='3', elapsed_seconds='0.02', target='X', event='confirm',
                         outcome='confirmed', text='(Combat): You roll to confirm a critical on X. You roll a 13 (+206): Critical Hit!'),
                    dict(line='30', elapsed_seconds='21.60', target='X', event='confirm',
                         outcome='confirmed', text='(Combat): You roll to confirm a critical on X. You roll a 14 (+206): Critical Hit!')]
        a, c = relink_attack_confirmations(attacks, confirms)
        self.assertEqual([bool(x.get('linked_attack_line')) for x in c], [True, False, False])
        self.assertEqual(a[0]['outcome'], 'critical_threat')
        self.assertEqual(assess_attack_integrity(a, c)['X']['status'], 'INVALID_ATTACK_DENOMINATOR')

    def test_no_link_to_miss_or_graze(self):
        a, c = relink_attack_confirmations(
            [{'line': 1, 'elapsed_seconds': 0, 'target': 'X', 'event': 'attack',
              'text': '(Combat): You attack X. You roll a 1 : you miss!'}],
            [{'line': 2, 'elapsed_seconds': 0.1, 'target': 'X', 'event': 'confirm',
              'text': '(Combat): You roll to confirm a critical on X. You roll a 10 (+206): Critical Hit!'}],
        )
        self.assertFalse(c[0]['linked_attack_line'])

    def test_suppress_averages_if_attacks_missing(self):
        attacks = [{'line': '10', 'target': 'X', 'event': 'attack', 'elapsed_seconds': '0.0',
                    'text': '(Combat): You attack X. You roll a 10 (+200): you hit!',
                    'outcome': 'hit'}]
        confirms = []
        for i in range(3):
            confirms.append({'line': str(11 + i), 'elapsed_seconds': str(0.1 + i * 0.1),
                             'target': 'X', 'event': 'confirm',
                             'text': '(Combat): You roll to confirm a critical on X. You roll a 10 (+200): Critical Hit!',
                             'outcome': 'confirmed', 'linked_attack_line': '10'})
        summaries, audit = analyze_crit_damage(attacks, confirms, [])
        self.assertEqual(summaries['X']['quality_status'], 'INVALID_ATTACK_DENOMINATOR')
        self.assertEqual(summaries['X']['matched_crits'], 0)
        self.assertEqual(summaries['X']['avg_physical_crit'], '')
        self.assertTrue(all(x['classification'] == 'unreliable_attack_log' for x in audit))

    def test_repair_old_confirm_csv_resisted(self):
        attacks = [{'line': '8', 'event': 'attack', 'target': 'X', 'elapsed_seconds': '1.5',
                    'outcome': 'critical_threat',
                    'text': '(Combat): You attack X. You roll a 15 (+206): you critical hit!'}]
        confirms = [{'line': '9', 'event': 'confirm', 'target': 'X', 'elapsed_seconds': '1.5',
                     'outcome': 'confirmed', 'confirmed': '1', 'linked_attack_line': '8',
                     'text': '(Combat): You roll to confirm a critical on X. You roll a 15 (+233): Critical hit resisted!'}]
        a, c = relink_attack_confirmations(attacks, confirms)
        self.assertEqual(c[0]['outcome'], 'resisted')
        self.assertEqual(c[0]['confirmed'], 0)
        self.assertEqual(assess_attack_integrity(a, c)['X']['confirmed_crit_messages'], 0)
        self.assertEqual(assess_attack_integrity(a, c)['X']['resisted_confirmations'], 1)

    def test_real_sessions_are_truthfully_reported(self):
        # Optional integration when the supplied regression inputs are available.
        dataset = Path(os.environ.get('DDO_TEST_DATA_DIR', '/nonexistent'))
        for folder, expected_attacks, expected_confirms in (
            ('combat_20261106_182601', 84, 25),
            ('combat_20261108_113928', 40, 184),
        ):
            path = dataset / folder / 'attack_events.csv'
            if not path.exists():
                continue
            with path.open(newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            a, c = relink_attack_confirmations(
                [r for r in rows if r.get('event') == 'attack'],
                [r for r in rows if r.get('event') == 'confirm'],
            )
            self.assertEqual(len(a), expected_attacks)
            self.assertEqual(len(c), expected_confirms)
            if folder == 'combat_20261108_113928':
                quality = assess_attack_integrity(a, c)['Karliath']
                self.assertEqual(quality['status'], 'INVALID_ATTACK_DENOMINATOR')
                self.assertEqual(quality['resisted_confirmations'], 4)
                self.assertEqual(quality['confirmed_crit_messages'], 165)
                self.assertLessEqual(quality['linked_confirmations'], len(a))
            else:
                self.assertEqual(assess_attack_integrity(a, c)['Experiment Y']['status'], 'OK')


if __name__ == '__main__':
    unittest.main()
