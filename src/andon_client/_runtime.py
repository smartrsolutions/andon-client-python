"""
The runtime of one view: holds the document, takes values, decides when to
upload, and uploads. AndonView is its public face.

When an upload happens:

  - a change is sent at most every interval_sec (never below the relay's 10 s);
    the first change after a quiet spell goes out at once;
  - without a change, a heartbeat goes out every stale_after_sec / 2, but only
    when values kept arriving since the last upload. A source whose sensors went
    quiet must turn stale in the app, so the package never vouches for values it
    has not seen again;
  - 429 waits for Retry-After, a network error or 5xx backs off, and 401, 404,
    400 or 413 stop until the next value: repeating them changes nothing.

Clock, timers and the upload are injected, so the tests run without waiting and
without a relay. The upload runs in the thread that calls flush(): the clock's
worker for a timed one, the caller's for AndonView.flush(). A lock keeps the
state whole between them; it is never held during an upload, so set() does not
wait for the relay.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from ._accepts import hint
from ._relay import THROTTLE_SEC, PutResult
from ._seal import fingerprint, seal
from ._tiles import ApplyResult, SeriesWindow, Update, apply, finish
from ._validate import Issue, summary, tile_of, validate
from ._words import API_WORDS, Words

MAX_BACKOFF_SEC = 300

log = logging.getLogger('andon_client')


class ValidationError(ValueError):
    """A value that leaves the document invalid. The view stays as it was."""


@dataclass(frozen=True)
class UploadReport:
    """What one upload was, without the envelope or a secret."""
    view: str
    kv: int
    notify: bool
    status: int
    """HTTP status, 0 when the relay was not reached"""
    tiles: int
    document_bytes: int
    envelope_bytes: int
    generated_at: Optional[str]
    heartbeat: bool
    """sent without a change, to keep the view fresh"""


@dataclass(frozen=True)
class ReceiveResult:
    changed: List[str]
    """the IDs of the tiles that changed"""
    warnings: List[Issue]
    """what looks off in the new document; it is sent anyway"""


@dataclass
class Event:
    """What the runtime reports: queued, sent, throttled or error."""
    type: str
    message: str = ''
    in_sec: Optional[int] = None
    retry_in_sec: Optional[float] = None
    report: Optional[UploadReport] = None
    sources: Optional[List[Any]] = None
    doc: Optional[Dict[str, Any]] = None
    fingerprint: Optional[str] = None
    live: Optional[List[str]] = None


class Clock:
    """
    The real clock: wall time for timestamps, and one worker thread that runs the
    timers. The worker runs only while a timer is pending and is not a daemon, so
    a script that sets values and ends still uploads them before Python exits, as
    a Node.js process does with its pending timers. A heartbeat or a retry that is
    due keeps it alive too; close() on the view ends it.

    A timer due at once runs after GRACE_MS. Node.js runs a timer only once the
    code that set it is done, so the values a script sets one after the other go
    out in one upload; a thread would otherwise send the first one alone, and the
    rest an interval later.
    """

    GRACE_MS = 100

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._heap: List[Any] = []
        self._seq = itertools.count()
        self._cancelled: Set[int] = set()
        self._thread: Optional[threading.Thread] = None

    def now(self) -> float:
        return time.time() * 1000

    def set_timeout(self, fn: Callable[[], None], ms: float) -> int:
        with self._cond:
            handle = next(self._seq)
            heapq.heappush(self._heap, (time.monotonic() + max(ms, self.GRACE_MS) / 1000, handle, fn))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='andon-client', daemon=False)
                self._thread.start()
            self._cond.notify()
            return handle

    def clear_timeout(self, handle: Any) -> None:
        with self._cond:
            if any(entry[1] == handle for entry in self._heap):
                self._cancelled.add(handle)
                self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    while self._heap and self._heap[0][1] in self._cancelled:
                        self._cancelled.discard(heapq.heappop(self._heap)[1])
                    if not self._heap:
                        self._thread = None
                        return
                    wait = self._heap[0][0] - time.monotonic()
                    if wait <= 0:
                        _, _, fn = heapq.heappop(self._heap)
                        break
                    self._cond.wait(wait)
            try:
                fn()
            except Exception:  # noqa: BLE001 - a timer must not end the worker
                log.exception('andon: a timer failed')


class _InFlight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[PutResult] = None


class ViewRuntime:
    def __init__(self, view: str, kv: int, key: bytes, put: Callable[[str], PutResult],
                 notify: str = 'auto', interval_sec: Optional[float] = None, stale_after_sec: Optional[float] = None,
                 window: Optional[SeriesWindow] = None, derive: Optional[Set[str]] = None,
                 template: Optional[Dict[str, Any]] = None, on_event: Optional[Callable[[Event], None]] = None,
                 clock: Any = None, restored: Optional[Dict[str, Any]] = None, words: Words = API_WORDS) -> None:
        self._view = view
        self._kv = kv
        self._key = key
        self._put = put
        self._notify = notify
        self._window = window
        self._derive = derive
        self._on_event = on_event
        self._clock = clock or Clock()
        self._words = words
        self.interval_sec = max(THROTTLE_SEC, _number(interval_sec) or 30)
        stale = _number(stale_after_sec)
        self.stale_after_sec = stale if stale and stale > 0 else self.interval_sec * 3
        self.doc: Optional[Dict[str, Any]] = (restored or {}).get('doc') or template or None
        self._last_fingerprint: Optional[str] = restored.get('fingerprint') if restored else None
        # Series and lanes that already hold real data (_tiles, _from_template).
        # None: the whole document came from the source, nothing is template.
        live = restored.get('live', []) if restored else []
        self._live: Optional[Set[str]] = set(live) if isinstance(live, list) else None
        self._dirty = False             # the document changed since the last upload
        self._seen = False              # a value arrived since the last upload, changed or not
        self._blocked: Optional[str] = None  # a fatal answer: wait for the next value
        self._last_upload: Optional[float] = None  # ms of the last 204
        self._not_before = 0.0          # Retry-After or backoff
        self._failures = 0
        self._sources: List[Any] = []
        self._timer: Any = None
        self._token: Optional[object] = None
        self._in_flight: Optional[_InFlight] = None
        self._closed = False
        self._lock = threading.RLock()

    def _emit(self, event: Event) -> None:
        if self._on_event:
            self._on_event(event)

    def receive(self, update: Update, source: Any = None) -> ReceiveResult:
        """
        receive applies one update. It raises InputError or ValidationError and then
        leaves the document exactly as it was, so one bad value never blocks the
        others. Returns the warnings of the new document and the changed tile IDs.
        """
        with self._lock:
            now = self._clock.now()
            result: ApplyResult = apply(self.doc, update, now, window=self._window, derive=self._derive,
                                        live=self._live, words=self._words)
            warnings: List[Issue] = []
            if result.changed:
                check = validate(finish(result.doc, now, self.stale_after_sec))
                if check.errors:
                    # A value that broke a tile gets told what that tile takes: the
                    # schema's verdict names the field, not the value that was meant.
                    broken: List[str] = []
                    for e in check.errors:
                        tile_id = tile_of(result.doc, e.path)
                        if tile_id and tile_id in result.changed and tile_id not in broken:
                            broken.append(tile_id)
                    tiles = (self.doc or {}).get('tiles') or []
                    hints = [h for h in (hint(next((t for t in tiles if t.get('id') == i), None), self._words)
                                         for i in broken[:2]) if h]
                    raise ValidationError(summary(result.doc, check.errors) + ('. ' + '. '.join(hints) if hints else ''))
                warnings = check.warnings
                self.doc = result.doc
                self._dirty = True
                if result.replaced:
                    self._live = None
                elif self._live is not None:
                    self._live.update(result.touched)
            self._seen = True
            self._blocked = None
            if source is not None and source not in self._sources:
                self._sources.append(source)
            events = self._schedule()
        self._emit_all(events)
        return ReceiveResult(result.changed, warnings)

    @property
    def pending(self) -> bool:
        """pending says whether a change is waiting for its upload."""
        with self._lock:
            return self._dirty and not self._blocked and not self._closed

    def due(self) -> Optional[float]:
        """due is when the next upload may happen, or None when none is needed."""
        if not self.doc or self._blocked or self._closed:
            return None
        if self._dirty:
            at = 0.0 if self._last_upload is None else self._last_upload + self.interval_sec * 1000
        elif self._seen and self._last_upload is not None:
            at = self._last_upload + max(self.interval_sec, self.stale_after_sec / 2) * 1000
        else:
            return None
        return max(at, self._not_before)

    def schedule(self) -> None:
        with self._lock:
            events = self._schedule()
        self._emit_all(events)

    def _schedule(self) -> List[Event]:
        if self._in_flight or self._closed:
            return []
        if self._timer is not None:
            self._clock.clear_timeout(self._timer)
            self._timer = self._token = None
        at = self.due()
        if at is None:
            return []
        wait = max(0.0, at - self._clock.now())
        # The token tells a timer that was replaced from the current one: one that
        # already left the clock's queue cannot be cancelled any more, only ignored.
        token = self._token = object()
        self._timer = self._clock.set_timeout(lambda: self._fire(token), wait)
        return [Event('queued', in_sec=math.ceil(wait / 1000))] if wait > 0 else []

    def _fire(self, token: object) -> None:
        with self._lock:
            if token is not self._token:
                return
            self._token = None
            self._timer = None
        self.flush()

    def _emit_all(self, events: List[Event]) -> None:
        for event in events:
            self._emit(event)

    def flush(self) -> Optional[PutResult]:
        """flush builds, seals and uploads the current document once, in the calling thread."""
        with self._lock:
            if self._in_flight is not None:
                waiting = self._in_flight
            elif self._closed or not self.doc:
                return None
            else:
                waiting = None
                prepared = self._prepare()
        if waiting is not None:
            waiting.done.wait()
            return waiting.result
        if isinstance(prepared, Event):
            self._emit(prepared)
            return None
        return self._upload(*prepared)

    def _prepare(self) -> Any:
        now = self._clock.now()
        sources = list(self._sources)
        doc = finish(self.doc, now, self.stale_after_sec)
        was_dirty = self._dirty
        self._dirty = False
        self._seen = False
        self._sources = []

        fp = fingerprint(doc)
        notify = fp != self._last_fingerprint
        if self._notify == 'always':
            notify = True
        elif self._notify == 'never':
            notify = False
        try:
            sealed = seal(doc, self._view, self._kv, self._key, notify)
        except ValueError as err:
            self._blocked = 'too_large'
            self._dirty = was_dirty
            return Event('error', message=str(err), sources=sources)
        self._in_flight = _InFlight()
        return now, sources, doc, was_dirty, fp, notify, sealed

    def _upload(self, now, sources, doc, was_dirty, fp, notify, sealed) -> PutResult:
        flight = self._in_flight
        try:
            res = self._put(sealed.text)
        except Exception as err:  # noqa: BLE001 - put never raises for the relay; anything else is a retry too
            res = PutResult('retry', 0, 'network', 'the upload failed: ' + str(err))
        events: List[Event] = []
        with self._lock:
            self._in_flight = None
            if not self._closed:
                report = UploadReport(self._view, self._kv, notify, res.status, len(doc['tiles']),
                                      sealed.document_bytes, sealed.bytes, doc.get('generatedAt'), not was_dirty)
                if res.outcome == 'ok':
                    self._last_upload = now
                    # Stored only after a 204: a status change whose upload failed must
                    # still wake the devices on the next try.
                    self._last_fingerprint = fp
                    self._failures = 0
                    self._not_before = 0
                    events.append(Event('sent', report=report, sources=sources, doc=self.doc, fingerprint=fp,
                                        live=sorted(self._live) if self._live is not None else None))
                else:
                    self._dirty = self._dirty or was_dirty
                    self._seen = self._seen or not was_dirty
                    for s in sources:
                        if s not in self._sources:
                            self._sources.append(s)
                    if res.outcome == 'throttled':
                        self._not_before = self._clock.now() + res.retry_after_sec * 1000
                        events.append(Event('throttled', message=res.message, retry_in_sec=res.retry_after_sec,
                                            report=report, sources=sources))
                    elif res.outcome == 'retry' and not threading.main_thread().is_alive():
                        # The program has ended, and only this thread keeps it: a
                        # script with the relay out of reach would otherwise never
                        # end, and a cron job would pile up. Node.js would retry on.
                        self._blocked = 'ended'
                        events.append(Event('error', message=res.message + '; the program has ended, so that was the last try',
                                            report=report, sources=sources))
                    elif res.outcome == 'retry':
                        self._failures += 1
                        backoff = min(MAX_BACKOFF_SEC, self.interval_sec * 2 ** (self._failures - 1))
                        self._not_before = self._clock.now() + backoff * 1000
                        events.append(Event('error', message=res.message, retry_in_sec=backoff, report=report, sources=sources))
                    else:
                        self._blocked = res.code or str(res.status)
                        events.append(Event('error', message=res.message, report=report, sources=sources))
                events += self._schedule()
        flight.result = res
        flight.done.set()
        self._emit_all(events)
        return res

    def idle(self) -> None:
        """idle returns once no upload is in flight."""
        while True:
            with self._lock:
                flight = self._in_flight
            if flight is None:
                return
            flight.done.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._token = None
            if self._timer is not None:
                self._clock.clear_timeout(self._timer)
                self._timer = None


def _number(value: Any) -> Optional[float]:
    """_number is Number(value) for a setting, None where JavaScript's would be NaN or 0."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n != 0 else None
