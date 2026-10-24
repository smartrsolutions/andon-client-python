"""
The rules against the TypeScript package's: tests/data/parity.json holds what
@smartrsolutions/andon-client answered to a few hundred documents and updates,
and every one of them has to get the same answer here, word for word. The file is
written by the source repository's tooling and arrives with each release.
"""

import copy
import json
import math
import unittest
from pathlib import Path

from andon_client._accepts import describe, forms, hint
from andon_client._js import iso
from andon_client._seal import fingerprint
from andon_client._tiles import InputError, SeriesWindow, Update, apply, finish
from andon_client._validate import summary, validate
from andon_client._words import TS_WORDS

DATA = Path(__file__).parent / 'data'
PARITY = json.loads((DATA / 'parity.json').read_text('utf-8'))
SAMPLE = json.loads((DATA / 'view-v1.sample.json').read_text('utf-8'))
NOW = PARITY['now']


def undiff(base, d):
    """undiff rebuilds a document from what it changes in `base` (parity.mjs, diff)."""
    if 'whole' in d:
        return copy.deepcopy(d['whole'])
    doc = copy.deepcopy(base)
    for k in d.get('del', []):
        del doc[k]
    for i, tile in d.get('tiles', {}).items():
        doc['tiles'][int(i)] = copy.deepcopy(tile)
    for k, v in d.get('set', {}).items():
        doc[k] = copy.deepcopy(v)
    return doc


def padded(doc, n):
    doc['tiles'] += [{'id': 't_pad' + str(k), 'view': 'line', 'title': 'Pad ' + str(k), 'value': 1,
                      'series': [{'name': 'Pad', 'samples': [[iso(NOW - (n - i) * 1000), 32.1] for i in range(n)]}]}
                     for k in range(6)]
    return doc


def same(a, b):
    """same is deep equality as JSON sees it: key order counts, True is no 1, 1.0 is 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def issues(found):
    return [{'level': i.level, 'path': i.path, 'message': i.message} for i in found]


def verdict(doc):
    r = validate(doc)
    return {'errors': issues(r.errors), 'warnings': issues(r.warnings), 'summary': summary(doc, r.errors)}


def run_apply(doc, update, options):
    window = options.get('window')
    try:
        r = apply(doc, Update(update.get('payload'), topic=update.get('topic'), timestamp=update.get('timestamp'),
                              series=update.get('series'), lane=update.get('lane')),
                  options['now'],
                  window=SeriesWindow(window['points'], window['ageSec']) if window else None,
                  derive=set(options['derive']) if options.get('derive') is not None else None,
                  live=set(options['live']) if options.get('live') is not None else None,
                  words=TS_WORDS)
    except InputError as err:
        return {'error': 'InputError', 'message': str(err)}
    return {'doc': r.doc, 'changed': r.changed, 'replaced': r.replaced, 'touched': r.touched,
            'check': verdict(finish(r.doc, options['now'], 90))}


class Parity(unittest.TestCase):
    def test_every_document_gets_the_same_verdict(self):
        self.assertGreater(len(PARITY['validate']), 1000)
        for case in PARITY['validate']:
            doc = padded(copy.deepcopy(SAMPLE), case['padded']) if 'padded' in case else undiff(SAMPLE, case['doc'])
            with self.subTest(case['name']):
                got = verdict(doc)
                self.assertEqual(got['errors'], case['errors'])
                self.assertEqual(got['warnings'], case['warnings'])
                self.assertEqual(got['summary'], case['summary'])

    def test_every_update_does_the_same(self):
        self.assertGreater(len(PARITY['apply']), 500)
        for case in PARITY['apply']:
            doc = undiff(SAMPLE, case['doc']) if case['doc'] is not None else None
            with self.subTest(case['name']):
                self.check(run_apply(doc, case['update'], case['options']), case['result'], doc)

    def test_a_chain_of_updates_does_the_same(self):
        state = copy.deepcopy(SAMPLE)
        live = set()
        for step in PARITY['chain']['steps']:
            with self.subTest(step['update']):
                self.assertEqual(sorted(live), sorted(step['live']))
                got = run_apply(state, step['update'], {'now': step['now'], 'window': {'points': 60, 'ageSec': 0},
                                                         'live': sorted(live)})
                self.check(got, step['result'], state)
                if 'error' not in got:
                    state = got['doc']
                    live |= set(got['touched'])

    def check(self, got, expected, doc):
        if 'error' in expected:
            self.assertEqual(got.get('error'), expected['error'], got.get('message'))
            self.assertEqual(got['message'], expected['message'])
            return
        self.assertNotIn('error', got, got.get('message'))
        want = undiff(doc, expected['doc'])
        self.assertTrue(same(got['doc'], want), json.dumps(got['doc'])[:2000] + '\n' + json.dumps(want)[:2000])
        self.assertEqual(got['changed'], expected['changed'])
        self.assertEqual(got['replaced'], expected['replaced'])
        self.assertEqual(got['touched'], expected['touched'])
        self.assertEqual(got['check'], expected['check'])

    def test_every_hint_and_form_reads_the_same(self):
        for case in PARITY['hints']:
            tile = case['tile']
            with self.subTest(tile['id']):
                self.assertEqual(hint(tile, TS_WORDS), case['hint'])
                got = [{'key': f.key, 'example': f.example, 'extra': f.extra, 'text': describe(f, TS_WORDS)}
                       for f in forms(tile, NOW)]
                self.assertTrue(same(got, case['forms']), json.dumps(got) + '\n' + json.dumps(case['forms']))

    def test_every_fingerprint_is_the_same(self):
        for case in PARITY['fingerprints']:
            self.assertEqual(fingerprint({'tiles': case['tiles']}), case['fingerprint'])


if __name__ == '__main__':
    unittest.main()
