"""
The real thing: the real clock and its thread, urllib, and a relay stand-in on
localhost that answers as the relay does. Slower than the rest, a few seconds.
"""

import base64
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from andon_client import AndonView

from helpers import KEY, SAMPLE, SECRET, VIEW, open_envelope, template, tile

KEY_TEXT = base64.urlsafe_b64encode(KEY).decode().rstrip('=')


class Server:
    """A relay stand-in: records every PUT, answers from `answers` (status, body, headers), 204 after."""

    def __init__(self):
        self.requests = []
        self.answers = []
        self.arrived = threading.Condition()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers['content-length'])).decode()
                # Recorded before the answer: once the client has its 204, the test
                # may look, and the request has to be there.
                with server.arrived:
                    server.requests.append({'path': self.path, 'auth': self.headers['authorization'], 'body': body})
                    status, text, headers = server.answers.pop(0) if server.answers else (204, '', {})
                    server.arrived.notify_all()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header('content-length', str(len(text.encode())))
                self.end_headers()
                self.wfile.write(text.encode())

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait(self, n, timeout=10):
        with self.arrived:
            return self.arrived.wait_for(lambda: len(self.requests) >= n, timeout)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def workers():
    return [t for t in threading.enumerate() if t.name == 'andon-client']


class Thread(unittest.TestCase):
    def setUp(self):
        self.server = Server()

    def tearDown(self):
        self.server.close()

    def test_a_value_goes_out_from_the_thread_and_close_ends_it(self):
        sent = []
        v = AndonView(VIEW, SECRET, KEY_TEXT, template=template(), relay_url=self.server.url, on_sent=sent.append)
        v.set('t_oee', 81)
        self.assertTrue(self.server.wait(1))
        request = self.server.requests[0]
        self.assertEqual(request['path'], '/v1/views/' + VIEW + '/envelope')
        self.assertEqual(request['auth'], 'Bearer ' + SECRET)
        self.assertEqual(tile(open_envelope(request['body'])[1], 't_oee')['value'], 81)
        v.set('t_oee', 82)  # inside the interval: waits for it, or for close
        v.close()
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(tile(open_envelope(self.server.requests[1]['body'])[1], 't_oee')['value'], 82)
        self.assertEqual([r.status for r in sent], [204, 204])
        deadline = time.monotonic() + 5
        while workers() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(workers(), [], 'no thread left behind')

    def test_many_threads_setting_at_once_leave_one_whole_document(self):
        errors = []
        v = AndonView(VIEW, SECRET, KEY_TEXT, template=template(), relay_url=self.server.url, interval_sec=10,
                      on_error=errors.append)

        def feed(k):
            for i in range(200):
                v.set('t_shift', 500 + k * 1000 + i)
                v.set({'t_oee': 50 + (i % 40), 't_order': k * 1000 + i})
                v.set('t_takt', 30 + i / 100)

        threads = [threading.Thread(target=feed, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        last = v.document
        v.close()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.server.requests), 3, 'one upload at once, the rest at close or after an interval')
        self.assertEqual(open_envelope(self.server.requests[-1]['body'])[1]['tiles'], last['tiles'])
        stamps = [s[0] for s in tile(last, 't_takt')['series'][0]['samples']]
        self.assertEqual(stamps, sorted(set(stamps)), 'one sample a second, in order: the later value wins')

    def test_the_relay_s_answers_reach_on_error(self):
        self.server.answers = [
            (429, '{"error":"throttled"}', {'retry-after': '7'}),
            (401, '{"error":"unauthorized"}', {}),
        ]
        errors = []
        v = AndonView(VIEW, SECRET, KEY_TEXT, template=template(), relay_url=self.server.url, on_error=errors.append)
        v.set('t_oee', 81)
        self.assertTrue(self.server.wait(1))
        result = v.flush()  # throttled: still pending, and flush tries at once
        self.assertEqual(result.status, 401)
        self.assertEqual((errors[0].status, errors[0].retry_in_sec), (429, 7))
        self.assertEqual(errors[0].message, 'throttled (throttled): the next upload carries the latest state')
        self.assertEqual(errors[1].message, 'the relay rejected the write secret (unauthorized): use the current .env of the view')
        self.assertIsNone(errors[1].retry_in_sec)
        v.close()

    def test_a_relay_that_is_not_there_is_retried(self):
        port = self.server.httpd.server_address[1]
        self.server.close()
        errors = []
        v = AndonView(VIEW, SECRET, KEY_TEXT, template=template(), relay_url='http://127.0.0.1:%d' % port, on_error=errors.append)
        v.set('t_oee', 81)
        deadline = time.monotonic() + 20
        while not errors and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((errors[0].status, errors[0].retry_in_sec), (0, 30))
        self.assertTrue(errors[0].message.startswith('the relay could not be reached: '), errors[0].message)
        v.close(flush=False)
        self.server = Server()  # for tearDown

    def test_a_script_that_ends_with_the_relay_out_of_reach_tries_once_and_ends(self):
        port = self.server.httpd.server_address[1]
        self.server.close()
        script = textwrap.dedent('''
            import json, sys
            from andon_client import AndonView
            view = AndonView.from_env(template=json.load(open(sys.argv[1], encoding='utf-8')))
            view.set('t_oee', 81)
        ''')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), ANDON_VIEW=VIEW, ANDON_WRITE_SECRET=SECRET,
                   ANDON_CONTENT_KEY=KEY_TEXT, ANDON_URL='http://127.0.0.1:%d' % port)
        started = time.monotonic()
        done = subprocess.run([sys.executable, '-c', script, str(SAMPLE)], env=env, timeout=60, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.monotonic() - started, 25, 'no retries after the program ended')
        self.assertIn('the relay could not be reached', done.stderr)
        self.assertIn('the program has ended, so that was the last try', done.stderr)
        self.server = Server()  # for tearDown

    def test_a_script_that_ends_still_uploads_what_it_set(self):
        # No close(), no flush(): the timer's thread is what keeps Python up until
        # the value is out, as a pending timer keeps Node.js up.
        script = textwrap.dedent('''
            import json, sys
            from andon_client import AndonView
            view = AndonView.from_env(template=json.load(open(sys.argv[1], encoding='utf-8')))
            view.set('t_oee', 81)
            view.set('t_shift', 600)
        ''')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), ANDON_VIEW=VIEW, ANDON_WRITE_SECRET=SECRET,
                   ANDON_CONTENT_KEY=KEY_TEXT, ANDON_URL=self.server.url)
        started = time.monotonic()
        done = subprocess.run([sys.executable, '-c', script, str(SAMPLE)], env=env, timeout=30, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.monotonic() - started, 20, 'and then it ends')
        self.assertEqual(len(self.server.requests), 1)
        doc = open_envelope(self.server.requests[0]['body'])[1]
        self.assertEqual((tile(doc, 't_oee')['value'], tile(doc, 't_shift')['value']), (81, 600))


if __name__ == '__main__':
    unittest.main()
