"""Conservative attack-to-damage estimates from reconstructed DDO Combat Log.

DDO often prints multiple damage events per attack: weapon/physical, sneak,
imbues, procs and delayed effects. No event carries a reliable attack ID.
This module does *not* claim to measure true weapon crit damage. It reports:

* largest physical damage event following each matched attack (estimate);
* all observed damage between that attack and the next player attack, with a
  0.75 s time limit (estimate, not guaranteed to be caused by that attack);
* exact coverage denominators and an auditable event-to-attack CSV.

Sneak-tagged damage may be the dominant physical hit, so it is eligible in the
first estimate. No attack is counted as a crit without a linked and confirmed
confirmation roll. Missing/ambiguous data is never treated as zero damage.
"""

from __future__ import annotations

import bisect
from attack_crit_integrity import relink_attack_confirmations, assess_attack_integrity
import csv
from collections import defaultdict
from pathlib import Path

MAX_DAMAGE_LAG_SECONDS = 0.75
ALGORITHM_VERSION = '2026-11-08-local-confirmation-status-v2'
PHYSICAL_TYPES = frozenset((
    'pierce', 'piercing', 'slash', 'slashing', 'bludgeon', 'bludgeoning',
))
SUMMARY_FIELDS = (
    'target', 'confirmed_crits', 'matched_crits', 'avg_physical_crit',
    'noncrit_hits', 'matched_noncrit', 'avg_physical_noncrit',
    'crit_damage_ratio', 'total_linked_crits', 'avg_total_linked_crit',
    'total_linked_noncrit', 'avg_total_linked_noncrit',
    'unlinked_confirmations', 'multi_physical_crits',
    'quality_status', 'observed_confirmations', 'observed_confirmed_crits',
    'resisted_confirmations', 'unknown_confirmations',
    'resisted_attacks', 'matched_resisted', 'avg_physical_resisted',
    'failed_confirm_attacks', 'matched_failed', 'avg_physical_failed',
    'algorithm_version',
)
AUDIT_FIELDS = (
    'attack_line', 'elapsed_seconds', 'target', 'roll', 'outcome',
    'classification', 'confirmation_line', 'linked_damage_events',
    'linked_damage_total', 'physical_candidates', 'selected_physical_line',
    'selected_physical_mode', 'selected_physical_type',
    'selected_physical_damage', 'selection_note', 'linked_damage_lines',
)


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seconds(value):
    try:
        return float(value) if value not in ('', None) else None
    except (TypeError, ValueError):
        return None


def _confirmed(value):
    return str(value).strip().lower() in ('1', 'true', 'yes')


def _fmt(value):
    return '' if value is None else f'{value:.6f}'


