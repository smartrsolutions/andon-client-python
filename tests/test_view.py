"""AndonView, the public face: against a stand-in relay and a clock the test moves."""

import base64
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from andon_client import AndonView, InputError, ValidationError

from helpers import KEY, SECRET, START, VIEW, FakeClock, Relay, template, tile

KEY_TEXT = base64.urlsafe_b64encode(KEY).decode().rstrip('=')


def view(answers=(), **extra):
    clock = FakeClock()
    relay = Relay(answers)
    errors = []
    options = dict(template=template(), transport=relay, clock=clock, on_error=errors.append)
    options.update(extra)
    v = AndonView(options.pop('view', VIEW), options.pop('write_secret', SECRET), options.pop('content_key', KEY_TEXT),
                  options.pop('key_version', 1), **options)
    return v, clock, relay.requests, errors


class View(unittest.TestCase):
    def test_a_value_goes_out_at_once_sealed_to_the_relay_the_secret_in_the_header_only(self):
        v, clock, requests, _ = view(relay_url='http://relay.test/')
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['url'], 'http://relay.test/v1/views/' + VIEW + '/envelope')
        self.assertEqual(requests[0]['headers']['authorization'], 'Bearer ' + SECRET)
        self.assertNotIn(SECRET, requests[0]['body'])
        self.assertEqual(tile(requests[0]['doc'], 't_oee')['value'], 81)
        v.close()

    def test_the_public_relay_is_the_default(self):
        v, clock, requests, _ = view()
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertTrue(requests[0]['url'].startswith('https://relay.andon.app/v1/views/'))
        v.close()

    def test_several_values_at_once_and_the_time_of_a_sample(self):
        v, clock, requests, _ = view()
        v.set({'t_oee': 82, 't_shift': 600})
        v.set('t_takt', 31.5, at=dt.datetime(2026, 9, 22, 18, 39, tzinfo=dt.timezone.utc))
        clock.advance(0)
        doc = requests[0]['doc']
        self.assertEqual(tile(doc, 't_oee')['value'], 82)
        self.assertEqual(tile(doc, 't_shift')['value'], 600)
        self.assertEqual(tile(doc, 't_takt')['series'][0]['samples'], [['2026-09-22T18:39:00Z', 31.5]])
        v.close()

    def test_at_takes_a_datetime_seconds_or_iso_8601(self):
        for at in (dt.datetime(2026, 9, 22, 18, 39, 30, 900000, tzinfo=dt.timezone.utc),
                   dt.datetime(2026, 9, 22, 20, 39, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))),
                   START / 1000 - 30, (START - 30000) / 1000 + 0.5, '2026-09-22T18:39:30Z', '2026-09-22T20:39:30.5+02:00'):
            with self.subTest(at):
                v = view()[0]
                v.set('t_takt', 1, at=at)
                self.assertEqual(tile(v.document, 't_takt')['series'][0]['samples'], [['2026-09-22T18:39:30Z', 1]])
        with self.assertRaisesRegex(InputError, '^the at argument is neither a datetime, seconds nor an ISO timestamp: gestern\\. '):
            view()[0].set('t_takt', 1, at='gestern')

    def test_a_refusal_names_the_api(self):
        v = view()[0]
        with self.assertRaisesRegex(InputError, '; the series argument picks which\\)$'):
            v.set('t_takt', 'x')
        with self.assertRaisesRegex(InputError, '^tile "t_oee": the value is empty\\. '):
            v.set('t_oee', None)
        with self.assertRaisesRegex(InputError, '^tile "t_oee": the value is empty\\. '):
            v.set('t_oee')
        with self.assertRaisesRegex(InputError, 'the view has no tile "t_nope"'):
            v.set('t_nope', 1)
        with self.assertRaisesRegex(InputError, 'the value names no tile'):
            v.set({})
        with self.assertRaises(ValidationError):
            v.set('t_takt', {'series': [{'name': 'Ist', 'start': 'x', 'stepSec': 60, 'points': [1]}]})
        with self.assertRaisesRegex(TypeError, 'set needs a tile ID'):
            v.set(' ', 1)
        with self.assertRaisesRegex(TypeError, 'replace\\(\\) takes a whole view document'):
            v.set(template())
        with self.assertRaisesRegex(TypeError, 'or a dict of tile IDs'):
            v.set({'t_oee': 1}, 2)
        self.assertEqual(tile(v.document, 't_oee')['value'], 79.3, 'the view stays as it was')

    def test_errors_are_value_errors_for_who_catches_those(self):
        self.assertTrue(issubclass(InputError, ValueError))
        self.assertTrue(issubclass(ValidationError, ValueError))

    def test_without_a_template_the_first_value_is_a_whole_document(self):
        v, clock, requests, _ = view(template=None)
        self.assertIsNone(v.document)
        with self.assertRaisesRegex(InputError, 'there is no template yet: pass one as the template argument'):
            v.set('t_oee', 1)
        v.replace(template())
        clock.advance(0)
        self.assertEqual(len(requests), 1)
        v.close()

    def test_the_template_can_be_json_text_or_a_path_and_an_invalid_one_is_refused_at_once(self):
        self.assertEqual(len(view(template=json.dumps(template()))[0].document['tiles']), 14)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'view.json'
            path.write_text(json.dumps(template()), 'utf-8-sig')  # with a BOM, as some editors save it
            self.assertEqual(len(view(template=path)[0].document['tiles']), 14)
        broken = template()
        broken['tiles'][0]['view'] = 'nope'
        with self.assertRaisesRegex(ValueError, '^the template is invalid: '):
            view(template=broken)
        with self.assertRaisesRegex(ValueError, 'not a JSON document'):
            view(template='{')

    def test_the_keys_are_checked_when_the_view_is_made_and_named_without_their_value(self):
        with self.assertRaisesRegex(ValueError, 'view ID'):
            view(view='nope')
        with self.assertRaisesRegex(ValueError, 'write secret') as caught:
            view(write_secret='ws_short')
        self.assertNotIn('ws_short', str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'expected 32'):
            view(content_key='abcd')
        with self.assertRaisesRegex(ValueError, 'key version'):
            view(key_version=0)
        with self.assertRaisesRegex(ValueError, 'notify'):
            view(notify='sometimes')
        with self.assertRaisesRegex(ValueError, 'window takes points and age_sec'):
            view(window={'ageSec': 1})
        self.assertIsNotNone(view(content_key=KEY)[0].document, 'the key as bytes')

    def test_changes_inside_the_interval_become_one_upload_flush_sends_at_once(self):
        v, clock, requests, _ = view()
        v.set('t_oee', 81)
        clock.advance(0)
        v.set('t_oee', 82)
        v.set('t_oee', 83)
        clock.advance(5000)
        self.assertEqual(len(requests), 1)
        self.assertEqual(v.flush().status, 204)
        self.assertEqual(len(requests), 2)
        self.assertEqual(tile(requests[1]['doc'], 't_oee')['value'], 83)
        self.assertIsNone(v.flush(), 'nothing pending')
        v.close()

    def test_close_sends_what_is_pending_and_stops_every_timer(self):
        v, clock, requests, _ = view()
        v.set('t_oee', 81)
        clock.advance(0)
        v.set('t_oee', 82)
        v.close()
        self.assertEqual(len(requests), 2)
        self.assertEqual(clock.pending(), 0)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            v.set('t_oee', 1)
        clock.advance(600000)
        self.assertEqual(len(requests), 2)
        v.close()  # a second close does nothing

    def test_close_with_flush_false_drops_what_is_pending(self):
        v, clock, requests, _ = view()
        v.set('t_oee', 81)
        clock.advance(0)
        v.set('t_oee', 82)
        v.close(flush=False)
        self.assertEqual(len(requests), 1)
        self.assertEqual(clock.pending(), 0)

    def test_with_closes_the_view(self):
        clock, relay = FakeClock(), Relay()
        with AndonView(VIEW, SECRET, KEY_TEXT, template=template(), transport=relay, clock=clock) as v:
            v.set('t_oee', 81)
        self.assertEqual(len(relay.requests), 1)
        self.assertEqual(clock.pending(), 0)

    def test_a_failed_upload_reaches_on_error_with_when_the_next_try_is(self):
        v, clock, _, errors = view([503, 401])
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertEqual((errors[0].status, errors[0].retry_in_sec), (503, 30))
        clock.advance(30000)
        self.assertEqual(errors[1].status, 401)
        self.assertIsNone(errors[1].retry_in_sec, 'a 401 waits for the next value')
        self.assertIn('use the current .env of the view', errors[1].message)
        v.close(flush=False)

    def test_without_on_error_a_failure_is_logged(self):
        clock = FakeClock()
        v = AndonView(VIEW, SECRET, KEY_TEXT, template=template(), transport=Relay([401]), clock=clock)
        with self.assertLogs('andon_client', 'WARNING') as logged:
            v.set('t_oee', 81)
            clock.advance(0)
        self.assertIn('andon: the relay rejected the write secret (e401)', logged.output[0])
        v.close(flush=False)

    def test_on_sent_gets_the_report_without_envelope_or_secret(self):
        reports = []
        v, clock, _, _ = view(on_sent=reports.append)
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertEqual((len(reports), reports[0].status, reports[0].notify), (1, 204, True))
        self.assertNotIn(SECRET, repr(reports[0]))
        v.close()

    def test_from_env_reads_the_names_of_the_env_from_the_configurator(self):
        relay, clock = Relay(), FakeClock()
        env = {'ANDON_CLOUD_VIEW': VIEW, 'ANDON_CLOUD_WRITE_SECRET': SECRET, 'ANDON_CLOUD_CONTENT_KEY': KEY_TEXT,
               'ANDON_CLOUD_KEY_VERSION': '2', 'ANDON_CLOUD_URL': 'http://own.test/'}
        v = AndonView.from_env('ANDON_CLOUD_', env, template=template(), transport=relay, clock=clock)
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertEqual(relay.requests[0]['env']['kv'], 2)
        self.assertTrue(relay.requests[0]['url'].startswith('http://own.test/v1/'))
        v.close()

    def test_from_env_reads_the_env_file_from_the_configurator_and_the_environment_wins(self):
        relay, clock = Relay(), FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('\n'.join([
                '# Andon: values of this view. Keep like a password; the relay cannot show the secrets again.',
                'ANDON_URL=http://file.test',
                'ANDON_VIEW=' + VIEW,
                'ANDON_WRITE_SECRET="' + SECRET + '"',
                'ANDON_INVITE_SECRET=is_whatever',
                "export ANDON_CONTENT_KEY='" + KEY_TEXT + "'",
                'ANDON_KEY_VERSION=1',
                '',
            ]), 'utf-8')
            v = AndonView.from_env(env={'ANDON_KEY_VERSION': '3'}, env_file=path, template=template(),
                                   transport=relay, clock=clock)
        v.set('t_oee', 81)
        clock.advance(0)
        self.assertTrue(relay.requests[0]['url'].startswith('http://file.test/v1/'))
        self.assertEqual(relay.requests[0]['env']['kv'], 3, 'the environment wins over the file')
        v.close()

    def test_from_env_names_a_variable_nobody_set(self):
        with self.assertRaisesRegex(ValueError, '^ANDON_WRITE_SECRET is not set$'):
            AndonView.from_env(env={'ANDON_VIEW': VIEW})
        with self.assertRaisesRegex(ValueError, 'upper case'):
            AndonView.from_env('andon', {})


if __name__ == '__main__':
    unittest.main()
