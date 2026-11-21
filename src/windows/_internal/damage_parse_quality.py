"""Conservative classification of unparsed Combat Log damage messages.

The old catch-all 'damage' plus 'you' heuristic also matched buffs and heals.
A genuine damage candidate must start like outgoing, incoming, or fully
resisted outgoing damage. Independent of OCR and GUI for both analysis paths.
"""
from __future__ import annotations

import re
from collections import Counter

OUTGOING_POSITIVE_RE = re.compile(
    r"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+", re.IGNORECASE
)
OUTGOING_ZERO_RE = re.compile(
    r"\bYou\s+(?:sneak\s+attack|hit|graz(?:e|ed))\s+.+?\s+"
    r"but\s+did\s+no\s+damage\b", re.IGNORECASE
)
INCOMING_HIT_RE = re.compile(
    r"\b(?:hits?|hit)\s+you\s+for\s+|"
    r"\bYou\s+(?:were|are)\s+hit\s+by\s+.+?\s+for\s+",
    re.IGNORECASE,
)
INCOMING_TAKE_RE = re.compile(
    r"\bYou\s+(?:take|suffer)\b.*?\b(?:points?\s+of|damage)\b",
    re.IGNORECASE,
)


def classify_damage_candidate(text):
    """Classify actual damage starts, including incomplete OCR numeric fields.

    Buffs like 'You gain +2 damage' and heal names containing 'damage' are not
    evidence of missed damage, so never trigger damage-parser warnings.
    """
    row = " ".join(str(text or "").split())
    if OUTGOING_ZERO_RE.search(row):
        return "outgoing_zero_blocked"
    if OUTGOING_POSITIVE_RE.search(row):
        return "outgoing_positive"
    if INCOMING_HIT_RE.search(row) or INCOMING_TAKE_RE.search(row):
        return "incoming_positive"
    return None


def legacy_damage_keyword_match(text):
    """The old heuristic, retained only to quantify keyword-only matches."""
    low = str(text or "").lower()
    return ((" damage" in low and ("you " in low or " you " in low))
            or ("healed" in low and "you" in low))


def build_damage_parse_audit(raw_texts, outgoing, incoming=(), healing=(), *, source="ffv1_post"):
    """Cross-check reconstructed OCR starts against actual parsed events.

    Audit CSV rows contain only unparsed and ignored lines, not thousands of
    already-parsed events. Wrapped lines use their original first OCR row.
    """
    parsed_out = {int(e["line"]) for e in outgoing}
    parsed_in = {int(e["line"]) for e in incoming}
    parsed_any = parsed_out | parsed_in | {int(e["line"]) for e in healing}
    counts = Counter()
    unmatched_outgoing = []
    unmatched_incoming = []
    ignored = []
    audit_rows = []

    for number, text in enumerate(raw_texts, start=1):
        category = classify_damage_candidate(text)
        if category:
            counts[category] += 1
            matched = number in (parsed_in if category == "incoming_positive" else parsed_out)
            if not matched:
                if category == "incoming_positive":
                    unmatched_incoming.append(number)
                else:
                    unmatched_outgoing.append(number)
                    counts["unparsed_" + category] += 1
                audit_rows.append((number, category, "unparsed", text))
        elif number not in parsed_any and legacy_damage_keyword_match(text):
            # These matched the old heuristic without evidence of damage.
            # Retain the OCR line for inspection; do not warn about lost DPS.
            ignored.append(number)
            audit_rows.append((number, "keyword_only", "ignored", text))

    unmatched_all = unmatched_outgoing + unmatched_incoming
    record = {
        "analysis_source": source,
        # Keep the historical field's positive-only semantics for clients.
        "candidate_outgoing_damage_messages": counts["outgoing_positive"],
        "candidate_outgoing_zero_damage_messages": counts["outgoing_zero_blocked"],
        "candidate_incoming_damage_messages": counts["incoming_positive"],
        "parsed_outgoing_damage_messages": len(outgoing),
        "parsed_incoming_damage_messages": len(incoming),
        "unparsed_positive_damage_count": counts["unparsed_outgoing_positive"],
        "unparsed_zero_damage_count": counts["unparsed_outgoing_zero_blocked"],
        "unparsed_incoming_damage_count": len(unmatched_incoming),
        "unparsed_damage_count": len(unmatched_all),
        "ignored_keyword_only_lines": len(ignored),
        # The legacy unmatched keys cover outgoing damage only.
        "unmatched_candidate_count": len(unmatched_outgoing),
        "unmatched_candidate_lines": unmatched_outgoing[:300],
        "unmatched_examples": [
            {"source_line": number, "ocr_text": raw_texts[number - 1]}
            for number in unmatched_outgoing[:30]
        ],
        "unparsed_incoming_examples": [
            {"source_line": number, "ocr_text": raw_texts[number - 1]}
            for number in unmatched_incoming[:30]
        ],
    }
    return record, audit_rows
