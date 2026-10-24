"""
Checks a view document before it is sealed: the schema (schema/view-v1.schema.json)
plus the rules the schema cannot express. The rules are those of the Andon
configurator, so a document the configurator rejects is rejected here, and the
other way round - except for the size limit, which is stricter here (see
_semantic_issues).

The TypeScript package checks with Ajv, this one with jsonschema. The two report
a fault in different shapes, so the schema's findings are put into Ajv's here: the
same path, the same words, in the same order, which is what a refusal shows.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from importlib import resources
from typing import Any, Dict, Iterable, List, Optional, Tuple

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from ._js import WS_CLASS, is_number, parse_date, stringify, to_string, utf16_len
from ._seal import MAX_ENVELOPE


@dataclass(frozen=True)
class Issue:
    """One finding. `path` is a JSON pointer into the document."""
    level: str
    """error or warning"""
    path: str
    message: str


@dataclass(frozen=True)
class Validation:
    errors: List[Issue]
    """the document must not be sent"""
    warnings: List[Issue]
    """it may be sent, but something looks off"""


SCHEMA: Dict[str, Any] = json.loads(resources.files(__package__).joinpath('schema/view-v1.schema.json').read_text('utf-8')) \
    if hasattr(resources, 'files') else json.loads(resources.read_text(__package__ + '.schema', 'view-v1.schema.json', 'utf-8'))

# The formats as ajv-formats checks them in its full mode: date-time with a time
# zone, a real day of the month, a leap second only at the end of a UTC minute.
_DATE = re.compile(r'(\d\d\d\d)-(\d\d)-(\d\d)', re.ASCII)
_TIME = re.compile(r'(\d\d):(\d\d):(\d\d(?:\.\d+)?)(z|([+-])(\d\d)(?::?(\d\d))?)?', re.ASCII | re.IGNORECASE)
_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
_UUID = re.compile(r'(?:urn:uuid:)?[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', re.IGNORECASE)


def _date(text: str) -> bool:
    m = _DATE.fullmatch(text)
    if not m:
        return False
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 1 <= month <= 12 and 1 <= day <= (29 if month == 2 and leap else _DAYS[month])


def _time(text: str) -> bool:
    m = _TIME.fullmatch(text)
    if not m:
        return False
    hr, mi, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
    sign = -1 if m.group(5) == '-' else 1
    tz_h, tz_m = int(m.group(6) or 0), int(m.group(7) or 0)
    if tz_h > 23 or tz_m > 59 or not m.group(4):
        return False
    if hr <= 23 and mi <= 59 and sec < 60:
        return True
    utc_min = mi - tz_m * sign
    utc_hr = hr - tz_h * sign - (1 if utc_min < 0 else 0)
    return utc_hr in (23, -1) and utc_min in (59, -1) and sec < 61


_SEPARATOR = re.compile('[tT]|' + WS_CLASS)

_formats = FormatChecker(formats=())


@_formats.checks('date-time')
def _date_time(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    parts = _SEPARATOR.split(value)
    return len(parts) == 2 and _date(parts[0]) and _time(parts[1])


@_formats.checks('uuid')
def _uuid(value: Any) -> bool:
    return not isinstance(value, str) or _UUID.fullmatch(value) is not None


def _pattern(validator, pattern: str, instance: Any, schema: Dict[str, Any]):
    # As JavaScript reads a pattern: `$` is the end of the text, never the place
    # before a final line break, which Python's `$` also matches - "t_oee\n" is no ID.
    if isinstance(instance, str) and not re.search(re.sub(r'(?<!\\)\$', r'\\Z', pattern), instance):
        yield jsonschema.ValidationError('does not match ' + pattern)


_Validator = jsonschema.validators.extend(Draft202012Validator, {'pattern': _pattern})
_validator = _Validator(SCHEMA, format_checker=_formats)

# The quick check. A tile's schema does not look at the other tiles, so a document
# is valid when its top level is (the tiles left out) and each tile is; and most
# tiles have been seen before, unchanged. Only a document that fails gets the full
# check, whose findings are what a refusal says. jsonschema is a hundred times
# slower than Ajv, and a source may send many values a second.
_top = _Validator(dict(SCHEMA, properties=dict(SCHEMA['properties'], tiles=dict(SCHEMA['properties']['tiles'], items=True))),
                  format_checker=_formats)
_tile = _Validator({'$schema': SCHEMA['$schema'], '$id': SCHEMA['$id'], '$defs': SCHEMA['$defs'], '$ref': '#/$defs/tile'},
                   format_checker=_formats)
_valid_tiles: 'OrderedDict[str, bool]' = OrderedDict()
_valid_lock = threading.Lock()


def _schema_valid(doc: Any) -> bool:
    if not _top.is_valid(doc):
        return False
    for tile in doc['tiles']:
        key = json.dumps(tile, separators=(',', ':'), ensure_ascii=False)
        with _valid_lock:
            known = key in _valid_tiles
            if known:
                _valid_tiles.move_to_end(key)
        if not known:
            if not _tile.is_valid(tile):
                return False
            with _valid_lock:
                _valid_tiles[key] = True
                while len(_valid_tiles) > 2048:
                    _valid_tiles.popitem(last=False)
    return True


def _pointer(path: Iterable[Any]) -> str:
    return ''.join('/' + str(p).replace('~', '~0').replace('/', '~1') for p in path)


def _join(values: Iterable[Any]) -> str:
    # Array.prototype.join: null is written as nothing.
    return ','.join('' if v is None else to_string(v) for v in values)


def _describe(e: jsonschema.ValidationError, extra: Optional[str] = None) -> str:
    """_describe words a finding as the TypeScript package does, from Ajv's keyword and parameters."""
    kw, value = e.validator, e.validator_value
    if kw == 'additionalProperties':
        return 'unknown field ' + json.dumps(extra, ensure_ascii=False)
    if kw == 'required':
        return 'missing required field ' + str(extra)
    if kw is None:
        return 'field is not allowed for this tile kind'
    if kw == 'enum':
        return 'must be one of ' + ', '.join('' if v is None else to_string(v) for v in value)
    if kw == 'const':
        return 'must be exactly ' + stringify(value)
    if kw == 'format':
        return 'invalid ' + value
    if kw == 'pattern':
        return 'timestamp must end in Z (UTC), no offset' if value == 'Z$' else 'does not match ' + value
    # Ajv's own words for the rest.
    if kw == 'type':
        return 'must be ' + (_join(value) if isinstance(value, list) else value)
    if kw == 'minItems':
        return 'must NOT have fewer than ' + str(value) + ' items'
    if kw == 'maxItems':
        return 'must NOT have more than ' + str(value) + ' items'
    if kw == 'items':
        return 'must NOT have more than ' + str(len(e.schema.get('prefixItems') or [])) + ' items'
    if kw == 'minLength':
        return 'must NOT have fewer than ' + str(value) + ' characters'
    if kw == 'maxLength':
        return 'must NOT have more than ' + str(value) + ' characters'
    if kw == 'minimum':
        return 'must be >= ' + to_string(value)
    if kw == 'maximum':
        return 'must be <= ' + to_string(value)
    if kw == 'not':
        return 'must NOT be valid'
    return str(kw)


