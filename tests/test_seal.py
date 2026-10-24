import base64
import hashlib
import json
import os
import unittest

from andon_client._seal import MAX_ENVELOPE, fingerprint, parse_key, parse_key_version, seal

from helpers import KEY, VIEW, open_envelope


class Seal(unittest.TestCase):
    def test_seal_writes_the_fields_in_the_order_of_the_format_and_opens_again(self):
        doc = {'tiles': [{'id': 'a', 'view': 'label', 'title': 'A', 'value': 1}]}
        out = seal(doc, VIEW, 2, KEY, True)
        self.assertEqual(list(json.loads(out.text)), ['v', 'view', 'kv', 'nonce', 'ct', 'notify'])
        self.assertEqual(open_envelope(out.text)[1], doc)
        self.assertEqual(out.bytes, len(out.text.encode()))

    def test_every_envelope_gets_a_nonce_of_its_own(self):
        a = json.loads(seal({'tiles': []}, VIEW, 1, KEY, False).text)
        b = json.loads(seal({'tiles': []}, VIEW, 1, KEY, False).text)
        self.assertNotEqual(a['nonce'], b['nonce'])
        self.assertEqual(len(base64.b64decode(a['nonce'])), 12)

    def test_an_envelope_above_the_relay_limit_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'KiB'):
            seal(b'x' * MAX_ENVELOPE, VIEW, 1, KEY, False)

    def test_parse_key_takes_base64url_and_standard_base64_and_only_32_bytes(self):
        raw = os.urandom(32)
        self.assertEqual(parse_key(base64.urlsafe_b64encode(raw).decode().rstrip('=')), raw)
        self.assertEqual(parse_key(base64.b64encode(raw).decode()), raw)
        self.assertEqual(parse_key(' ' + base64.b64encode(raw).decode() + '\n'), raw)
        with self.assertRaisesRegex(ValueError, 'decodes to 3 bytes, expected 32'):
            parse_key('abcd')

    def test_parse_key_version_takes_integers_from_1(self):
        self.assertEqual(parse_key_version('3'), 3)
        self.assertEqual(parse_key_version(2), 2)
        for bad in (0, '1.5', '', 'x', True, None, 1.5, float('inf')):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, 'key version'):
                parse_key_version(bad)

    def test_the_fingerprint_depends_on_statuses_not_on_order_or_values(self):
        a = fingerprint({'tiles': [{'id': 'a', 'status': 'ok'}, {'id': 'b'}]})
        self.assertEqual(a, fingerprint({'tiles': [{'id': 'b', 'status': None}, {'id': 'a', 'status': 'ok'}]}))
        self.assertNotEqual(a, fingerprint({'tiles': [{'id': 'a', 'status': 'warning'}, {'id': 'b'}]}))

    def test_the_fingerprint_sorts_by_utf16_code_unit_never_by_locale(self):
        tiles = [{'id': i, 'status': 'ok'} for i in ['b', 'ä', 'B', 'a', 'Z', '_', '\U0001F3ED', '\uff21']]
        # Code unit order: an astral character (a surrogate pair, 0xD83C...) sorts before
        # U+FF21, although its code point is higher; Python's own order says the opposite.
        order = ['B', 'Z', '_', 'a', 'b', 'ä', '\U0001F3ED', '\uff21']
        expected = hashlib.sha256('\n'.join(x + '=ok' for x in order).encode()).hexdigest()
        self.assertEqual(fingerprint({'tiles': tiles}), expected)


if __name__ == '__main__':
    unittest.main()
