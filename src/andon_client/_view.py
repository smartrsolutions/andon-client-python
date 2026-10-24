"""
AndonView: one view, from the outside. Values in by tile ID, tiles on the
devices paired with the view. Everything below it (when to upload, what a value
means for its tile, sealing, the relay's answers) follows the rules of the
TypeScript package @smartrsolutions/andon-client and of the Node-RED package built
on it, so all three behave alike.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Union

from . import _env
from ._js import clone, is_number, trim
from ._relay import DEFAULT_RELAY, PutResult, Transport, no_trailing_slash, put, relay_url
from ._runtime import Event, ReceiveResult, UploadReport, ViewRuntime
from ._seal import VIEW_ID, WRITE_SECRET, parse_key, parse_key_version
from ._tiles import DEFAULT_WINDOW, SeriesWindow, Update
from ._validate import summary, validate

log = logging.getLogger('andon_client')

_MISSING: Any = object()

Template = Union[Mapping[str, Any], str, 'os.PathLike[str]', None]


@dataclass(frozen=True)
class UploadFailure:
    """What on_error gets: why an upload failed, and when the next try is."""
    message: str
    status: Optional[int] = None
    """HTTP status, 0 when the relay was not reached, None when nothing was sent"""
    retry_in_sec: Optional[float] = None
    """when the next try is; None: uploads stop until the next value, because trying again changes nothing"""


def _template_of(value: Template) -> Optional[Dict[str, Any]]:
    if value is None or value == '':
        return None
    try:
        if isinstance(value, os.PathLike):
            with open(value, encoding='utf-8-sig') as f:
                doc = json.load(f)
        elif isinstance(value, str):
            doc = json.loads(value)
        else:
            doc = clone(value)
    except (ValueError, TypeError):
        raise ValueError('the template is not a JSON document') from None
    check = validate(doc)
    if check.errors:
        raise ValueError('the template is invalid: ' + summary(doc, check.errors))
    return doc


def _at(value: Any) -> Any:
    """_at turns what the caller passes into what the rules take: milliseconds or ISO 8601 text."""
    if isinstance(value, _dt.datetime):
        return value.timestamp() * 1000  # a naive datetime is local time, as Python takes it
    if is_number(value):
        return value * 1000  # seconds, as time.time() gives them
    return value


class AndonView:
    """
    One Andon view: set values by tile ID, and the package collects, checks,
    encrypts and uploads them.

    `view`, `write_secret`, `content_key` and `key_version` come from the .env the
    Andon configurator hands out; from_env() reads them from the environment.
    `template` is the configurator's document download, view.json: a dict, its
    JSON text or a path. Without one, the first value has to be a whole view
    document (replace()).
    """

    def __init__(
        self,
        view: str,
        write_secret: str,
        content_key: Union[str, bytes],
        key_version: Union[int, str, None] = 1,
        *,
        template: Template = None,
        interval_sec: Optional[float] = None,
        stale_after_sec: Optional[float] = None,
        notify: str = 'auto',
        window: Optional[Mapping[str, float]] = None,
        derive_status: Iterable[str] = (),
        on_sent: Optional[Callable[[UploadReport], None]] = None,
        on_error: Optional[Callable[[UploadFailure], None]] = None,
        relay_url: Optional[str] = None,
        transport: Optional[Transport] = None,
        clock: Any = None,
    ) -> None:
        view = trim(str(view or ''))
        if not VIEW_ID.fullmatch(view):
            raise ValueError('view ID missing or not one: "' + view + '" (vw_ and 26 characters)')
        write_secret = trim(str(write_secret or ''))
        if not WRITE_SECRET.fullmatch(write_secret):
            raise ValueError('write secret missing or not one (ws_ and 43 characters)')
        key = bytes(content_key) if isinstance(content_key, (bytes, bytearray)) else parse_key(content_key)
        if len(key) != 32:
            raise ValueError('content key is ' + str(len(key)) + ' bytes, expected 32')
        kv = parse_key_version(1 if key_version is None or key_version == '' else key_version)
        if notify not in ('auto', 'always', 'never'):
            raise ValueError('notify is auto, always or never, not ' + repr(notify))
        base = no_trailing_slash(trim(str(relay_url or _default_relay()))) or DEFAULT_RELAY
        sizes = dict(DEFAULT_WINDOW._asdict())
        for name, size in (window or {}).items():
            if name not in sizes:
                raise ValueError('window takes points and age_sec, not ' + repr(name))
            sizes[name] = size
        report_error = on_error or (lambda e: log.warning('andon: %s', e.message))

        def on_event(event: Event) -> None:
            if event.type == 'sent':
                if on_sent:
                    on_sent(event.report)
            elif event.type == 'error':
                report_error(UploadFailure(event.message, event.report.status if event.report else None, event.retry_in_sec))
            elif event.type == 'throttled':
                report_error(UploadFailure(event.message, 429, event.retry_in_sec))

        self._runtime = ViewRuntime(
            view, kv, key,
            put=lambda envelope: put(base, view, write_secret, envelope, transport),
            notify=notify,
            interval_sec=interval_sec,
            stale_after_sec=stale_after_sec,
            window=SeriesWindow(**sizes),
            derive=set(derive_status or ()),
            template=_template_of(template),
            on_event=on_event,
            clock=clock,
        )
        self._closing = False
        self._close_lock = threading.Lock()

    @classmethod
    def from_env(cls, prefix: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
                 env_file: 'Union[str, os.PathLike[str], None]' = None, **options: Any) -> 'AndonView':
        """
        from_env takes the view from the environment, under the names the .env from
        the configurator uses: ANDON_VIEW, ANDON_WRITE_SECRET, ANDON_CONTENT_KEY,
        ANDON_KEY_VERSION. A prefix such as ANDON_CLOUD_ replaces the leading ANDON_
        of every name.

        `env` is the environment to read, os.environ by default. `env_file` is a .env
        to read as well, the configurator's download as it is; a variable the
        environment already has wins over the file, as with python-dotenv. The other
        arguments are those of AndonView().
        """
        prefix = _env.check_prefix(prefix) or 'ANDON_'
        env = env if env is not None else os.environ
        if env_file is not None:
            env = {**_env.read_file(env_file), **env}

        def need(suffix: str) -> str:
            value = _env.pick(prefix, suffix, env)
            if not value:
                raise ValueError(_env.name(prefix, suffix) + ' is not set')
            return value

        own = no_trailing_slash(_env.pick(prefix, 'URL', env))
        options['relay_url'] = options.get('relay_url') or own or relay_url(env)
        return cls(need('VIEW'), need('WRITE_SECRET'), need('CONTENT_KEY'),
                   _env.pick(prefix, 'KEY_VERSION', env) or 1, **options)

    def set(self, tile: Union[str, Mapping[str, Any]], value: Any = _MISSING, *,
            at: Any = None, series: Optional[str] = None, lane: Optional[str] = None) -> ReceiveResult:
        """
        set gives one tile a value, or several tiles theirs: set('t_oee', 79.3) or
        set({'t_oee': 79.3, 't_shift': 580}). A dict for a single tile sets the
        fields it names: set('t_oee', {'value': 79.3, 'status': 'warning'}).

        `at` is when the value was measured, for a series or a timeline: a datetime,
        seconds since the epoch or ISO 8601; now by default. `series` and `lane`
        pick a series or a timeline lane by name; the first one by default.

        It raises InputError when a value does not fit its tile and ValidationError
        when it would leave the view invalid; the view then stays as it was. Values
        set together go out in the same upload.
        """
        self._open()
        extra = {'timestamp': _at(at), 'series': series, 'lane': lane}
        if isinstance(tile, str):
            if not trim(tile):
                raise TypeError('set needs a tile ID')
            return self._runtime.receive(Update(None if value is _MISSING else value, topic=tile, **extra))
        if value is not _MISSING or not isinstance(tile, Mapping) or isinstance(tile.get('tiles'), list):
            raise TypeError('set takes a tile ID and a value, or a dict of tile IDs and values; replace() takes a whole view document')
        return self._runtime.receive(Update(dict(tile), **extra))

    def replace(self, doc: Mapping[str, Any]) -> ReceiveResult:
        """replace swaps in a whole view document: for a source that builds its tiles itself."""
        self._open()
        if not isinstance(doc, Mapping) or not isinstance(doc.get('tiles'), (list, tuple)):
            raise TypeError('replace takes a view document, with tiles')
        return self._runtime.receive(Update(clone(doc)))

    @property
    def document(self) -> Optional[Dict[str, Any]]:
        """document is a copy of the view as it stands, or None before the first value without a template."""
        doc = self._runtime.doc
        return clone(doc) if doc else None

    def flush(self) -> Optional[PutResult]:
        """
        flush uploads a pending change now instead of at the end of the interval and
        returns the relay's answer; None when nothing was pending. The relay takes
        one upload per view every ten seconds and answers 429 to more.
        """
        self._runtime.idle()
        if not self._runtime.pending:
            return None
        return self._runtime.flush()

    def close(self, flush: bool = True) -> None:
        """
        close uploads what is still pending (unless flush=False) and stops every
        timer, so the process can exit. The view takes no values afterwards.
        """
        with self._close_lock:
            if self._closing:
                return
            self._closing = True
            if flush:
                self.flush()
            self._runtime.idle()
            self._runtime.close()

    def __enter__(self) -> 'AndonView':
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _open(self) -> None:
        if self._closing:
            raise RuntimeError('the view is closed')


def _default_relay() -> str:
    return relay_url()
