"""Cross-platform regression tests for separate attack/confirmation d20 tables."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_internal'))
from attack_crit_integrity import confirmation_result, relink_attack_confirmations, parse_and_link_attack_crit_events
from crit_roll_tables import (attack_d20_rows, confirmation_d20_rows,
                              confirmation_summary, observed_confirmation_threshold,
                              write_d20_distributions)
from crit_damage_metrics import analyze_crit_damage


def attack(line, roll, outcome='hit', target='X', t=0):
    return {'event':'attack', 'line':line, 'target':target, 'elapsed_seconds':t,
            'roll':roll, 'outcome':outcome,
            'text':f'(Combat): You attack {target}. You roll a {roll} (+100): you hit!'}


def confirmation(line, roll, result, target='X', t=0):
    text = {'confirmed':'Critical Hit!', 'resisted':'Critical hit resisted!',
            'failed':'Failure!', 'unknown':'Unknown message'}[result]
    return {'event':'confirm', 'line':line, 'target':target, 'elapsed_seconds':t,
            'roll':roll, 'bonus':200, 'total':roll+200,
            'outcome':result, 'confirmed':'1' if result=='confirmed' else '0',
            'text':f'(Combat): You roll to confirm a critical on {target}. '
                   f'You roll a {roll} (+200): {text}'}


class SeparateD20Tests(unittest.TestCase):
    def test_four_results_including_natural_1_and_20(self):
        confirms = [confirmation(2,1,'failed'), confirmation(4,2,'resisted'),
                    confirmation(6,20,'resisted'), confirmation(8,19,'confirmed'),
                    confirmation(10,9,'unknown')]
        rows=confirmation_d20_rows(confirms)
        self.assertEqual(len(rows),21)
        by_roll={r['roll']:r for r in rows}
        self.assertEqual(by_roll['1']['failed'],1)
        self.assertEqual(by_roll['20']['resisted'],1)
        self.assertEqual(by_roll['2']['resisted'],1)
        self.assertEqual(by_roll['9']['unknown'],1)
        self.assertEqual(by_roll['TOTAL']['total'],5)
        self.assertEqual(sum(by_roll['TOTAL'][key] for key in
                             ('confirmed','resisted','failed','unknown')),5)

    def test_success_and_fortification_denominators(self):
        rows = [confirmation(i*2,12,'confirmed') for i in range(165)] + [
            confirmation(400+i,18,'resisted') for i in range(4)] + [
            confirmation(450+i,2,'failed') for i in range(15)]
        c=confirmation_summary(rows)
        self.assertEqual(c['attempts'],184)
        self.assertEqual(c['known_attempts'],184)
        self.assertEqual(c['successful_confirmations'],169)
        self.assertAlmostEqual(c['confirmation_success_percent'],100*169/184)
        self.assertAlmostEqual(c['resisted_after_success_percent'],100*4/169)
        self.assertAlmostEqual(c['effective_crit_per_confirmation_percent'],100*165/184)
        self.assertEqual(c['successes_2_19'],169)

    def test_unknown_excluded_from_rate_not_count(self):
        rows=[confirmation(1,10,'unknown'),confirmation(2,10,'resisted'),
              confirmation(3,2,'failed')]
        c=confirmation_summary(rows)
        self.assertEqual(c['attempts'],3)
        self.assertEqual(c['known_attempts'],2)
        self.assertEqual(c['unknown'],1)
        self.assertEqual(c['confirmation_success_percent'],50.0)
        self.assertEqual(c['effective_crit_per_confirmation_percent'],0.0)
        self.assertEqual(confirmation_result('illegible', 'unresolved'),'unknown')

    def test_attack_d20_never_includes_confirmations(self):
        attacks=[attack(1,1,'miss'),attack(2,20,'critical_threat'),
                 attack(3,None,'other')]
        rows=attack_d20_rows(attacks)
        lookup={r['roll']:r for r in rows}
        self.assertEqual(lookup['1']['miss'],1)
        self.assertEqual(lookup['20']['critical_threat'],1)
        self.assertEqual(lookup['?']['other'],1)
        self.assertEqual(lookup['TOTAL']['total'],3)
        self.assertEqual(len(rows),22)

    def test_resisted_is_confirmation_threshold_success(self):
        bad=confirmation(1,6,'failed'); good=confirmation(3,7,'resisted')
        high,low,label=observed_confirmation_threshold([bad,good])
        self.assertEqual((high,low),(206,207))
        self.assertEqual(label,'207')
        # Natural 1/20 excluded from threshold, but not from the table.
        nat=confirmation(5,20,'confirmed')
        self.assertEqual(observed_confirmation_threshold([bad,good,nat]), (high,low,label))

    def test_two_targets_and_persisted_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            attack_rows=attack_d20_rows([attack(1,1,target='X'),attack(2,2,target='Y')])
            self.assertEqual(len(attack_rows),42)
            paths=write_d20_distributions(tmp,[attack(1,1,target='X')],
                                          [confirmation(2,20,'resisted',target='X')])
            with paths[1].open(newline='',encoding='utf8') as f:
                saved=list(csv.DictReader(f))
            self.assertEqual(len(saved),21)
            self.assertEqual(saved[-1]['total'],'1')
            self.assertEqual(saved[-1]['resisted'],'1')

    def test_linked_resisted_is_not_critical_damage(self):
        attacks=[attack(1,10,'critical_threat',t=0),attack(4,15,'critical_threat',t=1),
                 attack(7,12,'critical_threat',t=2)]
        confirms=[confirmation(2,20,'resisted',t=0.1),
                  confirmation(5,12,'confirmed',t=1.1),
                  confirmation(8,1,'failed',t=2.1)]
        outgoing=[]
        for i,amount in [(3,200),(6,800),(9,150)]:
            outgoing.append({'line':i,'amount':amount,'target':'X','source':'You',
                             'direction':'outgoing','damage_type':'pierce',
                             'elapsed_seconds':float((i-3)//3)+.2})
        stats,audit=analyze_crit_damage(attacks,confirms,outgoing)
        row=stats['X']
        self.assertEqual(row['confirmed_crits'],1)
        self.assertEqual(row['resisted_attacks'],1)
        self.assertEqual(row['failed_confirm_attacks'],1)
        self.assertEqual(row['matched_resisted'],1)
        self.assertEqual(row['matched_failed'],1)
        self.assertEqual(float(row['avg_physical_crit']),800.0)
        self.assertEqual(float(row['avg_physical_resisted']),200.0)
        self.assertEqual(float(row['avg_physical_failed']),150.0)
        self.assertEqual({r['classification'] for r in audit}, {'crit','resisted','failed'})

    def test_incomplete_confirm_message_remains_unknown(self):
        import re
        ar=re.compile(r'You attack (?P<target>.+?)\. You roll a (?P<roll>\d+)'
                      r'(?: \(\+(?P<bonus>\d+)\))?: (?P<result>.+)')
        cr=re.compile(r'You roll to confirm a critical on (?P<target>.+?)\.'
                      r' You roll a (?P<roll>\d+)'
                      r'(?: \(\+(?P<bonus>\d+)\))?: (?P<result>.+)')
        texts=['(Combat): You attack X. You roll a 20: you critical hit!',
               '(Combat): You roll to confirm a critical on X. You roll a 12 (+200):']
        a,c=parse_and_link_attack_crit_events(texts,[0.0,0.1],[1,2],
            attack_re=ar,confirm_re=cr,parse_number=lambda t:(int(t),None,None,None),
            normalize_text=str.strip,canonical_target=str.strip)
        self.assertEqual(len(a),1)
        self.assertEqual(len(c),1)
        self.assertEqual(c[0]['outcome'],'unknown')
        self.assertEqual(c[0]['roll'],12)
        self.assertEqual(confirmation_summary(c)['unknown'],1)

if __name__=='__main__':
    unittest.main()