def analyze_crit_damage(attacks, confirms, outgoing, max_lag=MAX_DAMAGE_LAG_SECONDS):
    """Analyze parsed dictionaries and return per-target summaries and audit rows.

    The first following player attack (regardless of target) closes the damage
    attribution window, preventing damage from a later attack being counted twice.
    Damage must name the same target and occur within max_lag seconds when both
    timestamps are available. Unknown/missing physical damage is excluded from
    averages, not counted as zero. A confirmed critical without a linked attack
    remains an unlinked confirmation and is excluded from damage estimates.
    """
    attacks, confirms = relink_attack_confirmations(attacks, confirms)
    integrity = assess_attack_integrity(attacks, confirms)
    ordered = sorted(
        (a for a in attacks if _integer(a.get('line')) is not None),
        key=lambda a: _integer(a['line']),
    )
    by_line = {_integer(a['line']): a for a in ordered}
    confirms_by_line = {}
    unlinked = defaultdict(int)
    for c in confirms:
        linked = _integer(c.get('linked_attack_line'))
        if linked is not None and linked in by_line and (
            str(c.get('target', '')) == str(by_line[linked].get('target', ''))
        ):
            confirms_by_line.setdefault(linked, []).append(c)
        else:
            unlinked[str(c.get('target', ''))] += 1

    boundaries = [_integer(a['line']) for a in ordered]
    assigned = defaultdict(list)
    for d in outgoing:
        dl = _integer(d.get('line'))
        amount = _integer(d.get('amount'))
        if dl is None or amount is None or amount < 0:
            continue
        if d.get('direction', 'outgoing') != 'outgoing':
            continue
        if str(d.get('source', 'You')) != 'You':
            continue
        ai = bisect.bisect_left(boundaries, dl) - 1
        if ai < 0:
            continue
        a = ordered[ai]
        if str(d.get('target', '')) != str(a.get('target', '')):
            continue
        t1, t2 = _seconds(a.get('elapsed_seconds')), _seconds(d.get('elapsed_seconds'))
        if t1 is not None and t2 is not None and (t2 < t1 - 1e-4 or t2 - t1 > max_lag):
            continue
        assigned[_integer(a['line'])].append(d)

    stats = defaultdict(lambda: {
        'confirmed_crits': 0, 'matched_crits': 0, 'physical_crit_sum': 0,
        'noncrit_hits': 0, 'matched_noncrit': 0, 'physical_noncrit_sum': 0,
        'total_linked_crits': 0, 'total_crit_sum': 0,
        'total_linked_noncrit': 0, 'total_noncrit_sum': 0,
        'multi_physical_crits': 0,
        'resisted_attacks': 0, 'matched_resisted': 0, 'physical_resisted_sum': 0,
        'failed_confirm_attacks': 0, 'matched_failed': 0, 'physical_failed_sum': 0,
    })
    audit = []
    for attack in ordered:
        line = _integer(attack['line'])
        target = str(attack.get('target', ''))
        outcome = str(attack.get('outcome', '')).lower()
        cands = confirms_by_line.get(line, [])
        # A confirmation can be attached to exactly one recent attack.  Each
        # outcome remains distinct; a resisted crit is NOT a final critical hit.
        if len(cands) > 1:
            classification = 'unresolved_confirmation'
        elif len(cands) == 1 and outcome not in ('miss', 'grazing_hit'):
            status = str(cands[0].get('outcome', '')).lower()
            classification = {
                'confirmed': 'crit', 'resisted': 'resisted',
                'failed': 'failed', 'unknown': 'unresolved_confirmation',
                'unresolved': 'unresolved_confirmation',
            }.get(status, 'unresolved_confirmation')
        elif outcome == 'hit':
            classification = 'noncrit'
        elif outcome == 'critical_threat':
            classification = 'unresolved_confirmation'
        else:
            classification = outcome or 'other'

        quality = integrity.get(target, {})
        if quality.get('status') != 'OK':
            # When attack lines are missing, even a nearby damage row cannot
            # be attributed reliably to one recognized attack.
            classification = 'unreliable_attack_log'
        damage = assigned.get(line, [])
        physical = [d for d in damage if str(d.get('damage_type', d.get('type', ''))).lower() in PHYSICAL_TYPES]
        physical.sort(key=lambda d: (-_integer(d.get('amount'), 0), _integer(d.get('line'), 0)))
        main = physical[0] if physical else None
        # If the largest two physical damage amounts tie, the primary event
        # cannot even be selected deterministically. Leave its value unknown.
        if len(physical) > 1 and _integer(physical[0]['amount']) == _integer(physical[1]['amount']):
            main = None
            note = 'tied_largest_physical'
        elif len(physical) > 1:
            note = 'largest_of_multiple_physical_estimate'
        elif len(physical) == 1:
            note = 'one_physical_event'
        else:
            note = 'no_physical_event'

        linked_total = sum(_integer(d.get('amount'), 0) for d in damage)
        if classification in ('crit', 'noncrit', 'resisted', 'failed'):
            stat = stats[target]
            is_crit = classification == 'crit'
            # Noncrit average includes ordinary hits + failed confirmations +
            # successfully confirmed but resisted crits. Separately report the
            # latter two categories so the user can inspect them individually.
            stat['confirmed_crits' if is_crit else 'noncrit_hits'] += 1
            if classification == 'resisted':
                stat['resisted_attacks'] += 1
            elif classification == 'failed':
                stat['failed_confirm_attacks'] += 1
            if main is not None:
                amount = _integer(main['amount'], 0)
                stat['matched_crits' if is_crit else 'matched_noncrit'] += 1
                stat['physical_crit_sum' if is_crit else 'physical_noncrit_sum'] += amount
                if classification in ('resisted', 'failed'):
                    stat['matched_' + classification] += 1
                    stat['physical_' + classification + '_sum'] += amount
                if is_crit and len(physical) > 1:
                    stat['multi_physical_crits'] += 1
            if damage:
                stat['total_linked_crits' if is_crit else 'total_linked_noncrit'] += 1
                stat['total_crit_sum' if is_crit else 'total_noncrit_sum'] += linked_total
        audit.append({
            'attack_line': line, 'elapsed_seconds': attack.get('elapsed_seconds', ''),
            'target': target, 'roll': attack.get('roll', ''), 'outcome': outcome,
            'classification': classification,
            'confirmation_line': _integer(cands[0].get('line'), '') if len(cands) == 1 else '',
            'linked_damage_events': len(damage), 'linked_damage_total': linked_total,
            'physical_candidates': len(physical),
            'selected_physical_line': _integer(main['line']) if main is not None else '',
            'selected_physical_mode': main.get('mode', '') if main is not None else '',
            'selected_physical_type': main.get('damage_type', main.get('type', '')) if main is not None else '',
            'selected_physical_damage': _integer(main['amount']) if main is not None else '',
            'selection_note': note if quality.get('status') == 'OK' else 'unreliable_attack_log',
            'linked_damage_lines': ';'.join(str(d.get('line', '')) for d in damage),
        })

    summaries = {}
    for target in sorted(set(stats) | set(unlinked) | set(integrity)):
        d = stats[target]
        quality = integrity.get(target, {})
        crit_avg = d['physical_crit_sum'] / d['matched_crits'] if d['matched_crits'] else None
        norm_avg = d['physical_noncrit_sum'] / d['matched_noncrit'] if d['matched_noncrit'] else None
        ratio = crit_avg / norm_avg if crit_avg is not None and norm_avg else None
        summaries[target] = {
            'target': target,
            'confirmed_crits': (d['confirmed_crits'] if quality.get('status') == 'OK'
                                else quality.get('linked_confirmed_crits', 0)),
            'matched_crits': d['matched_crits'],
            'avg_physical_crit': _fmt(crit_avg),
            'noncrit_hits': d['noncrit_hits'],
            'matched_noncrit': d['matched_noncrit'],
            'avg_physical_noncrit': _fmt(norm_avg),
            'crit_damage_ratio': _fmt(ratio),
            'total_linked_crits': d['total_linked_crits'],
            'avg_total_linked_crit': _fmt(d['total_crit_sum'] / d['total_linked_crits'] if d['total_linked_crits'] else None),
            'total_linked_noncrit': d['total_linked_noncrit'],
            'avg_total_linked_noncrit': _fmt(d['total_noncrit_sum'] / d['total_linked_noncrit'] if d['total_linked_noncrit'] else None),
            'unlinked_confirmations': unlinked[target],
            'multi_physical_crits': d['multi_physical_crits'],
            'quality_status': quality.get('status', 'UNKNOWN'),
            'observed_confirmations': quality.get('confirmations', 0),
            'observed_confirmed_crits': quality.get('confirmed_crit_messages', 0),
            'resisted_confirmations': quality.get('resisted_confirmations', 0),
            'unknown_confirmations': quality.get('unknown_confirmations', 0),
            'resisted_attacks': d['resisted_attacks'],
            'matched_resisted': d['matched_resisted'],
            'avg_physical_resisted': _fmt(d['physical_resisted_sum'] / d['matched_resisted'] if d['matched_resisted'] else None),
            'failed_confirm_attacks': d['failed_confirm_attacks'],
            'matched_failed': d['matched_failed'],
            'avg_physical_failed': _fmt(d['physical_failed_sum'] / d['matched_failed'] if d['matched_failed'] else None),
            'algorithm_version': ALGORITHM_VERSION,
        }
    return summaries, audit


