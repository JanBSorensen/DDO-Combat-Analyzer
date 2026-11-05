"""Shared, conservative DDO outgoing-damage reassembly and OCR-number hints.

This module intentionally has no GUI/OCR imports.  Live and offline processing
must use the *same* wrap recognition, so a saved OCR log can be replayed exactly.
Only sequential continuation rows without a new Combat prefix are consumed;
partial messages are never counted as damage.
"""
from __future__ import annotations

import itertools
import re

NUM_TOKEN_WITH_B = r"[0-9SOsoIl|Bb,]+"
OUTGOING_HEAD_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN_WITH_B})(?P<tail>\s*.*?)\s*$", re.I)
OUTGOING_END_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+"
    rf"{NUM_TOKEN_WITH_B}\s+points?\s+of\s+"
    rf"(?:[A-Za-z][A-Za-z -]*?\s+)?damage\b", re.I)
COMBAT_START_RE = re.compile(
    r"^\s*(?:V\s+)?[|\[\]{}Il1:;`'\".,_\-„“”]{0,3}\s*\(?\s*Combat\s*\)?\s*:", re.I)
OUTGOING_CANDIDATE_RE = re.compile(
    r"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+", re.I)

DIGIT_MAP = str.maketrans({'S':'5','s':'5','O':'0','o':'0','I':'1','l':'1','|':'1','B':'8','b':'8'})
DDO_FORMAT_RE = re.compile(r"^\d{1,3}(?:,\d{3})*$")


def _norm(line):
    return re.sub(r"\s+", " ", str(line or '').replace('\r',' ').replace('\n',' ')).strip()


def is_complete_outgoing(line):
    return bool(OUTGOING_END_RE.search(_norm(line)))


def damage_wrap_kind(line):
    """Return stage of a partial damage row, not arbitrary line fragments.

    Known real-world wraps include `... for 34,911` + `points of ... damage`
    and `... for 297 points of` + `damage.`.  Also supports three rows.
    """
    text = _norm(line)
    if is_complete_outgoing(text):
        return None
    m = OUTGOING_HEAD_RE.search(text)
    if not m:
        return None
    tail = m.group('tail').strip()
    if not tail:
        return 'number'
    if re.fullmatch(r"points?", tail, re.I):
        return 'points'
    if re.fullmatch(r"points?\s+of", tail, re.I):
        return 'of'
    if re.fullmatch(r"points?\s+of\s+[A-Za-z][A-Za-z -]*", tail, re.I):
        return 'type'
    return None


def join_damage_wrap(prefix, continuation, kind=None):
    """Append one syntactically valid next row; result can still be partial.

    The caller must check is_complete_outgoing before committing the join.
    """
    prefix = _norm(prefix)
    raw = _norm(continuation)
    if not raw or COMBAT_START_RE.match(raw):
        return None
    cont = re.sub(r"^[|\[\]{}Il1:;`'.,_\-]+\s*", "", raw).strip()
    kind = kind or damage_wrap_kind(prefix)
    if kind == 'number':
        permitted = r"^points?\b"
    elif kind == 'points':
        permitted = r"^of\b"
    elif kind == 'of':
        # Typed and untyped damage; an incomplete type can continue to row 3.
        permitted = r"^(?:damage\b|[A-Za-z][A-Za-z -]*$|[A-Za-z][A-Za-z -]*?\s+damage\b)"
    elif kind == 'type':
        permitted = r"^damage\b"
    else:
        return None
    if not re.match(permitted, cont, re.I):
        return None
    joined = _norm(prefix + ' ' + cont)
    return joined if is_complete_outgoing(joined) or damage_wrap_kind(joined) else None


def outgoing_b_token(line):
    """Suspicious complete outgoing amount; never reads B outside the amount."""
    text = _norm(line)
    if not is_complete_outgoing(text):
        return None
    m = OUTGOING_HEAD_RE.search(text)
    if not m:
        return None
    token = m.group('amount')
    return token if re.search(r'[Bb]', token) else None


def b_number_candidates(token):
    """Unique DDO-grouped values consistent with an OCR B->8 ambiguity.

    B8,219 may mean 8,219 (double glyph) or 88,219; do not infer it.
    A fresh isolated OCR pass is required for such cases.
    """
    text = str(token or '').strip()
    if not re.fullmatch(NUM_TOKEN_WITH_B, text, re.I) or not re.search(r'[Bb]', text):
        return set()
    norm = text.translate(DIGIT_MAP)
    candidates = set()
    if DDO_FORMAT_RE.fullmatch(norm):
        candidates.add(int(norm.replace(',','')))
    else:
        positions = [i for i,ch in enumerate(norm) if ch=='1']
        for count in range(1,min(3,len(positions))+1):
            for indices in itertools.combinations(positions,count):
                chars = list(norm)
                for i in indices:
                    chars[i]=','
                corrected=''.join(chars)
                if DDO_FORMAT_RE.fullmatch(corrected):
                    candidates.add(int(corrected.replace(',','')))
    # A B and an adjacent 8 can be a duplicated OCR glyph; neither candidate
    # is safe without pixel verification. This is present in the real session.
    for idx, ch in enumerate(text):
        if ch in 'Bb':
            # If removing either the B or its adjacent 8 produces a second
            # valid grouped number, the batch OCR could have duplicated an 8.
            for pos in (idx, idx-1, idx+1):
                if 0 <= pos < len(text) and (pos == idx or text[pos] == '8'):
                    shorter = norm[:pos] + norm[pos+1:]
                    if DDO_FORMAT_RE.fullmatch(shorter):
                        return set()
    return candidates


def find_unparsed_candidates(texts, parsed_start_lines):
    seen=set(int(x) for x in parsed_start_lines)
    return [i for i,t in enumerate(texts,1)
            if OUTGOING_CANDIDATE_RE.search(_norm(t)) and i not in seen]
