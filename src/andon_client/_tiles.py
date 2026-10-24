"""
What an update does to the view document: pure functions, no I/O in here.

The document is the state. The template from the configurator is the first
state; every update changes one or more tiles of it, and what a value means
follows from the tile's kind:

  label, gauge, progress, donut   a scalar replaces `value`
  line, bar without categories    a number is appended to a time series
  bar with categories, pie, donut an array replaces the series' `values`
  table                           an array replaces `rows`
  timeline                        a state ID appends a segment to a lane
  every kind                      an object sets the fields it names; None removes one

Nothing here decides whether the result is a valid document: that is the
validator's job, and the caller rolls the change back when it says no.

Times are milliseconds since the epoch in here, as in the TypeScript package
this is the twin of; AndonView turns what its caller passes into them.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, NamedTuple, Optional, Set

from ._accepts import hint
from ._js import clone, is_finite_number, is_number, iso, parse_date, stringify, to_string, trim
from ._words import API_WORDS, Words


class SeriesWindow(NamedTuple):
    """How much of a time series is kept: the newest `points`, and none older than `age_sec` (0: no limit)."""
    points: int = 60
    age_sec: float = 0


DEFAULT_WINDOW = SeriesWindow()
MAX_SEGMENTS = 200  # the most a lane may carry in a view document


class InputError(ValueError):
    """A value that does not fit its tile, or an update that names no tile. The view stays as it was."""


class Update(NamedTuple):
    """
    One update:

      topic = tile ID, payload = value or fields    one tile
      payload = {tile_id: value, ...}               several tiles
      payload = a whole view document (has tiles)   replaces the state
    """
    payload: Any
    topic: Any = None
    timestamp: Any = None
    """a sample's or a segment's time: milliseconds or ISO 8601"""
    series: Any = None
    """the series a value belongs to, by name; the first one without"""
    lane: Any = None
    """the timeline lane a state belongs to, by name; the first one without"""


class ApplyResult(NamedTuple):
    doc: Dict[str, Any]
    changed: List[str]
    """the IDs of the tiles that changed"""
    replaced: bool
    """the whole document was replaced"""
    touched: List[str]
    """the series and lanes that hold real data from now on"""


class _Context:
    def __init__(self, now: float, window: Optional[SeriesWindow], derive: Optional[Set[str]],
                 live: Optional[Set[str]], update: Update, words: Words) -> None:
        self.now = now
        self.window = window
        self.derive = derive
        self.live = live
        self.touched: List[str] = []
        self.timestamp = update.timestamp
        self.series = update.series
        self.lane = update.lane
        self.words = words


def _unset(value: Any) -> bool:
    return value is None or value == ''


def _timestamp_of(value: Any, now: float, words: Words) -> float:
    if _unset(value):
        return now
    if isinstance(value, _dt.datetime):
        ms = value.timestamp() * 1000
    elif is_number(value):
        ms = value
    else:
        ms = parse_date(value)
    # The range a JavaScript Date holds, and with it what iso() can write.
    if not is_finite_number(ms) or not -62135596800000 <= ms <= 253402300799999:
        raise InputError(words.timestamp + ' is neither ' + words.times + ': ' + to_string(value))
    return ms


_NUMERIC_KINDS = {'gauge', 'progress', 'line', 'donut'}
# The digits before and after the point are matched once each: \d+\.?\d* backtracks
# quadratically on a long run of digits that does not match. Surrounding white
# space goes by trim(), which removes what JavaScript's \s matches. [0-9], not \d,
# which in Python takes every script's digits.
_NUMERIC = re.compile(r'[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?')


def _numeric(text: str) -> bool:
    return _NUMERIC.fullmatch(trim(text)) is not None


def _as_number(text: str) -> Any:
    n = float(trim(text))
    return int(n) if n.is_integer() and abs(n) < 1e21 else n


def _number(value: Any, what: str) -> Any:
    """_number takes numbers and numeric strings: MQTT and most PLCs deliver text."""
    if is_finite_number(value):
        return value
    if isinstance(value, str) and _numeric(value):
        n = _as_number(value)
        if is_finite_number(n):
            return n
    raise InputError(what + ' needs a number, got ' + _json(value))


def _json(value: Any) -> str:
    try:
        return stringify(value)
    except (TypeError, ValueError):
        return to_string(value)


def status_from_bands(tile: Dict[str, Any]) -> Optional[str]:
    """
    status_from_bands is the opt-in judgement "status from bands": the band the
    value falls into. Below range.min counts as the first band, above the last band
    as the last one. The app never does this itself: a status is the producer's
    judgement.
    """
    bands = tile.get('bands')
    if not isinstance(bands, list) or not bands or not is_number(tile.get('value')):
        return None
    for band in bands:
        if tile['value'] <= band['to']:
            return band['status']
    return bands[-1]['status']


def _names(items: List[Any], field: str) -> str:
    return ', '.join('"' + to_string(item.get(field) if isinstance(item, dict) else None) + '"' for item in items)


def _pick_series(tile: Dict[str, Any], name: Any) -> Dict[str, Any]:
    series = tile.get('series')
    if not isinstance(series, list) or not series:
        raise InputError('tile "' + tile['id'] + '" has no series in the template')
    if _unset(name):
        return series[0]
    for s in series:
        if isinstance(s, dict) and s.get('name') == name:
            return s
    raise InputError('tile "' + tile['id'] + '" has no series "' + to_string(name) + '"; it has ' + _names(series, 'name'))


def _from_template(ctx: _Context, key: str) -> bool:
    """
    _from_template says whether a series or lane still holds the template's
    points. Those are start values drawn in the configurator, not measurements:
    the first real value replaces them rather than being appended to made-up
    history. The caller passes ctx.live (what already holds real data) and records
    ctx.touched once the change is accepted; without ctx.live everything counts as real.
    """
    seen = key in ctx.touched
    if not seen:
        ctx.touched.append(key)
    return ctx.live is not None and key not in ctx.live and not seen


def _mark_real(ctx: _Context, key: str) -> None:
    """
    _mark_real records that a series or lane holds real data now, so the next value
    appended to it keeps it: a whole series sent as an array or an object is a
    measurement, not the template's start values.
    """
    if key not in ctx.touched:
        ctx.touched.append(key)


def _append_sample(tile: Dict[str, Any], value: Any, ctx: _Context) -> None:
    series = _pick_series(tile, ctx.series)
    if series.get('points') or series.get('values'):
        raise InputError('tile "' + tile['id'] + '": appending needs a series with samples; "' + to_string(series.get('name')) +
                         '" uses ' + ('stepSec and points' if series.get('points') else 'values'))
    if _from_template(ctx, tile['id'] + '|series|' + to_string(series.get('name'))):
        series['samples'] = []
    if not isinstance(series.get('samples'), list):
        series['samples'] = []
    samples: List[Any] = series['samples']
    at = _timestamp_of(ctx.timestamp, ctx.now, ctx.words)
    stamp = iso(at)
    last = samples[-1] if samples else None
    if last and parse_date(last[0]) > parse_date(stamp):
        raise InputError('tile "' + tile['id'] + '": ' + stamp + ' is older than the last sample ' + to_string(last[0]))
    if last and last[0] == stamp:
        last[1] = value  # two values within one second: the later one wins
    else:
        samples.append([stamp, value])
    window = ctx.window or DEFAULT_WINDOW
    if window.age_sec > 0:
        oldest = at - window.age_sec * 1000
        while len(samples) > 1 and parse_date(samples[0][0]) < oldest:
            samples.pop(0)
    if window.points > 0 and len(samples) > window.points:
        del samples[:len(samples) - window.points]
    if tile.get('view') == 'line':
        tile['value'] = value


def _append_segment(tile: Dict[str, Any], state_id: str, ctx: _Context) -> bool:
    states = tile.get('states') if isinstance(tile.get('states'), list) else []
    if not any(isinstance(s, dict) and s.get('id') == state_id for s in states):
        raise InputError('tile "' + tile['id'] + '" has no state "' + state_id + '"; it has ' + _names(states, 'id'))
    lanes = tile.get('lanes') if isinstance(tile.get('lanes'), list) else []
    if _unset(ctx.lane):
        lane = lanes[0] if lanes else None
    else:
        lane = next((lane for lane in lanes if isinstance(lane, dict) and lane.get('name') == ctx.lane), None)
    if not lane:
        raise InputError('tile "' + tile['id'] + '" has no lane' + (' "' + to_string(ctx.lane) + '"' if ctx.lane else '') +
                         ('; it has ' + _names(lanes, 'name') if lanes else ''))
    if _from_template(ctx, tile['id'] + '|lane|' + to_string(lane.get('name'))):
        lane['segments'] = []
    if not isinstance(lane.get('segments'), list):
        lane['segments'] = []
    segments: List[Any] = lane['segments']
    last = segments[-1] if segments else None
    if last and len(last) > 1 and last[1] == state_id:
        return False  # the state goes on; nothing to record
    stamp = iso(_timestamp_of(ctx.timestamp, ctx.now, ctx.words))
    if last and parse_date(last[0]) > parse_date(stamp):
        raise InputError('tile "' + tile['id'] + '": ' + stamp + ' is older than the last segment ' + to_string(last[0]))
    if last and last[0] == stamp:
        last[1] = state_id
    else:
        segments.append([stamp, state_id])
    _trim_segments(tile, segments)
    return True


def _trim_segments(tile: Dict[str, Any], segments: List[Any]) -> None:
    """
    _trim_segments keeps a lane inside the tile: at most 200 segments, and nothing
    that starts before `from` except the one state that was running at `from`,
    which then starts there. So moving `from` forward is all a rolling timeline needs.
    """
    start = parse_date(tile.get('from'))
    if math.isfinite(start):
        first_inside = next((i for i, s in enumerate(segments) if parse_date(s[0]) >= start), len(segments))
        if first_inside > 0:
            running = segments[first_inside - 1]
            del segments[:first_inside - 1]
            # tile['from'] verbatim, not iso(start): a `from` with milliseconds is
            # valid, and rounding it down would start the segment before it.
            if parse_date(running[0]) < start:
                running[0] = tile['from']
            if len(segments) > 1 and segments[1][0] == running[0]:
                segments.pop(0)
    if len(segments) > MAX_SEGMENTS:
        del segments[:len(segments) - MAX_SEGMENTS]


def _set_fields(tile: Dict[str, Any], fields: Dict[str, Any]) -> bool:
    for key in fields:
        if key in ('id', 'view'):
            raise InputError('tile "' + tile['id'] + '": "' + key + '" comes from the template and cannot be changed by a message')
        if fields[key] is None:
            tile.pop(key, None)
        else:
            tile[key] = clone(fields[key])
    if isinstance(tile.get('value'), str) and tile.get('view') in _NUMERIC_KINDS:
        tile['value'] = _number(tile['value'], 'tile "' + tile['id'] + '", value')
    # A new `from` trims every lane at once, not only at the next state change: a
    # rolling timeline moves `from` on every tick, and in between the segments would
    # start before it.
    if tile.get('view') == 'timeline' and 'from' in fields and isinstance(tile.get('lanes'), list):
        for lane in tile['lanes']:
            if isinstance(lane, dict) and isinstance(lane.get('segments'), list):
                _trim_segments(tile, lane['segments'])
    return 'status' in fields


def _update_tile(tile: Dict[str, Any], payload: Any, ctx: _Context) -> Dict[str, Any]:
    """
    _update_tile returns the changed copy of one tile, or the tile itself when the
    update changed nothing. It raises InputError when the value does not fit the tile.
    """
    nxt = clone(tile)
    kind = nxt.get('view')
    categories = isinstance(nxt.get('categories'), list)
    status_given = False
    changed = True

    if payload is None:
        raise InputError('tile "' + tile['id'] + '": ' + ctx.words.payload + ' is empty')
    elif isinstance(payload, (list, tuple)):
        if (kind == 'bar' and categories) or kind in ('pie', 'donut'):
            _pick_series(nxt, ctx.series)['values'] = [
                None if v is None else _number(v, 'tile "' + tile['id'] + '", value ' + str(i + 1)) for i, v in enumerate(payload)]
        elif kind == 'table':
            nxt['rows'] = [{'cells': clone(row)} if isinstance(row, (list, tuple)) else clone(row) for row in payload]
        elif kind in ('line', 'bar'):
            # The series in whatever form it had becomes one of samples: an array of
            # pairs replaces a step series too, rather than sitting next to its points.
            series = _pick_series(nxt, ctx.series)
            nxt['series'][nxt['series'].index(series)] = {'name': series.get('name'), 'samples': clone(payload)}
            _mark_real(ctx, tile['id'] + '|series|' + to_string(series.get('name')))
            last = payload[-1] if payload else None
            if kind == 'line' and isinstance(last, (list, tuple)) and len(last) > 1 and is_number(last[1]):
                nxt['value'] = last[1]
        else:
            raise InputError('tile "' + tile['id'] + '" takes no array')
    elif isinstance(payload, dict):
        status_given = _set_fields(nxt, payload)
        for s in payload.get('series') if isinstance(payload.get('series'), list) else []:
            if isinstance(s, dict) and s.get('name'):
                _mark_real(ctx, tile['id'] + '|series|' + to_string(s['name']))
        for lane in payload.get('lanes') if isinstance(payload.get('lanes'), list) else []:
            if isinstance(lane, dict) and lane.get('name'):
                _mark_real(ctx, tile['id'] + '|lane|' + to_string(lane['name']))
    elif kind == 'label':
        # A label keeps the type its template gave it: "79.3" from MQTT stays a
        # number when the template has a number, and "Läuft" stays a text.
        is_num = is_number(payload) or (is_number(tile.get('value')) and _numeric(to_string(payload)))
        nxt['value'] = _number(payload, 'tile "' + tile['id'] + '"') if is_num else to_string(payload)
    elif kind in ('gauge', 'progress', 'donut'):
        nxt['value'] = _number(payload, 'tile "' + tile['id'] + '"')
    elif kind == 'line' or (kind == 'bar' and not categories):
        _append_sample(nxt, _number(payload, 'tile "' + tile['id'] + '"'), ctx)
    elif kind == 'timeline':
        changed = _append_segment(nxt, to_string(payload), ctx)
    else:
        raise InputError('tile "' + tile['id'] + '" takes no single value')

    if not changed:
        return tile
    if not status_given and ctx.derive and nxt.get('id') in ctx.derive:
        status = status_from_bands(nxt)
        if status:
            nxt['status'] = status
    return nxt


def apply(doc: Optional[Dict[str, Any]], update: Update, now: float, window: Optional[SeriesWindow] = None,
          derive: Optional[Set[str]] = None, live: Optional[Set[str]] = None, words: Words = API_WORDS) -> ApplyResult:
    """
    apply works out what one update means for the document and returns the new
    document plus the IDs of the tiles that changed. It never changes `doc`.
    `derive` names the tiles whose status follows from their bands; `live` the
    series and lanes that already hold real data, None: everything counts as real.
    """
    ctx = _Context(now, window, derive, live, update, words)
    topic = trim(update.topic) if isinstance(update.topic, str) else ''
    payload = update.payload

    if not topic and isinstance(payload, dict) and isinstance(payload.get('tiles'), list):
        return ApplyResult(clone(payload), [t.get('id') if isinstance(t, dict) else None for t in payload['tiles']], True, [])
    if not doc or not isinstance(doc.get('tiles'), list):
        raise InputError('there is no template yet: ' + words.template + ', or send a whole view document first')

    if topic:
        updates = [(topic, payload)]
    elif isinstance(payload, dict):
        updates = list(payload.items())
        if not updates:
            raise InputError(words.payload + ' names no tile')
    else:
        raise InputError(words.topic + ' is missing: set it to the ID of the tile this value belongs to')

    nxt = dict(doc)
    nxt['tiles'] = list(doc['tiles'])
    changed: List[str] = []
    for tile_id, value in updates:
        index = next((i for i, t in enumerate(nxt['tiles']) if isinstance(t, dict) and t.get('id') == tile_id), -1)
        if index == -1:
            raise InputError('the view has no tile "' + to_string(tile_id) + '"; it has ' + _names(nxt['tiles'], 'id'))
        try:
            tile = _update_tile(nxt['tiles'][index], value, ctx)
        except InputError as err:
            # The refusal says what was wrong; the hint says what would have been right.
            says = hint(nxt['tiles'][index], words)
            if says:
                raise InputError(str(err) + '. ' + says) from None
            raise
        # The same value again is no change: it keeps the view alive (see _runtime,
        # heartbeat) but is no reason for an upload of its own.
        if tile is not nxt['tiles'][index] and stringify(tile) != stringify(nxt['tiles'][index]):
            nxt['tiles'][index] = tile
            changed.append(tile_id)
    return ApplyResult(nxt if changed else doc, changed, False, ctx.touched)


def finish(doc: Dict[str, Any], now: float, stale_after_sec: float) -> Dict[str, Any]:
    """finish fills in what the source never sends itself: when, and how long it holds."""
    out = dict(doc)
    out['generatedAt'] = iso(now)
    if stale_after_sec > 0:
        out['staleAfterSec'] = stale_after_sec
    return out
