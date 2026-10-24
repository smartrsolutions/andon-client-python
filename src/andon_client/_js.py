"""
The few places where this package has to behave like JavaScript, because the
TypeScript package @smartrsolutions/andon-client is its twin: both read the same
template the same way, refuse the same values with the same words and seal the
same document. Each helper names the JavaScript it stands in for.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import Any

# What String.prototype.trim() removes: JavaScript's white space and line
# terminators. str.strip() removes a different set (it keeps U+FEFF and takes
# U+001C to U+001F and U+0085), and MQTT payloads do arrive with a BOM.
_WS = '\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a' \
      '\u2028\u2029\u202f\u205f\u3000\ufeff'
WS_CLASS = '[' + re.escape(_WS) + ']'


def trim(text: str) -> str:
    """trim is String.prototype.trim()."""
    return text.strip(_WS)


def is_number(value: Any) -> bool:
    """is_number is typeof value === 'number': bool is an int in Python, not here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """is_finite_number is typeof value === 'number' && Number.isFinite(value)."""
    return is_number(value) and math.isfinite(value)


def utf16_len(text: str) -> int:
    """utf16_len is String.prototype.length: UTF-16 code units, not code points."""
    return len(text.encode('utf-16-le')) // 2


def utf16_key(text: str) -> bytes:
    """utf16_key sorts strings by UTF-16 code unit, as sort() without a function does."""
    return text.encode('utf-16-be', 'surrogatepass')


def number_text(x: Any) -> str:
    """number_text is String(x) for a number: the shortest digits, in JavaScript's notation."""
    if isinstance(x, bool):
        return 'true' if x else 'false'
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return 'NaN'
    if math.isinf(x):
        return 'Infinity' if x > 0 else '-Infinity'
    if x == 0:
        return '0'
    sign = '-' if x < 0 else ''
    # repr gives the shortest digits that read back as x, as JavaScript does; only
    # where the point goes and when an exponent is written differ.
    r = repr(abs(x))
    mantissa, _, exp = r.partition('e')
    whole, _, frac = mantissa.partition('.')
    e = int(exp) if exp else 0
    digits = (whole + frac).lstrip('0')
    n = len(whole) + e if whole.strip('0') else e - (len(frac) - len(frac.lstrip('0')))
    digits = digits.rstrip('0')
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + '0' * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + '.' + digits[n:]
    if -6 < n <= 0:
        return sign + '0.' + '0' * (-n) + digits
    e = n - 1
    return sign + digits[0] + ('.' + digits[1:] if k > 1 else '') + 'e' + ('+' if e >= 0 else '-') + str(abs(e))


def to_string(value: Any) -> str:
    """to_string is String(value) for what an update can carry."""
    if value is None:
        return 'null'
    if isinstance(value, bool) or is_number(value):
        return number_text(value)
    return str(value)


def _plain(value: Any) -> Any:
    """_plain is the value as JSON.stringify sees it, in the types json.dumps writes alike."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None  # JSON.stringify writes NaN and Infinity as null
        if value.is_integer() and abs(value) < 1e21:
            return int(value)  # 81.0 is 81 in JavaScript, and is written so
        return value
    if isinstance(value, _dt.datetime):
        return iso_ms(value)  # what Date.prototype.toJSON writes
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError('a ' + type(value).__name__ + ' cannot be part of a view document')


def stringify(value: Any) -> str:
    """
    stringify is JSON.stringify(value): no spaces, keys in insertion order, a float
    with nothing after the point written as an integer, as JavaScript writes it.
    Floats below 1e-4 or from 1e16 keep Python's notation; both read back the same.
    """
    return json.dumps(_plain(value), separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def clone(value: Any) -> Any:
    """clone is JSON.parse(JSON.stringify(value)): tuples become lists, and nothing is shared."""
    return json.loads(stringify(value))


def iso_ms(moment: _dt.datetime) -> str:
    """iso_ms is Date.prototype.toISOString(); a naive datetime is local time, as datetime.timestamp() takes it."""
    ms = math.floor(moment.timestamp() * 1000 + 0.5) if moment.tzinfo is None else \
        round((moment - _EPOCH).total_seconds() * 1000)
    when = _EPOCH + _dt.timedelta(milliseconds=ms)
    return _stamp(when) + '.%03dZ' % (when.microsecond // 1000)


_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

# Date.parse for the date-time strings ISO 8601 allows and ECMAScript reads: a date
# alone is UTC, a date and time without an offset local time.
_ISO = re.compile(
    r'([+-]\d{6}|\d{4})-(\d\d)-(\d\d)'
    r'(?:[Tt ](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d+))?)?\s*([Zz]|[+-]\d\d(?::?\d\d)?)?)?')


def parse_date(text: Any) -> float:
    """
    parse_date is Date.parse(text) for ISO 8601: milliseconds since the epoch, or
    NaN. JavaScript reads more spellings than that (Date.prototype.toString() among
    them); this package takes ISO 8601 only, which is what the format asks for.
    """
    if not isinstance(text, str):
        return math.nan
    m = _ISO.fullmatch(trim(text))
    if not m:
        return math.nan
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    ms = int((m.group(7) or '0')[:3].ljust(3, '0'))
    zone = m.group(8)
    next_day = hour == 24 and minute == 0 and second == 0 and ms == 0
    if (hour > 23 and not next_day) or minute > 59 or second > 59:
        return math.nan
    try:
        base = _dt.datetime(year, month, day, 0 if next_day else hour, minute, second)
    except ValueError:
        return math.nan
    if next_day:
        base += _dt.timedelta(days=1)
    if m.group(4) is None or zone is not None:
        offset = 0
        if zone and zone not in 'Zz':
            sign = -1 if zone[0] == '-' else 1
            digits = zone[1:].replace(':', '')
            hours, minutes = int(digits[:2]), int(digits[2:] or 0)
            if hours > 23 or minutes > 59:
                return math.nan
            offset = sign * (hours * 60 + minutes)
        seconds = (base.replace(tzinfo=_dt.timezone.utc) - _EPOCH).total_seconds() - offset * 60
        return float(int(seconds) * 1000 + ms)
    try:
        return float(int(base.timestamp()) * 1000 + ms)
    except (OverflowError, OSError, ValueError):
        return math.nan


def iso(ms: float) -> str:
    """iso renders a timestamp as a view document demands: UTC, whole seconds, trailing Z."""
    return _stamp(_EPOCH + _dt.timedelta(seconds=math.floor(ms / 1000))) + 'Z'


def _stamp(when: _dt.datetime) -> str:
    # By hand: strftime's %Y does not pad years below 1000 everywhere.
    return '%04d-%02d-%02dT%02d:%02d:%02d' % (when.year, when.month, when.day, when.hour, when.minute, when.second)
