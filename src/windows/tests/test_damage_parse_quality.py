"""Regression tests for missing zero-damage grazing and misleading OCR warnings."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_internal"))
from damage_parse_quality import build_damage_parse_audit, classify_damage_candidate
from process_latest import parse_combat_events


class DamageQualityTests(unittest.TestCase):
    GRAZE = (
        "(Combat): You grazed Demogorgon but did no damage; "
        "312 points of damage were blocked by energy resistance."
    )
    BUFF = "(Combat): You gain +2 damage for 6 seconds after damaging an opponent beneath 25% health."
    HEAL = "(Combat): You were healed by Geosaurus-1's damage per caster level for 786 points."
    POSITIVE = "(Combat): You hit Demogorgon for 8,219 points of sonic damage."
    ZERO_HIT = "(Combat): You hit Demogorgon but did no damage; 140 points of damage were blocked by energy resistance."
    INCOMING = "(Combat): Demogorgon hits you for 1,000 points of slash damage."

    def test_false_positives_are_not_damage_candidates(self):
        for row in (self.BUFF, self.HEAL, "(Combat): You gain 5% damage resistance."):
            with self.subTest(row=row):
                self.assertIsNone(classify_damage_candidate(row))

    def test_outgoing_and_incoming_categories(self):
        self.assertEqual(classify_damage_candidate(self.POSITIVE), "outgoing_positive")
        self.assertEqual(classify_damage_candidate(self.GRAZE), "outgoing_zero_blocked")
        self.assertEqual(classify_damage_candidate(self.ZERO_HIT), "outgoing_zero_blocked")
        self.assertEqual(classify_damage_candidate(self.INCOMING), "incoming_positive")

    def test_grazing_zero_damage_is_parsed_as_blocked(self):
        outgoing, incoming, healing, corrections, suspicious = parse_combat_events([self.GRAZE])
        self.assertEqual(len(outgoing), 1)
        event = outgoing[0]
        self.assertEqual((event["target"], event["amount"], event["blocked_amount"]),
                         ("Demogorgon", 0, 312))
        self.assertEqual(event["pre_mitigation_amount"], 312)
        self.assertEqual(event["reduction_class"], "Energy resistance")
        self.assertEqual(suspicious, [])

    def test_existing_zero_hit_still_parses(self):
        outgoing, *_ = parse_combat_events([self.ZERO_HIT])
        self.assertEqual((len(outgoing), outgoing[0]["amount"], outgoing[0]["blocked_amount"]),
                         (1, 0, 140))

    def test_keyword_noise_ignored_but_missing_damage_flagged(self):
        rows = [self.BUFF, self.HEAL, self.GRAZE, self.POSITIVE, self.INCOMING]
        coverage, audit = build_damage_parse_audit(
            rows, outgoing=[{"line": 4, "amount": 8219}],
            incoming=[{"line": 5, "amount": 1000}],
        )
        self.assertEqual(coverage["ignored_keyword_only_lines"], 2)
        self.assertEqual(coverage["unparsed_zero_damage_count"], 1)
        self.assertEqual(coverage["unparsed_positive_damage_count"], 0)
        self.assertEqual(coverage["unparsed_incoming_damage_count"], 0)
        self.assertEqual(coverage["unmatched_candidate_lines"], [3])
        self.assertEqual(len(audit), 3)
        coverage, audit = build_damage_parse_audit(
            rows,
            outgoing=[{"line": 3, "amount": 0}, {"line": 4, "amount": 8219}],
            incoming=[{"line": 5, "amount": 1000}],
        )
        self.assertEqual(coverage["unparsed_damage_count"], 0)
        self.assertEqual(coverage["ignored_keyword_only_lines"], 2)
        self.assertEqual(len(audit), 2)

    def test_partial_outgoing_line_not_silently_ignored(self):
        coverage, _ = build_damage_parse_audit(
            ["(Combat): You hit Demogorgon for B8,219"], []
        )
        self.assertEqual(coverage["unparsed_positive_damage_count"], 1)
        self.assertEqual(coverage["unmatched_candidate_lines"], [1])


if __name__ == "__main__":
    unittest.main()
