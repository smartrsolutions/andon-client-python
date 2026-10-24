"""What the tests share: the template, a clock the test moves, and opening an envelope as a paired device does."""

import base64
import datetime as dt
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from andon_client._relay import Response

SAMPLE = Path(__file__).parent / 'data' / 'view-v1.sample.json'
VIEW = 'vw_totavbtprh6rdpgg2m2f6vrwny'
SECRET = 'ws_' + 'a' * 43
KEY = os.urandom(32)
START = dt.datetime(2026, 9, 22, 18, 40, tzinfo=dt.timezone.utc).timestamp() * 1000


def template():
    """The sample view document of the view format, 14 tiles in all nine kinds."""
    return json.loads(SAMPLE.read_text('utf-8'))


def tile(doc, tile_id):
    return next(t for t in doc['tiles'] if t['id'] == tile_id)


class FakeClock:
    """FakeClock runs timers when the test moves time, never on its own."""

    def __init__(self, now=START):
        self._now = now
        self._seq = 0
        self.timers = {}

    def now(self):
        return self._now

    def set_timeout(self, fn, ms):
        self._seq += 1
        self.timers[self._seq] = (self._now + ms, self._seq, fn)
        return self._seq

    def clear_timeout(self, handle):
        self.timers.pop(handle, None)

    def advance(self, ms):
        until = self._now + ms
        while True:
            due = sorted(self.timers.values())
            if not due or due[0][0] > until:
                break
            at, seq, fn = due[0]
            del self.timers[seq]
            self._now = at
            fn()
        self._now = until

    def pending(self):
        return len(self.timers)


def open_envelope(text, key=KEY):
    """open_envelope decrypts what was uploaded, so the tests look at the document itself."""
    env = json.loads(text)
    aad = ('andon-v1|' + env['view'] + '|' + str(env['kv'])).encode()
    plain = AESGCM(key).decrypt(base64.b64decode(env['nonce']), base64.b64decode(env['ct']), aad)
    return env, json.loads(plain)


class Relay:
    """Relay is a transport that records every request and answers from `answers`, 204 once they run out."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        env, doc = open_envelope(body.decode())
        self.requests.append({'url': url, 'headers': headers, 'body': body.decode(), 'env': env, 'doc': doc})
        status = self.answers.pop(0) if self.answers else 204
        return Response(status, '' if status == 204 else '{"error":"e' + str(status) + '"}', None)
