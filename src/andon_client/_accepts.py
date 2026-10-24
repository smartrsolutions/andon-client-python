"""
What a tile takes, read off the tile itself: its kind, whether it has
categories, how many series or lanes it has, and which form its first series
uses - a series in the step form (start, stepSec, points) takes no single number,
because there is no timestamp to append it at.

The hint a refused value gets, the same sentence as in the TypeScript package.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Mapping, NamedTuple, Optional

from ._js import is_number, iso
from ._words import API_WORDS, Words


class Form(NamedTuple):
    key: str
    """names the wording"""
    params: Dict[str, Any]
    """what the wording needs (count, states)"""
    example: Any
    """a value to show"""
    extra: Dict[str, Any]
    """further parts of the update the form needs, by name: timestamp, series, lane"""


def _text(w: Words) -> Dict[str, Dict[str, str]]:
    return {
        'text': {'payload': 'a text or a number', 'effect': 'replaces the value'},
        'number': {'payload': 'a number', 'effect': 'replaces the value'},
        'append': {'payload': 'a number', 'effect': 'is appended to the series, at the time it arrives'},
        'appendAt': {'payload': 'a number with ' + w.timestamp, 'effect': 'is appended at that time, in milliseconds or ISO 8601'},
        'appendTo': {'payload': 'a number with ' + w.series, 'effect': 'is appended to the series of that name'},
        'pairs': {'payload': 'an array of [timestamp, value] pairs', 'effect': 'replaces the series; ' + w.series + ' picks which'},
        'samples': {'payload': 'an object {series: [{name, samples}]}', 'effect': 'replaces every series'},
        'steps': {'payload': 'an object {series: [{name, start, stepSec, points}]}', 'effect': 'replaces every series: a point every stepSec from start, in UTC'},
        'values': {'payload': 'an array of numbers, one per category', 'effect': 'replaces the values; categories: __count__'},
        'valuesTo': {'payload': 'an array of numbers with ' + w.series, 'effect': 'replaces the values of the series of that name'},
        'categories': {'payload': 'an object {categories, series}', 'effect': 'replaces the categories and their values together'},
        'statuses': {'payload': 'an object {series: [{name, values, statuses}]}', 'effect': 'a verdict per bar: ok, warning, critical or null'},
        'rows': {'payload': 'an array of rows, each an array of cells', 'effect': 'replaces the rows; columns: __count__'},
        'rowStatus': {'payload': 'an array of {cells, status}', 'effect': 'replaces the rows, with a status per row'},
        'state': {'payload': 'a state ID (__states__)', 'effect': 'starts a new segment, at the time it arrives'},
        'stateAt': {'payload': 'a state ID with ' + w.timestamp, 'effect': 'starts the segment at that time'},
        'stateLane': {'payload': 'a state ID with ' + w.lane, 'effect': 'starts a segment in the lane of that name'},
        'window': {'payload': 'an object {from, to}', 'effect': 'moves the time axis; what lies before from is trimmed'},
        'fields': {'payload': 'an object of fields, e.g. {"status": "warning"}', 'effect': 'sets them; null removes one'},
    }


# The forms an error message names: what a value alone can be. Objects and forms
# that need a further part of the update are left to the documentation.
_IN_HINT = {'text', 'number', 'append', 'pairs', 'values', 'rows', 'state'}

_MINUTE = 60 * 1000
_HOUR = 60 * _MINUTE


def _form(key: str, example: Any, params: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> Form:
    return Form(key, params or {}, example, extra or {})


def _number_of(tile: Mapping[str, Any], fallback: float) -> Any:
    return tile.get('value') if is_number(tile.get('value')) else fallback


def _series_of(tile: Mapping[str, Any]) -> List[Any]:
    series = tile.get('series')
    return [s for s in series if isinstance(s, dict) and isinstance(s.get('name'), str)] if isinstance(series, list) else []


def _values_of(series: Mapping[str, Any], fallback: Any) -> List[Any]:
    """The last few values a series holds, whatever its form: what the examples are made of."""
    v: List[Any] = []
    if isinstance(series.get('samples'), list):
        v = [p[1] if isinstance(p, list) and len(p) > 1 else None for p in series['samples']]
    elif isinstance(series.get('points'), list):
        v = series['points']
    v = [x for x in v if is_number(x)][-4:]
    return v if v else [fallback, fallback + 1, fallback - 1]


def _pairs_of(values: List[Any], now: float) -> List[Any]:
    """[timestamp, value] pairs one minute apart, the last one now."""
    n = len(values) - 1
    return [[iso(now - (n - i) * _MINUTE), v] for i, v in enumerate(values)]


def _time_series_forms(tile: Mapping[str, Any], now: float) -> List[Form]:
    every = _series_of(tile)
    if not every:
        return []
    vals = [_values_of(s, _number_of(tile, 42)) for s in every]
    last = vals[0][-1]
    out: List[Form] = []
    if not isinstance(every[0].get('points'), list):
        out += [_form('append', last), _form('appendAt', last, {}, {'timestamp': {'example': iso(now)}})]
        if len(every) > 1:
            out.append(_form('appendTo', vals[1][-1], {}, {'series': {'example': every[1]['name']}}))

    def step(s: Mapping[str, Any]) -> Any:
        return s['stepSec'] if is_number(s.get('stepSec')) and s['stepSec'] > 0 else 3600

    out += [
        _form('pairs', _pairs_of(vals[0], now)),
        _form('samples', {'series': [{'name': s['name'], 'samples': _pairs_of(vals[k], now)} for k, s in enumerate(every)]}),
        # The step form with its last point at now.
        _form('steps', {'series': [{'name': s['name'], 'start': iso(now - (len(vals[k]) - 1) * step(s) * 1000),
                                    'stepSec': step(s), 'points': vals[k]} for k, s in enumerate(every)]}),
    ]
    return out


def _category_forms(tile: Mapping[str, Any], kind: str) -> List[Form]:
    count = len(tile['categories'])
    every = _series_of(tile)

    def values_for(s: Optional[Mapping[str, Any]]) -> List[Any]:
        if s and isinstance(s.get('values'), list) and len(s['values']) == count:
            return s['values']
        return [i + 1 for i in range(count)]

    out = [_form('values', values_for(every[0] if every else None), {'count': count})]
    if len(every) > 1:
        out.append(_form('valuesTo', values_for(every[1]), {}, {'series': {'example': every[1]['name']}}))
    out.append(_form('categories', {
        'categories': tile['categories'],
        'series': [{'name': s['name'], 'values': values_for(s)} for s in (every or [{'name': 'A'}])],
    }))
    # A verdict per bar exists only on a bar with categories and exactly one series.
    if kind == 'bar' and len(every) <= 1:
        v = values_for(every[0] if every else None)
        out.append(_form('statuses', {'series': [{'name': every[0]['name'] if every else 'A', 'values': v,
                                                  'statuses': ['critical' if i == len(v) - 1 else None for i in range(len(v))]}]}))
    return out


def _table_forms(tile: Mapping[str, Any]) -> List[Form]:
    columns = len(tile['columns']) if isinstance(tile.get('columns'), list) and tile['columns'] else 1
    rows = tile.get('rows')
    first = rows[0].get('cells') if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    row = first if isinstance(first, list) and len(first) == columns else ['A' if i == 0 else i for i in range(columns)]
    return [_form('rows', [row], {'count': columns}), _form('rowStatus', [{'cells': row, 'status': 'warning'}])]


def _timeline_forms(tile: Mapping[str, Any], now: float) -> List[Form]:
    states = [s.get('id') for s in (tile.get('states') if isinstance(tile.get('states'), list) else [])
              if isinstance(s, dict) and s.get('id')]
    if not states:
        return []
    lanes = [lane for lane in (tile.get('lanes') if isinstance(tile.get('lanes'), list) else [])
             if isinstance(lane, dict) and isinstance(lane.get('name'), str)]
    out = [
        _form('state', states[0], {'states': ', '.join(str(s) for s in states)}),
        _form('stateAt', states[0], {}, {'timestamp': {'example': iso(now)}}),
    ]
    if len(lanes) > 1:
        out.append(_form('stateLane', states[0], {}, {'lane': {'example': lanes[1]['name']}}))
    out.append(_form('window', {'from': iso(now - 8 * _HOUR), 'to': iso(now)}))
    return out


def forms(tile: Any, now: Optional[float] = None) -> List[Form]:
    """forms returns every form the tile takes, the most common first."""
    if not isinstance(tile, dict):
        return []
    at = now if is_number(now) else time.time() * 1000
    categories = isinstance(tile.get('categories'), list)
    kind = tile.get('view')
    if kind == 'label':
        value = tile.get('value')
        out = [_form('text', value if is_number(value) or isinstance(value, str) else 'Running')]
    elif kind in ('gauge', 'progress'):
        out = [_form('number', _number_of(tile, 0))]
    elif kind == 'donut':
        out = [_form('number', _number_of(tile, 0))] + (_category_forms(tile, 'donut') if categories else [])
    elif kind == 'pie':
        out = _category_forms(tile, 'pie') if categories else []
    elif kind == 'bar':
        out = _category_forms(tile, 'bar') if categories else _time_series_forms(tile, at)
    elif kind == 'line':
        out = _time_series_forms(tile, at)
    elif kind == 'table':
        out = _table_forms(tile)
    elif kind == 'timeline':
        out = _timeline_forms(tile, at)
    else:
        return []
    if not out:
        return []
    out.append(_form('fields', {'status': 'warning'}))
    return out


def _fill(s: str, params: Mapping[str, Any]) -> str:
    return re.sub(r'__(\w+)__', lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), s)


def describe(f: Form, words: Words = API_WORDS) -> str:
    """describe is the English wording of one form: "a number (replaces the value)"."""
    t = _text(words)[f.key]
    return _fill(t['payload'], f.params) + ' (' + _fill(t['effect'], f.params) + ')'


def hint(tile: Any, words: Words = API_WORDS) -> str:
    """
    hint is one English sentence for an error message: what a value for the tile
    can be. A refused value is the moment someone needs it. Objects and forms that
    need a further part of the update are left out.
    """
    if not tile:
        return ''
    every = [describe(f, words) for f in forms(tile) if f.key in _IN_HINT]
    if not every:
        return ''
    kind = str(tile.get('view')) + (' with categories' if isinstance(tile.get('categories'), list) else '')
    last = every.pop()
    return 'A ' + kind + ' takes ' + (', '.join(every) + ' or ' if every else '') + last
