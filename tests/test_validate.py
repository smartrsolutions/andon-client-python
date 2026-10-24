"""
The semantic rules against the configurator's: the same verdict, and a warning
wherever the configurator warns. Each case changes one thing in the sample
document. test_parity.py holds the rest against the TypeScript package.
"""

import unittest

from andon_client import validate
from andon_client._js import iso

from helpers import START, template, tile


def messages(issues):
    return [i.path + ' ' + i.message for i in issues]


def variant(change):
    doc = template()
    change(doc)
    return validate(doc)


class Validate(unittest.TestCase):
    def test_the_sample_document_has_no_errors_and_no_warnings(self):
        r = validate(template())
        self.assertEqual((r.errors, r.warnings), ([], []))

    def test_samples_out_of_order_are_a_warning_not_an_error(self):
        r = variant(lambda d: tile(d, 't_takt')['series'][0]['samples'].reverse())
        self.assertEqual(r.errors, [])
        self.assertIn('timestamps not ascending', '\n'.join(messages(r.warnings)))

    def test_segments_out_of_order_are_an_error(self):
        r = variant(lambda d: tile(d, 't_history')['lanes'][0]['segments'].reverse())
        self.assertIn('segments must be ascending', '\n'.join(messages(r.errors)))

    def test_each_warning_of_the_configurator_is_raised(self):
        def drop_short(d):
            del tile(d, 't_oee')['titleShort']
            tile(d, 't_oee')['title'] = 'Gesamtanlageneffektivität'

        def nine(d):
            t = tile(d, 't_defects')
            t['categories'] = list('abcdefghi')
            t['series'][0]['values'] = [1] * 9

        def all_ok(d):
            s = tile(d, 't_scrap_station')['series'][0]
            s['statuses'] = ['ok'] * len(s['values'])

        def later_from(d):
            t = tile(d, 't_history')
            t['from'] = t['lanes'][0]['segments'][1][0]

        cases = [
            (lambda d: tile(d, 't_oee').update(target=120), 'target lies outside range'),
            (lambda d: tile(d, 't_oee')['bands'][2].update(to=90), 'last band ends before range.max'),
            (lambda d: tile(d, 't_oee').update(status='ok'), '"ok", but the value sits in the "warning" band'),
            (drop_short, 'title is cut in small widgets'),
            (nine, 'more than 8 categories'),
            (lambda d: tile(d, 't_scrap_station')['series'][0]['statuses'].pop(), 'statuses but'),
            (all_ok, 'every bar ok'),
            (lambda d: tile(d, 't_stations')['rows'][0]['cells'].pop(), 'cells but'),
            (lambda d: tile(d, 't_line').update(subtext='seit 3 Minuten ago'), 'time of day rather than duration'),
            (lambda d: d.update(staleAfterSec=10), 'very small'),
            (later_from, 'starts before from'),
        ]
        for change, expected in cases:
            with self.subTest(expected):
                r = variant(change)
                self.assertEqual(r.errors, [])
                self.assertIn(expected, '\n'.join(messages(r.warnings)))

    def test_a_document_too_large_to_seal_is_an_error(self):
        doc = template()
        tile(doc, 't_line')['subtext'] = 'x' * 80
        doc['tiles'] += [{'id': 't_pad' + str(k), 'view': 'line', 'title': 'Pad ' + str(k), 'value': 1,
                          'series': [{'name': 'Pad', 'samples': [[iso(START + i * 1000), 32.1] for i in range(1100)]}]}
                         for k in range(6)]
        self.assertIn('sealed it would pass the 256 KiB limit', '\n'.join(messages(validate(doc).errors)))

    def test_a_broken_series_names_its_own_fault_not_the_other_form(self):
        steps = variant(lambda d: tile(d, 't_takt').update(series=[
            {'name': 'Ist', 'start': 'Sat Oct 03 2026 00:00:00 GMT+0200', 'stepSec': 3600, 'points': [1, 2]}]))
        self.assertEqual(messages(steps.errors), [
            '/tiles/5/series/0/start timestamp must end in Z (UTC), no offset',
            '/tiles/5/series/0/start invalid date-time',
        ])
        samples = variant(lambda d: tile(d, 't_takt').update(series=[{'name': 'Ist', 'samples': [['gestern', 1]]}]))
        self.assertNotRegex('\n'.join(messages(samples.errors)), 'start|stepSec|points')
        self.assertIn('samples/0/0 invalid date-time', '\n'.join(messages(samples.errors)))
        neither = variant(lambda d: tile(d, 't_takt').update(series=[{'name': 'Ist'}]))
        self.assertRegex('\n'.join(messages(neither.errors)), 'missing required field samples[\\s\\S]*missing required field start')

    def test_a_pattern_ends_where_the_text_ends(self):
        # Python's $ also matches before a final line break; JavaScript's does not.
        r = variant(lambda d: d['tiles'][0].update(id=d['tiles'][0]['id'] + '\n'))
        self.assertIn('/tiles/0/id does not match', '\n'.join(messages(r.errors)))


if __name__ == '__main__':
    unittest.main()