# Where Ajv checks each keyword, and with it where its finding lands in the list:
# depth first, and at each schema first the keywords for any type, then those of
# each data type, every group in the order of Ajv's vocabularies. jsonschema goes
# by the order of the schema's fields instead.
_GROUPS = [
    ['$ref', 'const', 'enum', 'not', 'anyOf', 'oneOf', 'allOf', 'if'],
    ['maximum', 'minimum', 'exclusiveMaximum', 'exclusiveMinimum', 'multipleOf'],
    ['maxLength', 'minLength', 'pattern', 'format'],
    ['maxItems', 'minItems', 'uniqueItems', 'prefixItems', 'items', 'contains'],
    ['maxProperties', 'minProperties', 'required', 'propertyNames', 'additionalProperties', 'dependencies',
     'properties', 'patternProperties'],
]
_RANK = {kw: (g, i) for g, group in enumerate(_GROUPS) for i, kw in enumerate(group)}
_TYPE_GROUP = {'number': 1, 'integer': 1, 'string': 2, 'array': 3, 'object': 4}


def _type_rank(node: Dict[str, Any]) -> Tuple[int, int]:
    # One type with keywords of its own is checked where that type's keywords are;
    # otherwise before everything else.
    types = node.get('type')
    if isinstance(types, str) and types in _TYPE_GROUP:
        group = _TYPE_GROUP[types]
        if any(_RANK.get(k, (-1, 0))[0] == group for k in node):
            return group, -1
    return -1, 0


