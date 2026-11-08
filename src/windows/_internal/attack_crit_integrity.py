"""Safer Combat Log attack and confirmation reconstruction.

Facts, estimates and unknowns must remain distinct.  In particular, a
confirmation is not evidence that a missing ``You attack`` line was captured,
and two confirmations can never be assigned to the same observed attack.
No DDO game memory or internal data is inspected.
"""
from __future__ import annotations

from collections import defaultdict
import re

MAX_CONFIRM_LAG = 0.75  # seconds between two reconstructed OCR event timestamps
MAX_CONFIRM_LINES = 24  # fall-back/secondary guard when timestamps are sparse
MAX_WRAP_LINES = 4
MAX_WRAP_LAG = 0.75

ATTACK_START = re.compile(r'\bYou\s+attack\s+', re.IGNORECASE)
CONFIRM_START = re.compile(r'\bYou\s+roll\s+to\s+confirm\s+a\s+critical\s+on\s+', re.IGNORECASE)
NEW_COMBAT = re.compile(r'^\s*[|\[\]{}Il1:;`\'".,_\-„“”]{0,4}\s*\(?Combat\)?\s*:', re.IGNORECASE)
LIVE_ATTACK_START = re.compile(
    r'^\s*[|\[\]{}Il1:;`\'".,_\-„“”]{0,4}\s*\(?Combat\)?\s*:\s*You\s+attack\s+',
    re.IGNORECASE,
)


def _number(value, default=None):
    try:
        return int(value) if value not in ('', None) else default
    except (TypeError, ValueError):
        return default


def _time(value):
    try:
        return float(value) if value not in ('', None) else None
    except (TypeError, ValueError):
        return None


def confirmation_result(text, previous=''):
    """A resisted crit is NOT a successful critical hit.

    A resisted result is evidence of a successful confirmation that was
    negated by a separate resistance/fortification check. It must not be
    counted as a final critical hit or as a failed confirmation roll.
    """
    result = str(text or '').lower()
    if re.search(r'\bcritical\s+hit\s+resisted\b', result) or (
        'critical hit' in result and 'resist' in result
    ):
        return 'resisted'
    if re.search(r'\bcritical\s+hit\b', result):
        return 'confirmed'
    if re.search(r'\b(fail(?:ure|ed)?|miss|not\s+confirmed)\b', result):
        return 'failed'
    if not result and previous in ('confirmed', 'failed', 'resisted', 'unknown', 'unresolved'):
        return 'unknown' if previous == 'unresolved' else previous
    return 'unknown'


def attack_result(text):
    result = str(text or '').lower()
    if 'grazing hit' in result:
        return 'grazing_hit'
    if 'critical hit' in result:
        return 'critical_threat'
    if re.search(r'\byou\s+miss\b', result):
        return 'miss'
    if re.search(r'\byou\s+hit\b', result):
        return 'hit'
    return 'other'


def _stitched(texts, idx, times, attack_re, confirm_re):
    """Join a roll's continuation rows only before the next Combat message.

    Other damage parsing is independent and still sees the ORIGINAL rows.
    Never merge across a new Combat header, long capture gaps or >4 rows.
    """
    first = str(texts[idx] or '')
    initial_attack = attack_re.search(first)
    initial_confirm = confirm_re.search(first)
    if initial_attack and attack_result(initial_attack.group('result')) != 'other':
        return first
    if initial_confirm and confirmation_result(initial_confirm.group('result')) != 'unknown':
        return first
    if not (ATTACK_START.search(first) or CONFIRM_START.search(first)):
        return first
    pieces = [first]
    t0 = _time(times[idx]) if times is not None and idx < len(times) else None
    for j in range(idx + 1, min(len(texts), idx + MAX_WRAP_LINES)):
        next_text = str(texts[j] or '').strip()
        if NEW_COMBAT.match(next_text):
            break
        # Indra frequently adds a left-edge '|' to visual continuation
        # rows, e.g. '|(+155): you hit!' or '|You roll a 13...'.
        next_text = re.sub(r'^\s*[|\[\]{}\\]+', '', next_text).strip()
        tj = _time(times[j]) if times is not None and j < len(times) else None
        if t0 is not None and tj is not None and not (-0.0001 <= tj - t0 <= MAX_WRAP_LAG):
            break
        pieces.append(next_text)
        joined = ' '.join(pieces)
        am = attack_re.search(joined)
        cm = confirm_re.search(joined)
        if (am and attack_result(am.group('result')) != 'other') or (
            cm and confirmation_result(cm.group('result')) != 'unknown'
        ):
            return joined
    return first


