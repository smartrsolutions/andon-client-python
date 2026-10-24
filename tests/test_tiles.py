"""
What a value does to its tile. The bulk of it is in test_parity.py, against the
TypeScript package's answers; these are the cases that need more than one answer
to compare, or a clock.
"""

import re
import time
import unittest

from andon_client._js import parse_date
from andon_client._tiles import InputError, SeriesWindow, Update, apply, finish, status_from_bands
from andon_client._validate import validate

from helpers import START, template, tile

NOW = START


def run(doc, payload, topic=None, **options):
    update = Update(payload, topic=topic, timestamp=options.pop('timestamp', None), series=options.pop('series', None),
                    lane=options.pop('lane', None))
    return apply(doc, update, NOW, window=options.pop('window', SeriesWindow()), **options)


class Tiles(unittest.TestCase):
    def test_a_number_for_a_gauge_replaces_its_value_and_leaves_every_other_tile_alone(self):
        doc = template()
        r = run(doc, 81.2, 't_oee')
        self.assertEqual(r.changed, ['t_oee'])
        self.assertEqual(tile(r.doc, 't_oee')['value'], 81.2)
        self.assertEqual(tile(doc, 't_oee')['value'], 79.3, 'the input document is not touched')
        self.assertEqual([t for t in r.doc['tiles'] if t['id'] != 't_oee'], [t for t in doc['tiles'] if t['id'] != 't_oee'])

    def test_numeric_text_is_what_the_earlier_pattern_took_and_a_long_run_of_digits_costs_no_time(self):
        # The pattern of the TypeScript package's 1.0.0, in JavaScript's reading:
        # \s with a BOM, \d of ASCII digits only.
        before = re.compile('[\\s\ufeff]*[-+]?([0-9]+\\.?[0-9]*|\\.[0-9]+)([eE][-+]?[0-9]+)?[\\s\ufeff]*')
        texts = ['81.5', ' 81.5 ', '\t7\n', ' 1e3\ufeff', '-2', '+3.0E-2', '1.', '.5', '0007', '1e', '1.2.3', '', ' ', '.',
                 'e3', '0x10', 'Infinity', 'NaN', '1_000', '1,5', '--1', '+.5e+7', '5 5', '\u0663', '7\n']
        for text in texts:
            try:
                run(template(), text, 't_oee')
                took = True
            except InputError:
                took = False
            self.assertEqual(took, bool(before.fullmatch(text)), repr(text))
        start = time.perf_counter()
        with self.assertRaises(InputError):
            run(template(), '1' * 50000 + 'x', 't_oee')
        self.assertLess(time.perf_counter() - start, 0.5, 'refused within half a second')

    def test_numbers_keep_the_type_javascript_gives_them(self):
        self.assertIs(type(tile(run(template(), '1800', 't_order').doc, 't_order')['value']), int)
        self.assertIs(type(tile(run(template(), '1e3', 't_oee').doc, 't_oee')['value']), int)
        self.assertEqual(tile(run(template(), '0.5', 't_oee').doc, 't_oee')['value'], 0.5)
        with self.assertRaisesRegex(InputError, 'needs a number, got true'):
            run(template(), True, 't_oee')

    def test_a_label_keeps_the_type_of_its_template_value(self):
        doc = template()
        self.assertEqual(tile(run(doc, 'Störung', 't_line').doc, 't_line')['value'], 'Störung')
        self.assertEqual(tile(run(doc, '1800', 't_order').doc, 't_order')['value'], 1800)
        self.assertEqual(tile(run(doc, True, 't_line').doc, 't_line')['value'], 'true')

    def test_tuples_work_where_lists_do(self):
        pairs = (('2026-09-22T18:00:00Z', 30.1), ('2026-09-22T18:30:00Z', 31.4))
        r = run(template(), pairs, 't_takt')
        self.assertEqual(tile(r.doc, 't_takt')['series'][0]['samples'], [list(p) for p in pairs])
        self.assertEqual(validate(r.doc).errors, [])

    def test_an_array_of_pairs_replaces_a_step_series_too_and_later_numbers_append_to_it(self):
        pairs = [['2026-09-22T18:00:00Z', 120], ['2026-09-22T18:30:00Z', 130]]
        r = run(template(), pairs, 't_pieces_hour', series='Target')
        self.assertEqual(tile(r.doc, 't_pieces_hour')['series'][1], {'name': 'Target', 'samples': pairs})
        first = run(template(), pairs, 't_takt', live=set())
        nxt = run(first.doc, 140, 't_takt', live=set(first.touched))
        self.assertEqual(len(tile(nxt.doc, 't_takt')['series'][0]['samples']), 3)
        self.assertEqual(tile(nxt.doc, 't_takt')['value'], 140)

    def test_the_window_keeps_a_series_at_its_last_n_points_and_none_older_than_age(self):
        s = tile(run(template(), 31.9, 't_takt', window=SeriesWindow(5, 0)).doc, 't_takt')['series'][0]['samples']
        self.assertEqual(len(s), 5)
        self.assertEqual(s[4], ['2026-09-22T18:40:00Z', 31.9])
        s = tile(run(template(), 31.9, 't_takt', window=SeriesWindow(0, 600)).doc, 't_takt')['series'][0]['samples']
        self.assertTrue(all(parse_date(p[0]) >= NOW - 600000 for p in s))

    def test_the_timestamp_sets_the_sample_time_and_an_older_one_is_refused(self):
        doc = run(template(), 31, 't_takt', timestamp='2026-09-22T18:35:12.700Z').doc
        self.assertEqual(tile(doc, 't_takt')['series'][0]['samples'][-1][0], '2026-09-22T18:35:12Z')
        with self.assertRaisesRegex(InputError, 'older than the last sample'):
            run(doc, 30, 't_takt', timestamp='2026-09-22T18:00:00Z')

    def test_an_object_sets_fields_none_removes_one_id_and_view_are_refused(self):
        doc = template()
        t = tile(run(doc, {'value': 70, 'status': 'critical', 'subtext': None}, 't_oee').doc, 't_oee')
        self.assertEqual((t['value'], t['status'], 'subtext' in t), (70, 'critical', False))
        with self.assertRaisesRegex(InputError, 'comes from the template'):
            run(doc, {'view': 'label'}, 't_oee')

    def test_the_same_value_again_is_no_change(self):
        doc = template()
        r = run(doc, 79.3, 't_oee')
        self.assertEqual(r.changed, [])
        self.assertIs(r.doc, doc)

    def test_status_from_bands_is_opt_in_and_an_explicit_status_wins(self):
        doc = template()
        self.assertEqual(tile(run(doc, 50, 't_oee').doc, 't_oee')['status'], 'warning')
        self.assertEqual(tile(run(doc, 50, 't_oee', derive={'t_oee'}).doc, 't_oee')['status'], 'critical')
        self.assertEqual(tile(run(doc, 90, 't_oee', derive={'t_oee'}).doc, 't_oee')['status'], 'ok')
        self.assertEqual(tile(run(doc, {'value': 50, 'status': 'ok'}, 't_oee', derive={'t_oee'}).doc, 't_oee')['status'], 'ok')
        self.assertEqual(status_from_bands({'value': 200, 'bands': [{'to': 10, 'status': 'ok'}, {'to': 20, 'status': 'critical'}]}),
                         'critical')

    def test_finish_sets_generated_at_in_whole_seconds_and_stale_after_sec(self):
        out = finish(template(), NOW + 999, 90)
        self.assertEqual(out['generatedAt'], '2026-09-22T18:40:00Z')
        self.assertEqual(out['staleAfterSec'], 90)


if __name__ == '__main__':
    unittest.main()
