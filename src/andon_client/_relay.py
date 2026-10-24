"""
The one request this package makes: PUT /v1/views/{id}/envelope.

The write secret travels in the Authorization header and nowhere else; it is
never part of a URL, a result or an error text. Users talk to relay.andon.app;
ANDON_RELAY_URL exists for development against a local relay only.
"""

from __future__ import annotations

import json
import math
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional

from ._js import trim

DEFAULT_RELAY = 'https://relay.andon.app'
TIMEOUT_SEC = 15

THROTTLE_SEC = 10
"""The relay accepts at most one upload per view per ten seconds (429 otherwise)."""


def no_trailing_slash(url: str) -> str:
    return url.rstrip('/')


def relay_url(env: Optional[Mapping[str, str]] = None) -> str:
    """relay_url is ANDON_RELAY_URL without a trailing slash, or the public relay."""
    value = no_trailing_slash(trim((env if env is not None else os.environ).get('ANDON_RELAY_URL') or ''))
    return value or DEFAULT_RELAY


def relay_aliases(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """
    relay_aliases are the other addresses under which a .env may name this same
    relay. Development only: in a container a local relay is reachable as
    host.docker.internal:8080, while the .env names it as 127.0.0.1:8080.
    ANDON_RELAY_ALIASES is a comma-separated list, empty by default.
    """
    text = (env if env is not None else os.environ).get('ANDON_RELAY_ALIASES') or ''
    return [a for a in (no_trailing_slash(trim(s)) for s in text.split(',')) if a]


@dataclass(frozen=True)
class PutResult:
    """
    The answer to one upload:

      ok         204, stored
      throttled  429, try again after retry_after_sec
      retry      network error or 5xx, worth trying again later
      fatal      401, 404, 400, 413: trying again changes nothing until a person does
    """
    outcome: str
    status: int
    """HTTP status, 0 when the relay was not reached"""
    code: str = ''
    """the relay's error code, network when it was not reached"""
    message: str = ''
    retry_after_sec: Optional[float] = None
    """for throttled: when the relay takes the next upload"""


class Response(NamedTuple):
    """What a transport hands back: the status, the body as text, the Retry-After header."""
    status: int
    text: str
    retry_after: Optional[str]


Transport = Callable[[str, Dict[str, str], bytes, float], Response]
"""transport(url, headers, body, timeout_sec) makes the PUT; raises OSError when there is no answer."""


def urllib_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method='PUT')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:  # noqa: S310 - the URL is the relay's
            return Response(res.status, '' if res.status == 204 else _read(res), res.headers.get('retry-after'))
    except urllib.error.HTTPError as err:
        return Response(err.code, _read(err), err.headers.get('retry-after') if err.headers else None)


def _read(res) -> str:
    try:
        return res.read(4096).decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001 - a body that cannot be read carries no code, nothing more
        return ''


def _error_code(text: str) -> str:
    if not text.startswith('{'):
        return ''
    try:
        parsed = json.loads(text)
    except ValueError:
        return ''  # not the relay's JSON (a proxy's page, say): no code to report
    code = parsed.get('error') if isinstance(parsed, dict) else None
    return code if isinstance(code, str) else ''


def _unreachable(err: BaseException) -> str:
    """_unreachable says why a request got no answer at all."""
    if isinstance(err, (socket.timeout, TimeoutError)) or \
            (isinstance(err, urllib.error.URLError) and isinstance(err.reason, (socket.timeout, TimeoutError))):
        return 'the relay did not answer within ' + str(TIMEOUT_SEC) + ' s'
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    return 'the relay could not be reached: ' + (str(reason) or type(reason).__name__)


def _hint(status: int, code: str) -> str:
    """_hint says what to change, per the relay's answers."""
    if status == 401:
        return 'the relay rejected the write secret (' + code + '): use the current .env of the view'
    if status == 404:
        return 'the relay does not know this view (' + code + '): check the view ID, or use the current .env'
    if status == 413:
        return 'the envelope is larger than 256 KiB (' + code + '): fewer tiles or shorter series'
    if status == 400:
        return 'the relay refused the envelope (' + code + ')'
    if status == 429:
        return 'throttled (' + code + '): the next upload carries the latest state'
    return 'upload failed with HTTP ' + str(status) + (' (' + code + ')' if code else '')


def _retry_after(value: Optional[str]) -> float:
    try:
        seconds = float(trim(value)) if value is not None and trim(value) else 0
    except ValueError:
        return THROTTLE_SEC  # an HTTP date: the throttle is what the relay means
    return seconds if math.isfinite(seconds) and seconds > 0 else THROTTLE_SEC


def put(base: str, view: str, write_secret: str, envelope: str, transport: Optional[Transport] = None) -> PutResult:
    """put uploads one envelope and classifies the answer. It never raises for the relay's sake."""
    url = base + '/v1/views/' + urllib.parse.quote(view, safe='') + '/envelope'
    headers = {'content-type': 'application/json', 'authorization': 'Bearer ' + write_secret}
    try:
        res = (transport or urllib_transport)(url, headers, envelope.encode('utf-8'), TIMEOUT_SEC)
    except (OSError, ValueError) as err:  # URLError and timeouts are OSErrors; ValueError for a broken URL
        return PutResult('retry', 0, 'network', _unreachable(err))
    text = '' if res.status == 204 else (res.text or '')[:300]
    code = _error_code(text)
    if res.status == 204:
        return PutResult('ok', 204)
    if res.status == 429:
        return PutResult('throttled', 429, code, _hint(429, code or 'throttled'), _retry_after(res.retry_after))
    if res.status >= 500:
        return PutResult('retry', res.status, code, _hint(res.status, code))
    return PutResult('fatal', res.status, code, _hint(res.status, code))
