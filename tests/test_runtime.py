"""When the runtime uploads, and what: against a clock the test moves and an upload that records."""

import json
import unittest

from andon_client._relay import PutResult
from andon_client._runtime import ValidationError, ViewRuntime
from andon_client._tiles import InputError, SeriesWindow, Update

from helpers import KEY, VIEW, FakeClock, open_envelope, template, tile

OK = PutResult('ok', 204)


def runtime(answers=(), **extra):
    clock = FakeClock()
    uploads, events = [], []
    answers = list(answers)

    def put(text):
        env, doc = open_envelope(text)
        uploads.append({'at': clock.now(), 'env': env, 'doc': doc})
        return answers.pop(0) if answers else OK

    options = dict(notify='auto', interval_sec=30, stale_after_sec=120, window=SeriesWindow(60, 0), derive=set(),
                   template=template(), clock=clock, on_event=events.append)
    options.update(extra)
    rt = ViewRuntime(VIEW, 1, KEY, put, **options)
    return rt, clock, uploads, events


def send(rt, payload, topic=None, source=None, **extra):
    return rt.receive(Update(payload, topic=topic, **extra), source)


class Runtime(unittest.TestCase):
    def test_the_first_change_goes_out_at_once_sealed_for_this_view(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 81, 't_oee', 'n1')
        clock.advance(0)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]['env']['view'], VIEW)
        self.assertEqual(tile(uploads[0]['doc'], 't_oee')['value'], 81)
        self.assertEqual(uploads[0]['doc']['generatedAt'], '2026-09-22T18:40:00Z')
        self.assertEqual(uploads[0]['doc']['staleAfterSec'], 120)

    def test_changes_inside_the_interval_become_one_upload_with_the_latest_state(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 81, 't_oee')
        clock.advance(0)
        for i in range(10):
            send(rt, 600 + i, 't_shift')
            clock.advance(1000)
        self.assertEqual(len(uploads), 1)
        clock.advance(20000)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[1]['at'] - uploads[0]['at'], 30000)
        self.assertEqual(tile(uploads[1]['doc'], 't_shift')['value'], 609)

    def test_an_interval_below_the_relay_throttle_is_raised_to_10_s(self):
        self.assertEqual(runtime(interval_sec=2)[0].interval_sec, 10)
        self.assertEqual(runtime(interval_sec=-5)[0].interval_sec, 10)
        self.assertEqual(runtime(interval_sec=None)[0].interval_sec, 30)

    def test_the_heartbeat_goes_out_only_while_values_keep_arriving(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 81, 't_oee')
        clock.advance(0)
        send(rt, 81, 't_oee')  # the same value again: no change, but proof the source is alive
        clock.advance(59000)
        self.assertEqual(len(uploads), 1, 'no upload for a repeated value before stale_after_sec / 2')
        clock.advance(1000)
        self.assertEqual(len(uploads), 2, 'heartbeat at stale_after_sec / 2')
        clock.advance(600000)
        self.assertEqual(len(uploads), 2, 'silence stays silent: the app has to see the view go stale')

    def test_notify_is_true_only_when_the_status_fingerprint_changes(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 81, 't_oee')
        clock.advance(0)
        send(rt, 82, 't_oee')
        clock.advance(30000)
        send(rt, {'status': 'critical'}, 't_oee')
        clock.advance(30000)
        self.assertEqual([u['env']['notify'] for u in uploads], [True, False, True])

    def test_notify_always_and_never_override_the_fingerprint(self):
        for mode, expected in (('always', True), ('never', False)):
            rt, clock, uploads, _ = runtime(notify=mode)
            send(rt, 81, 't_oee')
            clock.advance(0)
            send(rt, 82, 't_oee')
            clock.advance(30000)
            self.assertEqual([u['env']['notify'] for u in uploads], [expected, expected], mode)

    def test_a_failed_upload_keeps_the_fingerprint_so_the_retry_still_wakes_the_devices(self):
        rt, clock, uploads, _ = runtime([PutResult('retry', 503, '', 'x')])
        send(rt, 81, 't_oee')
        clock.advance(0)
        clock.advance(30000)
        self.assertEqual([u['env']['notify'] for u in uploads], [True, True])

    def test_429_waits_for_retry_after_and_then_sends_the_latest_state(self):
        rt, clock, uploads, events = runtime([PutResult('throttled', 429, '', 'throttled', 7)])
        send(rt, 81, 't_oee')
        clock.advance(0)
        send(rt, 83, 't_oee')
        clock.advance(6000)
        self.assertEqual(len(uploads), 1)
        clock.advance(1000)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(tile(uploads[1]['doc'], 't_oee')['value'], 83)
        self.assertTrue(any(e.type == 'throttled' for e in events))

    def test_a_network_error_backs_off_doubling_up_to_five_minutes(self):
        rt, clock, uploads, _ = runtime([PutResult('retry', 0, 'network', 'down')] * 6)
        send(rt, 81, 't_oee')
        clock.advance(0)
        clock.advance(30000 + 60000 + 120000 + 240000 + 300000)
        gaps = [(b['at'] - a['at']) / 1000 for a, b in zip(uploads, uploads[1:])]
        self.assertEqual(gaps, [30, 60, 120, 240, 300])

    def test_401_stops_the_uploads_until_the_next_value(self):
        rt, clock, uploads, events = runtime([PutResult('fatal', 401, 'unauthorized', 'the relay rejected the write secret')])
        send(rt, 81, 't_oee', 'n1')
        clock.advance(0)
        clock.advance(600000)
        self.assertEqual(len(uploads), 1)
        err = next(e for e in events if e.type == 'error')
        self.assertIn('write secret', err.message)
        self.assertEqual(err.sources, ['n1'])
        send(rt, 82, 't_oee')
        clock.advance(0)
        self.assertEqual(len(uploads), 2)

    def test_an_invalid_value_is_refused_and_the_document_stays_as_it_was(self):
        rt, clock, uploads, _ = runtime()
        with self.assertRaises(ValidationError):
            send(rt, {'range': None}, 't_shift')  # a progress without range fails the schema
        with self.assertRaises(InputError):
            send(rt, 'hoch', 't_oee')
        clock.advance(60000)
        self.assertEqual(uploads, [])
        send(rt, 81, 't_oee')
        clock.advance(0)
        self.assertIn('range', tile(uploads[0]['doc'], 't_shift'), 'the refused change never reached the document')

    def test_a_document_the_schema_refuses_names_the_fault_and_what_the_broken_tile_takes(self):
        rt = runtime()[0]
        series = [{'name': 'Ist', 'start': 'Sat Oct 03 2026 00:39:48 GMT+0200', 'stepSec': 3600, 'points': [1, 2]}]
        with self.assertRaises(ValidationError) as caught:
            send(rt, {'series': series}, 't_takt')
        message = str(caught.exception)
        self.assertRegex(message, '^tile "t_takt" /series/0/start: timestamp must end in Z')
        self.assertIn('. A line takes a number (is appended to the series', message)
        self.assertNotIn('missing required field samples', message)

    def test_the_sent_event_names_the_sources_and_carries_no_secret(self):
        rt, clock, _, events = runtime()
        send(rt, 81, 't_oee', 'n1')
        send(rt, 600, 't_shift', 'n2')
        clock.advance(0)
        sent = next(e for e in events if e.type == 'sent')
        self.assertEqual(sorted(sent.sources), ['n1', 'n2'])
        self.assertNotRegex(repr(sent.report), 'ws_|nonce')

    def test_close_stops_every_timer(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 81, 't_oee')
        rt.close()
        clock.advance(60000)
        self.assertEqual(uploads, [])
        self.assertEqual(clock.pending(), 0)

    def test_the_first_real_value_replaces_the_template_points_of_a_series_later_ones_append(self):
        rt, clock, uploads, _ = runtime()
        # Earlier than the template's last sample: fine, the template history is not real.
        send(rt, 31.5, 't_takt', timestamp='2026-09-22T18:00:00Z')
        send(rt, 31.7, 't_takt')
        clock.advance(0)
        self.assertEqual(tile(uploads[0]['doc'], 't_takt')['series'][0]['samples'],
                         [['2026-09-22T18:00:00Z', 31.5], ['2026-09-22T18:40:00Z', 31.7]])

    def test_the_first_real_state_replaces_the_template_segments_of_a_lane(self):
        rt, clock, uploads, _ = runtime()
        send(rt, 'run', 't_history')
        clock.advance(0)
        self.assertEqual(tile(uploads[0]['doc'], 't_history')['lanes'][0]['segments'], [['2026-09-22T18:40:00Z', 'run']])

    def test_a_refused_value_does_not_count_as_real_data(self):
        rt, clock, uploads, _ = runtime()
        with self.assertRaises(InputError):
            send(rt, 'x', 't_takt')
        send(rt, 81, 't_oee')
        clock.advance(0)
        self.assertGreater(len(tile(uploads[0]['doc'], 't_takt')['series'][0]['samples']), 1, 'template points still there')

    def saved_state(self, *updates):
        rt, clock, _, events = runtime()
        for payload, topic in updates:
            send(rt, payload, topic)
        clock.advance(0)
        sent = [e for e in events if e.type == 'sent'][-1]
        return json.loads(json.dumps({'doc': sent.doc, 'fingerprint': sent.fingerprint, 'live': sent.live}))

    def test_a_restored_state_goes_on_where_it_stopped(self):
        saved = self.saved_state((31.5, 't_takt'))
        self.assertEqual(saved['live'], ['t_takt|series|' + tile(saved['doc'], 't_takt')['series'][0]['name']])
        rt, clock, uploads, _ = runtime(restored=saved)
        clock.advance(1000)
        send(rt, 31.7, 't_takt')
        send(rt, 'run', 't_history')
        clock.advance(0)
        samples = tile(uploads[0]['doc'], 't_takt')['series'][0]['samples']
        self.assertEqual(samples[-2:], [['2026-09-22T18:40:00Z', 31.5], ['2026-09-22T18:40:01Z', 31.7]], 'appended, not replaced')
        self.assertEqual(tile(uploads[0]['doc'], 't_history')['lanes'][0]['segments'], [['2026-09-22T18:40:01Z', 'run']])
        self.assertFalse(uploads[0]['env']['notify'], 'the statuses are the ones last sent: nothing to wake')

    def test_a_restored_state_from_a_whole_document_counts_everything_as_real(self):
        saved = self.saved_state((template(), None))
        self.assertIsNone(saved['live'])
        rt, clock, uploads, _ = runtime(restored=saved)
        before = len(tile(saved['doc'], 't_takt')['series'][0]['samples'])
        send(rt, 31.7, 't_takt')
        clock.advance(0)
        self.assertEqual(len(tile(uploads[0]['doc'], 't_takt')['series'][0]['samples']), min(before + 1, 60))


if __name__ == '__main__':
    unittest.main()
