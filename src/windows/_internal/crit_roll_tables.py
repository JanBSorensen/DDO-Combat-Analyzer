"""Separate observed attack d20 and critical-confirmation d20 distributions.

The Combat Log does not always provide every attack-roll line.  These tables
are OBSERVATIONS, not inferred attack counts.  A confirmation attempt is never
silently turned into a synthetic attack.  Natural 1/20 outcomes are shown as
logged, while threshold analysis excludes their automatic d20 outcomes.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CONFIRM_RESULTS = ('confirmed', 'resisted', 'failed', 'unknown')
ATTACK_RESULTS = ('miss', 'grazing_hit', 'hit', 'critical_threat', 'other')


def _roll(value):
    try:
        v = int(value)
    except (ValueError, TypeError):
        return None
    return v if 1 <= v <= 20 else None


def _result(item):
    outcome = str(item.get('outcome', 'unknown') or 'unknown').lower()
    if outcome == 'unresolved':
        return 'unknown'
    return outcome if outcome in CONFIRM_RESULTS else 'unknown'


def _targets(rows):
    return sorted({str(r.get('target', '') or 'Unknown') for r in rows}, key=str.casefold)


def _build(rows, kinds, kind_selector):
    """Return 1..20 plus optional ? and TOTAL rows for EACH observed target."""
    result = []
    for target in _targets(rows):
        target_rows = [r for r in rows if str(r.get('target', '') or 'Unknown') == target]
        counts = {n: {key: 0 for key in kinds} for n in range(1, 21)}
        missing = {key: 0 for key in kinds}
        for r in target_rows:
            roll = _roll(r.get('roll'))
            outcome = kind_selector(r)
            key = outcome if outcome in kinds else kinds[-1]
            (counts[roll] if roll is not None else missing)[key] += 1
        for roll in range(1, 21):
            d = counts[roll]
            result.append({'target': target, 'roll': str(roll), **d, 'total': sum(d.values())})
        if sum(missing.values()):
            result.append({'target': target, 'roll': '?', **missing, 'total': sum(missing.values())})
        totals = {key: sum(r[key] for r in result if r['target'] == target) for key in kinds}
        result.append({'target': target, 'roll': 'TOTAL', **totals, 'total': sum(totals.values())})
    return result


def attack_d20_rows(attacks):
    """Each attack has exactly one outcome column. No confirmation columns."""
    return _build(attacks, ATTACK_RESULTS,
                  lambda r: str(r.get('outcome', 'other') or 'other').lower())


def confirmation_d20_rows(confirms):
    """Each confirmation has exactly one observed result, including 1 and 20."""
    return _build(confirms, CONFIRM_RESULTS, _result)


def confirmation_summary(confirms, target=None):
    """Success includes resisted, but actual effective crits exclude resisted.

    Unknown outcomes are shown in attempts but excluded from the denominators of
    all success/resist percentages.  Percentages may be None when no evidence.
    """
    rows = [c for c in confirms if target is None or str(c.get('target', '')) == target]
    counts = {key: 0 for key in CONFIRM_RESULTS}
    attempts_2_19 = 0
    success_2_19 = 0
    failed_2_19 = 0
    for c in rows:
        outcome = _result(c)
        counts[outcome] += 1
        if _roll(c.get('roll')) in range(2, 20):
            if outcome in ('confirmed', 'resisted', 'failed'):
                attempts_2_19 += 1
            if outcome in ('confirmed', 'resisted'):
                success_2_19 += 1
            elif outcome == 'failed':
                failed_2_19 += 1
    attempts = len(rows)
    known = counts['confirmed'] + counts['resisted'] + counts['failed']
    successes = counts['confirmed'] + counts['resisted']
    return {
        **counts,
        'attempts': attempts,
        'known_attempts': known,
        'successful_confirmations': successes,
        'confirmation_success_percent': (100.0 * successes / known if known else None),
        'resisted_after_success_percent': (100.0 * counts['resisted'] / successes if successes else None),
        'effective_crit_per_confirmation_percent': (100.0 * counts['confirmed'] / known if known else None),
        'known_attempts_2_19': attempts_2_19,
        'successes_2_19': success_2_19,
        'failed_2_19': failed_2_19,
        'confirmation_success_percent_2_19': (100.0 * success_2_19 / attempts_2_19 if attempts_2_19 else None),
    }


def write_d20_distributions(session, attacks, confirms):
    """Write audit-friendly CSV; never replace legacy attack_roll_distribution.csv."""
    session = Path(session)
    files = (
        (session / 'attack_d20_distribution.csv', attack_d20_rows(attacks), ATTACK_RESULTS),
        (session / 'confirmation_d20_distribution.csv', confirmation_d20_rows(confirms), CONFIRM_RESULTS),
    )
    for path, rows, kinds in files:
        with path.open('w', newline='', encoding='utf-8') as stream:
            w = csv.DictWriter(stream, fieldnames=('target', 'roll', *kinds, 'total'))
            w.writeheader()
            w.writerows(rows)
    return tuple(path for path, _, _ in files)


def observed_confirmation_threshold(confirms, target=None):
    """Observe total-roll lower/upper evidence from d20=2..19 only.

    Both confirmed and resisted are successful confirmation rolls. Natural
    1/20 and unknown results do NOT establish an ordinary roll threshold.
    """
    rows = [c for c in confirms if target is None or str(c.get('target', '')) == target]
    good, bad = [], []
    for c in rows:
        if _roll(c.get('roll')) not in range(2, 20):
            continue
        try:
            total = int(c['total'])
        except (KeyError, TypeError, ValueError):
            continue
        if _result(c) in ('confirmed', 'resisted'):
            good.append(total)
        elif _result(c) == 'failed':
            bad.append(total)
    highest_failed = max(bad) if bad else None
    lowest_success = min(good) if good else None
    if highest_failed is not None and lowest_success is not None:
        lower, upper = highest_failed + 1, lowest_success
        label = (str(lower) if lower == upper else
                 f'{lower}-{upper}' if lower < upper else
                 f'varies ({highest_failed} fail / {lowest_success} success)')
    elif lowest_success is not None:
        label = f'<= {lowest_success}'
    elif highest_failed is not None:
        label = f'> {highest_failed}'
    else:
        label = 'N/A'
    return highest_failed, lowest_success, label
