"""Stdlib tests; from program root: py -m unittest discover -s tests -v"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_internal'))
from damage_row_parser import damage_wrap_kind, join_damage_wrap, is_complete_outgoing, b_number_candidates, outgoing_b_token, find_unparsed_candidates
from process_latest import reassemble_wrapped_damage_rows, parse_combat_events
from post_numeric_guard import NumericGuard

class WrapNumericTests(unittest.TestCase):
    def parse(self, rows, guard=None):
        reconstructed, times, lines = reassemble_wrapped_damage_rows(rows, [i/10 for i in range(len(rows))], stats:={})
        events, *_ = parse_combat_events(reconstructed, times, lines, resolver=(guard.resolve if guard else NumericGuard().resolve))
        return events, stats

    def test_narrow_sneak_2_rows(self):
        ev,stats = self.parse(['(Combat): You sneak attack Experiment Y for 34,911','points of bludgeon damage.'])
        self.assertEqual((len(ev),ev[0]['amount'],ev[0]['mode'],stats['outgoing_wraps']), (1,34911,'sneak',1))

    def test_untyped_damage_2_rows(self):
        ev,stats = self.parse(['(Combat): You hit Experiment Y for 297', 'points of damage.'])
        self.assertEqual((len(ev), ev[0]['amount'],ev[0]['damage_type'],stats['outgoing_wraps']), (1,297,'untyped',1))

    def test_three_row_and_four_row(self):
        for rows in (
            ['(Combat): You sneak attack Experiment Y for 8,567', 'points of', 'bludgeon damage.'],
            ['(Combat): You hit Experiment Y for 4,567', 'points', 'of', 'piercing damage.'],
        ):
            with self.subTest(rows=rows):
                ev,stats=self.parse(rows)
                self.assertEqual(len(ev),1)
                self.assertEqual(stats['outgoing_wraps'],1)

    def test_no_join_across_new_combat(self):
        rows=['(Combat): You sneak attack Experiment Y for 12,345',
              '(Combat): You hit Experiment Y for 200 points of damage.']
        ev,stats=self.parse(rows)
        self.assertEqual((len(ev),ev[0]['amount'],stats['outgoing_wraps']),(1,200,0))
        self.assertEqual(find_unparsed_candidates(rows, [e['line'] for e in ev]),[1])

    def test_no_join_incomplete(self):
        rows=['(Combat): You hit Experiment Y for 15,000', 'bludgeon damage.']
        ev,stats=self.parse(rows)
        self.assertEqual(len(ev),0)
        self.assertEqual(stats['outgoing_wraps'],0)

    def test_b_in_number_only(self):
        text='(Combat): You hit Experiment Y for B,129 points of slash damage.'
        self.assertEqual(outgoing_b_token(text),'B,129')
        self.assertEqual(b_number_candidates('B,129'), {8129})
        self.assertEqual(b_number_candidates('B,140'), {8140})
        self.assertEqual(b_number_candidates('3,88B'), {3888})
        self.assertEqual(b_number_candidates('B1128'), {8128})
        self.assertEqual(b_number_candidates('B8,219'), set())
        self.assertIsNone(outgoing_b_token('(Combat): You hit B-Experiment for 8,219 points of damage.'))

    def test_b_ambiguous_requires_crop_verification(self):
        text='(Combat): You hit Experiment Y for B8,219 points of slash damage.'
        g=NumericGuard()
        ev,_=self.parse([text],g)
        self.assertEqual(len(ev),0)
        self.assertEqual(g.stats['b_rejected'],1)
        self.assertEqual(g.rows[0]['original_token'],'B8,219')

    def test_b_verified_correct_row(self):
        text='(Combat): You hit Experiment Y for B8,219 points of slash damage.'
        g=NumericGuard(trusted_live={1:{'result':'b_ocr_verified','final':8219,'original_token':'B8,219'}})
        ev,_=self.parse([text],g)
        self.assertEqual(len(ev),1)
        self.assertEqual(ev[0]['amount'],8219)
        self.assertEqual(g.stats['b_live_verified'],1)
        g_wrong=NumericGuard(trusted_live={1:{'result':'b_ocr_verified','final':8219,'original_token':'B8,218'}})
        ev,_=self.parse([text],g_wrong)
        self.assertEqual(len(ev),0)

    def test_no_random_number_from_same_timestamp(self):
        text='(Combat): You hit Experiment Y for 4,547 points of slash damage.'
        guard=NumericGuard(trusted_live={1:{'result':'ddo_format_verified','final':116,'original_token':'4,547'}})
        ev,_=self.parse([text],guard)
        self.assertEqual(len(ev),1)
        self.assertEqual(ev[0]['amount'],4547)

    def test_b_non_damage_not_changed(self):
        ev,_=self.parse(['(Combat): You hit Experiment B for 9,123 points of slash damage.'])
        self.assertEqual((len(ev),ev[0]['amount']), (1,9123))

if __name__=='__main__': unittest.main()
