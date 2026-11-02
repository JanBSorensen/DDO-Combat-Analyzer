"""Conservative DDO damage-number validation for the FFV1 and live-text parsers.

This module never changes the archived OCR text. Every correction or rejected
number is individually auditable in post_numeric_validation.csv.
"""
from __future__ import annotations

import csv
import itertools
import re
import time
from pathlib import Path

DIGIT_MAP = str.maketrans({'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1','|':'1'})
DDO_GROUPS = re.compile(r'^\d{1,3}(?:,\d{3})*$')


def cleaned(token):
    return str(token or '').translate(DIGIT_MAP).strip()


def valid(token):
    return bool(DDO_GROUPS.fullmatch(cleaned(token)))


def value(token):
    digits = cleaned(token).replace(',', '')
    return int(digits) if digits.isdigit() else None


def comma_one_candidates(token, max_replacements=3):
    s = cleaned(token)
    if not s or valid(s):
        return set()
    positions = [i for i, c in enumerate(s) if c == '1']
    out = set()
    for n in range(1, min(max_replacements, len(positions)) + 1):
        for indices in itertools.combinations(positions, n):
            chars = list(s)
            for i in indices:
                chars[i] = ','
            candidate = ''.join(chars)
            if valid(candidate):
                out.add((value(candidate), candidate))
    return out


def collapse_11_candidates(amount):
    if amount is None:
        return set()
    s = str(int(amount))
    result = {int(s[:i] + '1' + s[i+2:])
              for i in range(len(s) - 1) if s[i:i+2] == '11'}
    result.discard(int(amount))
    return result


class NumericGuard:
    def __init__(self, converter=None, crops=None, source_rows=None, trusted_live=None):
        self.converter = converter
        self.crops = crops or []
        self.source_rows = source_rows or []
        self.trusted_live = trusted_live or {}
        self.rows = []
        self.stats = {'accepted':0, 'lookalike':0, 'verified':0, 'comma_inferred':0,
                      'collapsed_11':0, 'confirmed_11':0, 'rejected':0, 'unresolved_11':0}
        self._recheck_cache = {}
        self.ocr_seconds = 0.0

    def _ocr_rechecks(self, lineno):
        if lineno in self._recheck_cache:
            return self._recheck_cache[lineno]
        output = []
        idx = int(lineno) - 1
        if self.converter is not None and 0 <= idx < len(self.crops):
            import cv2
            from process_latest import preprocess_line_indra, preprocess_line
            crop = self.crops[idx]
            variants = [preprocess_line_indra(crop), preprocess_line(crop)]
            for img in variants:
                t0 = time.perf_counter()
                try:
                    page = self.converter.ocr(img)
                    txt = ' '.join(str(getattr(z, 'text', '') or '')
                                   for z in (getattr(page, 'lines', []) or []))
                    output.append(re.sub(r'\s+', ' ', txt).strip())
                except Exception:
                    output.append('')
                self.ocr_seconds += time.perf_counter() - t0
        self._recheck_cache[lineno] = output
        return output

    @staticmethod
    def _field_token(text, field):
        from process_latest import (OUT_RE, OUT_UNTYPED_RE, IN_HIT_RE,
                                    IN_WERE_HIT_RE, IN_SUFFER_RE, IN_TAKE_RE,
                                    HEAL_TARGET_RE, YOU_HEAL_RE, IN_UNTYPED_RE, BLOCKED_AFTER_RE,
                                    ZERO_BLOCKED_OUT_RE)
        if field == 'blocked_damage':
            m = BLOCKED_AFTER_RE.search(text) or ZERO_BLOCKED_OUT_RE.search(text)
            return m.group('blocked') if m else None
        patterns = (OUT_RE, OUT_UNTYPED_RE, ZERO_BLOCKED_OUT_RE,
                    IN_HIT_RE, IN_WERE_HIT_RE, IN_SUFFER_RE, IN_TAKE_RE,
                    HEAL_TARGET_RE, YOU_HEAL_RE)
        # Isolated OCR does not always include the wrap's final 'damage'.
        if field == 'damage':
            m = re.search(r'\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+'
                          r'(?P<amount>[0-9SOsoIl|,]+)\s+points?\b', text, re.I)
            if m:
                return m.group('amount')
        for pat in patterns:
            m = pat.search(text)
            if m and 'amount' in m.groupdict() and m.groupdict().get('amount'):
                return m.group('amount')
        return None

    def resolve(self, token, field, lineno, line):
        original = str(token)
        initial = value(original)
        acceptable = valid(original)
        result = initial if acceptable else None
        action = 'accepted' if acceptable else 'rejected'
        evidence = ''
        if acceptable and original != cleaned(original):
            action = 'lookalike'
        # Live OCR's completed numeric recheck is first-class evidence for the
        # quick report, but only for the same outgoing OCR row/number field.
        trusted = self.trusted_live.get(int(lineno)) if field == 'damage' else None
        if trusted and trusted.get('result') in (
            'ddo_format_verified', 'comma_as_1_inferred', 'changed_11_to_1', 'confirmed'
        ):
            try:
                trusted_result = int(trusted['final'])
                if trusted_result >= 0 and (not acceptable or trusted_result == initial or
                                             trusted_result in collapse_11_candidates(initial)):
                    result = trusted_result
                    action = 'verified_live'
                    evidence = trusted.get('result', '')
            except (ValueError, TypeError, KeyError):
                pass
        if action != 'verified_live' and (not acceptable or collapse_11_candidates(initial)):
            alternatives = self._ocr_rechecks(lineno)
            verified = []
            for candidate_text in alternatives:
                tok = self._field_token(candidate_text, field)
                if tok is not None and valid(tok):
                    verified.append((value(tok), candidate_text))
            if not acceptable:
                if verified:
                    # The first isolated, properly punctuated read wins; it is
                    # safer evidence than a missing-comma inference.
                    result, evidence = verified[0]
                    action = 'verified'
                else:
                    inferred = comma_one_candidates(original)
                    if len(inferred) == 1:
                        result, formatted = next(iter(inferred))
                        action, evidence = 'comma_inferred', formatted
                    else:
                        result, action = None, 'rejected'
                        evidence = ' | '.join(alternatives)[:240]
            else:
                candidates = collapse_11_candidates(initial)
                match = next(((v, txt) for v, txt in verified if v in candidates), None)
                if match:
                    result, evidence = match
                    action = 'collapsed_11'
                elif candidates:
                    # The original amount can already be confirmed by a fresh
                    # OCR read.  A doubled '11' is only a *possibility*, not a
                    # reason to mark that confirmed number as unresolved.
                    same = next(((v, txt) for v, txt in verified if v == initial), None)
                    if same:
                        action = 'confirmed_11'
                        evidence = same[1]
                    else:
                        action = 'unresolved_11'
                        evidence = ' | '.join(alternatives)[:240]

        self.stats[action] = self.stats.get(action, 0) + 1
        if result is None or result != initial or action not in ('accepted',):
            self.rows.append({
                'source_line': lineno, 'field':field, 'original_token':original,
                'normalized_token':cleaned(original), 'initial_value':initial,
                'final_value':result if result is not None else '',
                'action':action, 'recheck_evidence':evidence, 'ocr_line':line,
            })
        # Return same shape as parse_ocr_number (value/raw/corrected/changed).
        return result, original, cleaned(original), (result is not None and (result != initial or original != cleaned(original)))

    def save(self, path):
        columns = ['source_line','field','original_token','normalized_token',
                   'initial_value','final_value','action','recheck_evidence','ocr_line']
        with Path(path).open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(self.rows)
