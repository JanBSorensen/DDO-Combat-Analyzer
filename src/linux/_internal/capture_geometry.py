"""Unambiguous (screen-coordinate) Combat Log capture geometry validation.

An FFV1 recording and the persistent live helper are separate processes. Both
must use the same four coordinates; checking only the PNG dimensions is not
sufficient because a shifted rectangle can have the same size.
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ('left', 'top', 'width', 'height')


def rectangle(value):
    if not isinstance(value, dict):
        raise ValueError('Capture region must be a dictionary')
    result = {key: int(value[key]) for key in FIELDS}
    if result['width'] <= 0 or result['height'] <= 0:
        raise ValueError('Capture width and height must be positive')
    return result


def session_rectangle(session):
    session = Path(session)
    snapshot = json.loads((session / 'config_used.json').read_text(encoding='utf-8'))
    return rectangle(snapshot['capture'])


def comparison(expected, actual, observed_size=None):
    expected = rectangle(expected)
    actual = rectangle(actual)
    matched = (expected == actual)
    result = {'expected_recorder_capture': expected,
              'actual_live_capture': actual,
              'coordinates_match': matched}
    if observed_size is not None:
        width, height = (int(x) for x in observed_size)
        result['observed_live_width'] = width
        result['observed_live_height'] = height
        result['dimensions_match'] = (width == expected['width'] and
                                      height == expected['height'])
        matched = matched and result['dimensions_match']
    result['match'] = bool(matched)
    return result


def write_comparison(session, expected, actual, observed_size=None, stage=''):
    result = comparison(expected, actual, observed_size)
    result['stage'] = stage
    target = Path(session) / 'capture_geometry_comparison.json'
    temp = target.with_suffix('.json.tmp')
    temp.write_text(json.dumps(result, indent=2), encoding='utf-8')
    temp.replace(target)
    return result