def save_crit_damage(session, summaries, audit):
    """Persist estimates and all source-line matches for manual verification."""
    session = Path(session)
    with (session / 'crit_damage_by_target.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summaries.values())
    with (session / 'crit_damage_attack_audit.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
        w.writeheader()
        w.writerows(audit)


def load_or_build_crit_damage(session):
    """Old sessions can show the new stats without re-recording/reanalysis."""
    session = Path(session)
    output = session / 'crit_damage_by_target.csv'
    if output.is_file() and (session / 'crit_damage_attack_audit.csv').is_file():
        with output.open(newline='', encoding='utf-8') as f:
            prior = {row['target']: row for row in csv.DictReader(f)}
        if all(row.get('algorithm_version') == ALGORITHM_VERSION for row in prior.values()):
            return prior
    with (session / 'attack_events.csv').open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    with (session / 'damage_events.csv').open(newline='', encoding='utf-8') as f:
        outgoing = [r for r in csv.DictReader(f) if r.get('event') == 'damage_dealt']
    summaries, audit = analyze_crit_damage(
        [r for r in rows if r.get('event') == 'attack'],
        [r for r in rows if r.get('event') == 'confirm'],
        outgoing,
    )
    try:
        save_crit_damage(session, summaries, audit)
    except OSError:
        # Read-only extracted sessions can still display the calculation.
        pass
    return summaries