def _walk(node: Any, steps: List[Any], i: int, data: List[Any], d: int) -> Optional[List[Any]]:
    """
    _walk follows a jsonschema schema path through the schema and returns Ajv's
    rank at every step, or None when the path does not lead through `node`.
    jsonschema leaves $ref and if out of its paths, so both are tried where a
    step is not the node's own.
    """
    if i == len(steps):
        return []
    if not isinstance(node, dict):
        return None
    kw = steps[i]
    own = _own(node, kw, steps, i, data, d) if kw in node or kw in ('then', 'else') else None
    if own is not None:
        return own
    target = _ref(node)
    if target is not None:
        rest = _walk(target, steps, i, data, d)
        if rest is not None:
            return [_RANK['$ref']] + rest
    return None


def _own(node: Dict[str, Any], kw: Any, steps: List[Any], i: int, data: List[Any], d: int) -> Optional[List[Any]]:
    last = i + 1 == len(steps)
    if kw in ('then', 'else'):
        rest = _walk(node.get(kw), steps, i + 1, data, d)
        return None if rest is None else [_RANK['if']] + rest
    rank = _type_rank(node) if kw == 'type' else _RANK.get(kw, (len(_GROUPS), 0))
    if last:
        return [rank]
    nxt = steps[i + 1]
    if kw == 'properties' and isinstance(node[kw], dict) and nxt in node[kw]:
        rest = _walk(node[kw][nxt], steps, i + 2, data, d + 1)
        return None if rest is None else [rank, (list(node[kw]).index(nxt),)] + rest
    if kw == 'prefixItems' and isinstance(nxt, int) and nxt < len(node[kw]):
        rest = _walk(node[kw][nxt], steps, i + 2, data, d + 1)
        return None if rest is None else [rank, (nxt,)] + rest
    if kw == 'items' and d < len(data):
        rest = _walk(node[kw], steps, i + 1, data, d + 1)
        return None if rest is None else [rank, (data[d],)] + rest
    if kw in ('allOf', 'anyOf', 'oneOf') and isinstance(nxt, int) and nxt < len(node[kw]):
        rest = _walk(node[kw][nxt], steps, i + 2, data, d)
        return None if rest is None else [rank, (nxt,)] + rest
    return None


def _order(e: jsonschema.ValidationError, more: Tuple[Any, ...] = ()) -> Tuple[Any, ...]:
    """_order is where Ajv would have reported e: a key to sort the findings by."""
    key = _walk(SCHEMA, list(e.absolute_schema_path) + list(more), 0, list(e.absolute_path) + list(more), 0) or []
    # A oneOf or anyOf is reported after its branches, as Ajv does.
    if e.validator in ('oneOf', 'anyOf'):
        key.append((len(_GROUPS) + 1,))
    return tuple(key)


class _Finding:
    """A finding in Ajv's shape: the instance path, the keyword, the words."""

    def __init__(self, path: Tuple[Any, ...], keyword: Optional[str], message: str, error: jsonschema.ValidationError):
        self.path = path
        self.keyword = keyword
        self.message = message
        self.error = error
        self.order = _order(error)


