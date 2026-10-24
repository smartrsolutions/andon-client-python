"""
Envelope v1: the one place this package touches the content key.

AES-256-GCM, a fresh 12-byte nonce per envelope, AAD "andon-v1|<view>|<kv>", the
16-byte tag appended to the ciphertext, nonce and ct as standard base64 with
padding. The format is described at https://andon.app/en/developers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ._js import is_finite_number, stringify, to_string, trim, utf16_key

MAX_ENVELOPE = 262144
"""The relay refuses larger envelopes."""

VIEW_ID = re.compile(r'vw_[a-z2-7]{26}')
WRITE_SECRET = re.compile(r'ws_[A-Za-z0-9_-]{43}')


def parse_key(text: Any) -> bytes:
    """
    parse_key accepts base64url and standard base64, with or without padding: the
    configurator writes base64url, older notes may carry the other spelling.
    """
    normalised = trim(str(text or '')).replace('-', '+').replace('_', '/')
    # Node's base64 decoder skips what it cannot read and needs no padding; the
    # length check below is what refuses a wrong key.
    cleaned = re.sub(r'[^A-Za-z0-9+/]', '', normalised.split('=')[0])
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    raw = base64.b64decode(cleaned + '=' * (-len(cleaned) % 4))
    if len(raw) != 32:
        raise ValueError('content key decodes to ' + str(len(raw)) + ' bytes, expected 32')
    return raw


def parse_key_version(value: Any) -> int:
    if isinstance(value, str):
        text = trim(value)
        try:
            kv: Any = float(text) if text and '_' not in text else 0
        except ValueError:
            kv = math.nan
    elif is_finite_number(value):
        kv = value
    else:
        kv = math.nan
    if not is_finite_number(kv) or kv != int(kv) or kv < 1:
        raise ValueError('key version must be an integer >= 1, got ' + to_string(value))
    return int(kv)


def fingerprint(doc: Mapping[str, Any]) -> str:
    """
    fingerprint is the status fingerprint of a document: tile ID and status of
    every tile, sorted, SHA-256. Only a change of it wakes devices under
    notify='auto', because a wake-up costs battery on every paired device.
    """
    tiles: Sequence[Mapping[str, Any]] = doc['tiles']
    parts = [str(tile['id']) + '=' + (to_string(tile['status']) if tile.get('status') is not None else '-')
             for tile in tiles]
    # By UTF-16 code unit, as JavaScript's sort() without a function does, never by
    # locale: the fingerprint has to be the same everywhere (the envelope format, notify).
    parts.sort(key=utf16_key)
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


class Sealed(NamedTuple):
    text: str
    """The envelope as JSON text, ready for the request body."""
    bytes: int
    """Its size in bytes."""
    document_bytes: int
    """The size of the document before sealing."""


def seal(doc: Any, view: str, kv: int, key: bytes, notify: bool, nonce: Optional[bytes] = None) -> Sealed:
    """
    seal turns a view document into the envelope JSON text. The text is assembled
    here rather than by whoever sends it, because the field order v, view, kv,
    nonce, ct, notify is part of the format. The nonce is for test vectors only:
    real envelopes take a random one.
    """
    plaintext = doc if isinstance(doc, bytes) else stringify(doc).encode('utf-8')
    iv = nonce or os.urandom(12)
    aad = ('andon-v1|' + view + '|' + str(kv)).encode('utf-8')
    ct = AESGCM(key).encrypt(iv, plaintext, aad)  # the tag is appended, as the format wants it
    text = json.dumps({
        'v': 1,
        'view': view,
        'kv': kv,
        'nonce': base64.b64encode(iv).decode('ascii'),
        'ct': base64.b64encode(ct).decode('ascii'),
        'notify': bool(notify),
    }, separators=(',', ':'), ensure_ascii=False)
    size = len(text.encode('utf-8'))
    if size > MAX_ENVELOPE:
        raise ValueError('the sealed document is ' + str(math.ceil(size / 1024)) + ' KiB, the relay takes at most ' +
                         str(MAX_ENVELOPE // 1024) + ' KiB: fewer tiles, shorter series or a smaller window')
    return Sealed(text, size, len(plaintext))