def parse_and_link_attack_crit_events(texts, line_times=None, line_numbers=None, *,
                                      attack_re, confirm_re, parse_number,
                                      normalize_text, canonical_target):
    """Parse all recognized (including wrapped) attack/confirmation messages.

    Incomplete attack starts are retained as unknown rather than silently
    omitted; they do not contribute a d20 outcome or hit/miss percentage.
    """
    attacks, confirmations = [], []
    for i, source_text in enumerate(texts):
        row = _stitched(texts, i, line_times, attack_re, confirm_re)
        line = line_numbers[i] if line_numbers is not None and i < len(line_numbers) else i + 1
        elapsed = line_times[i] if line_times is not None and i < len(line_times) else None
        m = attack_re.search(row)
        if m:
            roll = parse_number(m.group('roll'))[0]
            bonus = parse_number(m.group('bonus'))[0] if m.group('bonus') else None
            target = canonical_target(m.group('target'))
            outcome = attack_result(normalize_text(m.group('result')))
            attacks.append({
                'line': line, 'elapsed_seconds': elapsed, 'event': 'attack',
                'target': target, 'roll': roll, 'bonus': bonus,
                'total': (roll + bonus) if roll is not None and bonus is not None else None,
                'outcome': outcome, 'confirmed': '', 'linked_attack_line': '',
                'text': row,
            })
            continue
        # Safe partial attack: recognizable Combat message start with a target
        # and a terminating dot, but its roll/result was not reconstructed.
        if LIVE_ATTACK_START.search(row):
            partial = re.search(r'\bYou\s+attack\s+(?P<target>.+?)\.', row, re.IGNORECASE)
            if partial:
                attacks.append({
                    'line': line, 'elapsed_seconds': elapsed, 'event': 'attack',
                    'target': canonical_target(partial.group('target')),
                    'roll': None, 'bonus': None, 'total': None,
                    'outcome': 'other', 'confirmed': '', 'linked_attack_line': '',
                    'text': row,
                })
                continue
        m = confirm_re.search(row)
        if m:
            roll = parse_number(m.group('roll'))[0]
            bonus = parse_number(m.group('bonus'))[0] if m.group('bonus') else None
            outcome = confirmation_result(normalize_text(m.group('result')))
            confirmations.append({
                'line': line, 'elapsed_seconds': elapsed, 'event': 'confirm',
                'target': canonical_target(m.group('target')), 'roll': roll,
                'bonus': bonus,
                'total': (roll + bonus) if roll is not None and bonus is not None else None,
                'outcome': outcome, 'confirmed': int(outcome == 'confirmed'),
                'linked_attack_line': '', 'text': row,
            })
            continue
        # The confirmation message is visible but its result was clipped or
        # unreadable. Preserve the attempt as UNKNOWN instead of silently
        # dropping it or guessing based on the natural d20 value.
        if CONFIRM_START.search(row):
            partial_target = re.search(
                r'\bYou\s+roll\s+to\s+confirm\s+a\s+critical\s+on\s+'
                r'(?P<target>.+?)[.,](?=\s|$)', row, re.IGNORECASE)
            if not partial_target:
                continue
            remainder = row[partial_target.end():].strip()
            # A truncated valid confirmation can end at the target dot/comma
            # or in its own subsequent roll; do not accept malformed messages
            # such as 'Karliath You roll ...', 'Karliath: ...' or 'You attack'.
            if remainder and not re.match(r'^You\s+roll\s+a\b', remainder, re.IGNORECASE):
                continue
            roll_match = re.search(r'\bYou\s+roll\s+a\s+(?P<roll>\d{1,2})'
                                   r'(?:\s*\(\+(?P<bonus>\d+)\))?', remainder, re.IGNORECASE)
            roll = parse_number(roll_match.group('roll'))[0] if roll_match else None
            bonus = (parse_number(roll_match.group('bonus'))[0]
                     if roll_match and roll_match.group('bonus') else None)
            confirmations.append({
                'line': line, 'elapsed_seconds': elapsed, 'event': 'confirm',
                'target': canonical_target(partial_target.group('target'))
                          if partial_target else 'Unknown',
                'roll': roll, 'bonus': bonus,
                'total': roll + bonus if roll is not None and bonus is not None else None,
                'outcome': 'unknown', 'confirmed': 0,
                'linked_attack_line': '', 'text': row,
            })
    return relink_attack_confirmations(attacks, confirmations)