def _ref(schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ref = schema.get('$ref') if isinstance(schema, dict) else None
    if not isinstance(ref, str) or not ref.startswith('#/$defs/'):
        return None
    return SCHEMA['$defs'].get(ref[len('#/$defs/'):])


def _intended_branch(e: jsonschema.ValidationError) -> Optional[int]:
    """
    _intended_branch picks the branch of a failed oneOf that the object was meant
    for: the one that knows the most of its fields. A series with start, stepSec
    and points means the step form even when its start is broken. Without a clear
    winner it returns None, and every branch's errors stand.
    """
    data = e.instance
    if not isinstance(data, dict) or not isinstance(e.validator_value, list):
        return None
    best, best_score, tie = None, -1, False
    for i, branch in enumerate(e.validator_value):
        target = _ref(branch)
        props = target.get('properties') if target else None
        if not isinstance(props, dict):
            return None
        score = sum(1 for k in data if k in props)
        if score > best_score:
            best, best_score, tie = i, score, False
        elif score == best_score:
            tie = True
    return None if tie else best


def _node_at(steps: List[Any]) -> Any:
    """_node_at is the schema a jsonschema schema path leads to, $ref followed where the path leaves it out."""
    node: Any = SCHEMA
    for step in steps:
        while isinstance(node, dict) and step not in node and _ref(node) is not None:
            node = _ref(node)
        if isinstance(node, dict):
            node = node.get(step)
        elif isinstance(node, list) and isinstance(step, int) and step < len(node):
            node = node[step]
        else:
            return None
    return node


def _data_at(doc: Any, path: Iterable[Any]) -> Any:
    for step in path:
        if isinstance(doc, dict) and step in doc:
            doc = doc[step]
        elif isinstance(doc, list) and isinstance(step, int) and step < len(doc):
            doc = doc[step]
        else:
            return None
    return doc


def _findings(e: jsonschema.ValidationError, doc: Any, done: set) -> List[_Finding]:
    """_findings flattens one jsonschema error into Ajv's list: a oneOf or anyOf brings its branches."""
    path = tuple(e.absolute_path)
    out: List[_Finding] = []
    if e.validator in ('oneOf', 'anyOf'):
        for c in e.context or []:
            out += _findings(c, doc, done)
    if e.validator is None and e.absolute_schema_path and e.absolute_schema_path[-1] == 'properties':
        # A field a `false` schema forbids. jsonschema reports it at the object, one
        # error per field and none naming it; Ajv names it. The fields are those the
        # schema forbids that the object has, in the schema's order.
        where = (tuple(e.absolute_schema_path), path)
        if where in done:
            return out
        done.add(where)
        props = _node_at(list(e.absolute_schema_path))
        data = _data_at(doc, path)
        for name, sub in (props.items() if isinstance(props, dict) else []):
            if sub is False and isinstance(data, dict) and name in data:
                f = _Finding(path + (name,), None, _describe(e), e)
                f.order = _order(e, (name,))
                out.append(f)
        return out
    if e.validator == 'additionalProperties' and isinstance(e.instance, dict):
        known = e.schema.get('properties') or {}
        patterns = e.schema.get('patternProperties') or {}
        for key in e.instance:
            if key not in known and not any(re.search(p, key) for p in patterns):
                out.append(_Finding(path, 'additionalProperties', _describe(e, key), e))
        return out
    if e.validator == 'required' and isinstance(e.instance, dict):
        for key in e.validator_value:
            if key not in e.instance:
                out.append(_Finding(path, 'required', _describe(e, key), e))
        return out
    out.append(_Finding(path, e.validator, _describe(e), e))
    return out


def _ajv_order(findings: List[_Finding]) -> List[_Finding]:
    return sorted(findings, key=lambda f: f.order)


def _narrow(findings: List[_Finding], doc: Any) -> List[_Finding]:
    """
    _narrow replaces the findings of every branch of a failed oneOf with those of
    the branch that was meant. Both report all of them, and the first three - all
    a refusal shows - then say "missing samples" and "unknown field start" about a
    series whose only fault is the format of its start.
    """
    out = findings
    for f in findings:
        if f.keyword != 'oneOf' or f.error is None:
            continue
        best = _intended_branch(f.error)
        if best is None:
            continue
        mine_errors = [c for c in f.error.context or [] if c.relative_schema_path[0] == best]
        if not mine_errors:
            continue
        at = f.path

        def inside(x: _Finding) -> bool:
            return x.path[:len(at)] == at

        done: set = set()
        mine = _narrow(_ajv_order([g for c in mine_errors for g in _findings(c, doc, done)]), doc)
        out = [x for x in out if not inside(x) or x is f] + mine
    return out


def _schema_errors(doc: Any) -> List[Issue]:
    errors = list(_validator.iter_errors(doc))
    if not errors:
        return []
    done: set = set()
    findings = _narrow(_ajv_order([f for e in errors for f in _findings(e, doc, done)]), doc)
    seen = set()
    out: List[Issue] = []
    for f in findings:
        # Both report every branch of the if/then tree; the leaves are the useful part.
        if f.keyword in ('if', 'allOf', 'oneOf', 'anyOf'):
            continue
        issue = Issue('error', _pointer(f.path) or '/', f.message)
        key = issue.path + '|' + issue.message
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out


def _round(x: float) -> int:
    return math.floor(x + 0.5)  # Math.round, not Python's round half to even


def _semantic_issues(doc: Dict[str, Any]) -> List[Issue]:
    """
    _semantic_issues follows the configurator's validator rule for rule and in the
    same order. Three deliberate differences:

      - size: an error from 72 % of 256 KiB of plaintext, not 95 %. Sealing adds a
        third (base64), so from about 192 KiB the envelope no longer fits and the
        relay refuses it; 95 % is too late for a document that is about to be sealed.
      - generatedAt in the future or older than staleAfterSec: not checked. The
        package sets generatedAt itself, just before sealing.
      - "fewer than 3 points": not checked. The first real value replaces the
        template's points, so a fresh series starts at one point on every start;
        warning about that would be noise, not advice.
    """
    out: List[Issue] = []
    tiles = doc.get('tiles') if isinstance(doc.get('tiles'), list) else []

    def error(path: str, message: str) -> None:
        out.append(Issue('error', path, message))

    def warning(path: str, message: str) -> None:
        out.append(Issue('warning', path, message))

    size = len(stringify(doc).encode('utf-8'))
    if size > MAX_ENVELOPE * 0.72:
        error('/', 'the document is ' + str(_round(size / 1024)) + ' KiB; sealed it would pass the 256 KiB limit')
    elif size > 64 * 1024:
        warning('/', 'the document is ' + str(_round(size / 1024)) + ' KiB; fewer points per series save radio and battery')
    if is_number(doc.get('staleAfterSec')) and doc['staleAfterSec'] < 30:
        warning('/staleAfterSec', 'very small; rule of thumb: two to three times the upload interval')

    ids: Dict[str, int] = {}
    for i, tile in enumerate(tiles):
        p = '/tiles/' + str(i)
        if isinstance(tile, dict) and isinstance(tile.get('id'), str):
            if tile['id'] in ids:
                error(p + '/id', 'duplicate tile id "' + tile['id'] + '" (also at /tiles/' + str(ids[tile['id']]) + ')')
            else:
                ids[tile['id']] = i
        if not isinstance(tile, dict):
            continue
        if isinstance(tile.get('titleShort'), str) and utf16_len(tile['titleShort']) > 12:
            error(p + '/titleShort', 'longer than 12 characters')
        if isinstance(tile.get('title'), str) and utf16_len(tile['title']) > 12 and not tile.get('titleShort'):
            warning(p + '/titleShort', 'missing: the title is cut in small widgets')

        r = tile.get('range')
        has_range = isinstance(r, dict) and is_number(r.get('min')) and is_number(r.get('max'))
        value = tile.get('value')
        if has_range and r['max'] <= r['min']:
            error(p + '/range', 'max must be greater than min')
        if has_range and is_number(value) and (value < r['min'] or value > r['max']):
            warning(p + '/value', 'lies outside range')
        if has_range and is_number(tile.get('target')) and (tile['target'] < r['min'] or tile['target'] > r['max']):
            warning(p + '/target', 'lies outside range')
        bands = tile.get('bands')
        if isinstance(bands, list) and bands:
            prev = r['min'] if has_range else -math.inf
            for bi, b in enumerate(bands):
                if isinstance(b, dict) and is_number(b.get('to')) and b['to'] < prev:
                    error(p + '/bands/' + str(bi) + '/to', 'bands must be ascending')
                if isinstance(b, dict) and is_number(b.get('to')):
                    prev = b['to']
            if has_range and prev < r['max']:
                warning(p + '/bands', 'last band ends before range.max; the rest of the scale stays uncoloured')
            if tile.get('status') and has_range and is_number(value):
                start = r['min']
                for b in bands:
                    if start <= value <= b['to']:
                        if b['status'] != tile['status']:
                            warning(p + '/status', '"' + to_string(tile['status']) + '", but the value sits in the "' +
                                    to_string(b['status']) + '" band')
                        break
                    start = b['to']

        categories = tile.get('categories')
        series = tile.get('series')
        if isinstance(categories, list):
            if tile.get('view') in ('pie', 'donut') and len(categories) > 8:
                warning(p + '/categories', 'more than 8 categories: the palette starts over')
            if isinstance(series, list):
                for si, s in enumerate(series):
                    if not isinstance(s, dict):
                        continue
                    values = s.get('values')
                    statuses = s.get('statuses')
                    if isinstance(values, list) and len(values) != len(categories):
                        error(p + '/series/' + str(si) + '/values', str(len(values)) + ' values but ' + str(len(categories)) + ' categories')
                    if isinstance(statuses, list) and isinstance(values, list):
                        if len(statuses) != len(values):
                            warning(p + '/series/' + str(si) + '/statuses', str(len(statuses)) + ' statuses but ' + str(len(values)) +
                                    ' values; the app reads missing ones as null and ignores surplus ones')
                        if len(statuses) > 1 and all(x == 'ok' for x in statuses):
                            warning(p + '/series/' + str(si) + '/statuses',
                                    'every bar ok: traffic lights only for bars that carry a verdict, the rest stays null')
        if tile.get('view') == 'line' and isinstance(series, list):
            for si, s in enumerate(series):
                n = 0
                if isinstance(s, dict) and isinstance(s.get('samples'), list):
                    n = len(s['samples'])
                elif isinstance(s, dict) and isinstance(s.get('points'), list):
                    n = len(s['points'])
                if n > 500:
                    warning(p + '/series/' + str(si), str(n) + ' points; 30 to 60 are enough for one screen')
                if isinstance(s, dict) and isinstance(s.get('samples'), list):
                    last = -math.inf
                    for k, sample in enumerate(s['samples']):
                        ts = parse_date(sample[0] if isinstance(sample, list) and sample else None)
                        if math.isfinite(ts) and ts < last:
                            warning(p + '/series/' + str(si) + '/samples/' + str(k), 'timestamps not ascending')
                            break
                        if math.isfinite(ts):
                            last = ts

        columns, rows = tile.get('columns'), tile.get('rows')
        if tile.get('view') == 'table' and isinstance(columns, list) and isinstance(rows, list):
            for ri, row in enumerate(rows):
                if isinstance(row, dict) and isinstance(row.get('cells'), list) and len(row['cells']) != len(columns):
                    warning(p + '/rows/' + str(ri) + '/cells', str(len(row['cells'])) + ' cells but ' + str(len(columns)) + ' columns')
            if len(rows) > 10:
                warning(p + '/rows', str(len(rows)) + ' rows; in a widget ten is a lot')

        if tile.get('view') == 'timeline':
            start = parse_date(tile.get('from'))
            end = parse_date(tile.get('to'))
            if math.isfinite(start) and math.isfinite(end) and end <= start:
                error(p + '/to', 'to must be after from')
            state_ids = {s.get('id') for s in (tile.get('states') if isinstance(tile.get('states'), list) else []) if isinstance(s, dict)}
            for li, lane in enumerate(tile.get('lanes') if isinstance(tile.get('lanes'), list) else []):
                last = -math.inf
                segments = lane.get('segments') if isinstance(lane, dict) and isinstance(lane.get('segments'), list) else []
                for si, seg in enumerate(segments):
                    at = p + '/lanes/' + str(li) + '/segments/' + str(si)
                    state = seg[1] if isinstance(seg, list) and len(seg) > 1 else None
                    if state not in state_ids:
                        error(at, 'unknown state "' + ('undefined' if state is None else to_string(state)) + '"')
                    ts = parse_date(seg[0] if isinstance(seg, list) and seg else None)
                    if math.isfinite(ts) and ts < last:
                        error(at, 'segments must be ascending')
                    if math.isfinite(ts):
                        last = ts
                    if math.isfinite(ts) and math.isfinite(start) and ts < start:
                        warning(at, 'starts before from')

        if isinstance(tile.get('subtext'), str) and re.search(r'\b(vor \d|ago)\b', tile['subtext'], re.IGNORECASE | re.ASCII):
            warning(p + '/subtext', 'time of day rather than duration: "3 minutes ago" is already wrong when read')
    return out


def validate(doc: Any) -> Validation:
    """
    validate returns the errors (the document must not be sent) and the warnings
    (it may be sent, but something looks off). Semantic checks run only on a
    document that passed the schema: on a broken one they would mostly repeat it.
    """
    schema_issues = [] if _schema_valid(doc) else _schema_errors(doc)
    issues = schema_issues if schema_issues else _semantic_issues(doc)
    return Validation([i for i in issues if i.level == 'error'], [i for i in issues if i.level == 'warning'])


_TILE = re.compile(r'/tiles/(\d+)')


def tile_of(doc: Any, path: Optional[str]) -> Optional[str]:
    """tile_of names the tile an issue path points into, for messages a person reads."""
    m = _TILE.match(path or '')
    tiles = doc.get('tiles') if isinstance(doc, dict) else None
    tile = tiles[int(m.group(1))] if m and isinstance(tiles, list) and int(m.group(1)) < len(tiles) else None
    return tile['id'] if isinstance(tile, dict) and isinstance(tile.get('id'), str) else None


def summary(doc: Any, issues: List[Issue]) -> str:
    """summary puts the first three issues into one line."""
    parts = []
    for i in issues[:3]:
        tile_id = tile_of(doc, i.path)
        rest = _TILE.sub('', i.path, count=1) if tile_id and _TILE.match(i.path) else i.path
        parts.append(('tile "' + tile_id + '"' if tile_id else 'document') + (' ' + rest if rest and rest != '/' else '') + ': ' + i.message)
    return '; '.join(parts) + (' (+' + str(len(issues) - 3) + ' more)' if len(issues) > 3 else '')