def relink_attack_confirmations(attacks, confirmations):
    """Revalidate even OLD session CSVs using a recent 1-to-1 temporal link.

    Never trust old ``linked_attack_line`` values: previous builds reused an
    attack from up to 21.6 seconds earlier.  Never reuse a consumed attack.
    """
    attacks = [dict(a) for a in attacks]
    confirmations = [dict(c) for c in confirmations]
    for a in attacks:
        # Existing session rows can have been mutated to threat by the old
        # unbounded linker. Recover the printed attack result before relinking.
        a['outcome'] = attack_result(a.get('text', '')) if a.get('text') else str(a.get('outcome', 'other'))
        a['linked_attack_line'] = ''
    for c in confirmations:
        c['outcome'] = confirmation_result(c.get('text'), c.get('outcome'))
        c['confirmed'] = 1 if c['outcome'] == 'confirmed' else 0
        c['linked_attack_line'] = ''
        c['link_status'] = 'no_recent_attack'
    ordered = sorted(attacks + confirmations, key=lambda e: (_number(e.get('line'), 10**12),
                                                            0 if e.get('event') == 'attack' else 1))
    last_by_target = {}
    consumed = set()
    for row in ordered:
        target = str(row.get('target', ''))
        if row.get('event') == 'attack':
            last_by_target[target] = row
            continue
        prior = last_by_target.get(target)
        if prior is None:
            continue
        a_line = _number(prior.get('line'))
        c_line = _number(row.get('line'))
        if a_line is None or c_line is None or not (a_line < c_line <= a_line + MAX_CONFIRM_LINES):
            continue
        ta, tc = _time(prior.get('elapsed_seconds')), _time(row.get('elapsed_seconds'))
        if ta is not None and tc is not None and not (-0.0001 <= tc - ta <= MAX_CONFIRM_LAG):
            continue
        if a_line in consumed:
            row['link_status'] = 'attack_already_confirmed'
            continue
        if prior.get('outcome') in ('miss', 'grazing_hit'):
            row['link_status'] = 'attack_is_miss_or_graze'
            continue
        consumed.add(a_line)
        row['linked_attack_line'] = a_line
        row['link_status'] = 'linked'
        prior['outcome'] = 'critical_threat'
    return attacks, confirmations


def assess_attack_integrity(attacks, confirms):
    """Report OBSERVED values only: confirmations do not synthesize attacks."""
    stats = defaultdict(lambda: {
        'attacks': 0, 'confirmations': 0, 'linked_confirmations': 0,
        'unlinked_confirmations': 0, 'confirmed_crit_messages': 0,
        'linked_confirmed_crits': 0, 'resisted_confirmations': 0,
        'unknown_confirmations': 0, 'unresolved_confirmations': 0,
    })
    for a in attacks:
        stats[str(a.get('target', ''))]['attacks'] += 1
    for c in confirms:
        d = stats[str(c.get('target', ''))]
        d['confirmations'] += 1
        if c.get('linked_attack_line') not in ('', None, 0, '0'):
            d['linked_confirmations'] += 1
            if c.get('outcome') == 'confirmed':
                d['linked_confirmed_crits'] += 1
        else:
            d['unlinked_confirmations'] += 1
        if c.get('outcome') == 'confirmed':
            d['confirmed_crit_messages'] += 1
        elif c.get('outcome') == 'resisted':
            d['resisted_confirmations'] += 1
        elif c.get('outcome') == 'unknown':
            d['unknown_confirmations'] += 1
            d['unresolved_confirmations'] += 1
    for d in stats.values():
        severe = d['confirmations'] > d['attacks']
        incomplete = severe or bool(d['unlinked_confirmations']) or bool(d['unknown_confirmations'])
        d['status'] = ('INVALID_ATTACK_DENOMINATOR' if severe else
                       'INCOMPLETE_CONFIRMATION_LINKS' if incomplete else 'OK')
        d['crit_rate_valid'] = not incomplete and d['attacks'] > 0
        d['apm_reliable'] = not severe and not bool(d['unlinked_confirmations'])
    return dict(stats)


def live_attack_start(line):
    """Count an attack from its start row even if its roll/result wraps."""
    return LIVE_ATTACK_START.search(str(line or '')) is not None
