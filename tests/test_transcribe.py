"""Tests for the Flask routes that proxy to PyWhispr and manage the server list."""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pywhispr_client as pywhispr
from app import app
from tests.test_servers import FakeResponse, health


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.app = app.test_client()

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(pywhispr, 'CONFIG_PATH', os.path.join(self._tmp.name, 'config.json'))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {'PYWHISPR_SERVERS': ''})
        env.start()
        self.addCleanup(env.stop)

    def authenticate(self):
        with self.app.session_transaction() as sess:
            sess['authenticated'] = True

    def write_servers(self, *urls, active=None):
        config = pywhispr.load_config()
        config['servers'] = [{'id': f'id{i}', 'name': f'srv{i}', 'url': url} for i, url in enumerate(urls)]
        config['active'] = active
        pywhispr.save_config(config)

    def post_audio(self, body=b'\x00\x01' * 16):
        return self.app.post('/api/transcribe?sample_rate=16000&channels=1&format=s16le',
                             data=body, content_type='application/octet-stream')


class TestAuthentication(RouteTestCase):
    def test_api_routes_return_401_not_a_redirect(self):
        # A redirect would arrive at fetch() as an opaque HTML success.
        for path in ('/api/servers', '/api/servers/status', '/api/ready'):
            with self.subTest(path=path):
                response = self.app.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json()['error']['code'], 'unauthenticated')

    def test_transcribe_requires_authentication(self):
        self.assertEqual(self.post_audio().status_code, 401)

    def test_settings_page_redirects_with_a_next_parameter(self):
        response = self.app.get('/settings')
        self.assertEqual(response.status_code, 302)
        self.assertIn('next=/settings', response.location)

    def test_login_honours_next_and_refuses_other_origins(self):
        response = self.app.post('/login?next=/settings',
                                 data={'username': 'user', 'password': 'password'})
        self.assertTrue(response.location.endswith('/settings'))

        response = self.app.post('/login?next=https://evil.example.com',
                                 data={'username': 'user', 'password': 'password'})
        self.assertTrue(response.location.endswith('/'))


class TestServerConfigRoutes(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_get_servers_is_empty_initially(self):
        data = self.app.get('/api/servers').get_json()
        self.assertEqual(data['servers'], [])
        self.assertEqual(data['cache_ttl_seconds'], pywhispr.DEFAULT_TTL_SECONDS)

    def test_put_normalises_and_persists(self):
        response = self.app.put('/api/servers', json={
            'servers': [{'name': 'desktop', 'url': '192.168.1.10'}, {'url': 'laptop.local:9150'}],
            'cache_ttl_seconds': 120,
        })
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['servers'][0]['url'], 'http://192.168.1.10:9149')
        self.assertEqual(data['servers'][1]['name'], 'laptop.local')
        self.assertEqual(data['cache_ttl_seconds'], 120)
        self.assertEqual(pywhispr.load_config()['servers'][0]['name'], 'desktop')

    def test_put_preserves_order_as_priority(self):
        self.app.put('/api/servers', json={'servers': [{'url': 'b'}, {'url': 'a'}]})
        urls = [s['url'] for s in pywhispr.load_config()['servers']]
        self.assertEqual(urls, ['http://b:9149', 'http://a:9149'])

    def test_put_invalidates_the_cached_choice(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        self.app.put('/api/servers', json={'servers': [{'url': 'http://a:9149'}]})
        self.assertIsNone(pywhispr.load_config()['active'])

    def test_put_rejects_a_bad_url(self):
        response = self.app.put('/api/servers', json={'servers': [{'url': 'ftp://nope'}]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('http or https', response.get_json()['error']['message'])

    def test_put_rejects_a_non_object_body(self):
        response = self.app.put('/api/servers', data='[]', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_put_clamps_the_ttl(self):
        response = self.app.put('/api/servers', json={'servers': [], 'cache_ttl_seconds': 1})
        self.assertEqual(response.get_json()['cache_ttl_seconds'], pywhispr.MIN_TTL_SECONDS)

    def test_status_route_reports_each_server(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())):
            data = self.app.get('/api/servers/status').get_json()
        self.assertTrue(data['servers'][0]['ready'])
        self.assertEqual(data['servers'][0]['name'], 'srv0')


class TestReadyRoute(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_reports_ready_with_the_recording_limits(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get',
                          return_value=FakeResponse(200, health(max_audio_seconds=42))):
            data = self.app.get('/api/ready').get_json()
        self.assertTrue(data['ready'])
        self.assertEqual(data['server']['name'], 'srv0')
        self.assertEqual(data['max_audio_seconds'], 42)

    def test_reports_not_ready_with_reasons(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health('loading'))):
            data = self.app.get('/api/ready').get_json()
        self.assertFalse(data['ready'])
        self.assertIn('still loading', data['servers'][0]['error'])

    def test_no_servers_configured(self):
        data = self.app.get('/api/ready').get_json()
        self.assertFalse(data['ready'])
        self.assertIn('configured', data['message'])

    def test_a_cached_choice_still_reports_limits(self):
        # select_server() returns no health payload on a cache hit, so the route
        # has to probe for the limits rather than returning None.
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        with patch.object(pywhispr.requests, 'get',
                          return_value=FakeResponse(200, health(max_audio_seconds=77))):
            data = self.app.get('/api/ready').get_json()
        self.assertTrue(data['ready'])
        self.assertEqual(data['max_audio_seconds'], 77)


class TestTranscribeRoute(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_relays_text_and_names_the_server(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post',
                          return_value=FakeResponse(200, {'text': 'hello world'})) as post:
            response = self.post_audio()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['text'], 'hello world')
        self.assertEqual(data['server']['name'], 'srv0')
        # Only the PCM parameters are forwarded, verbatim.
        self.assertEqual(post.call_args.kwargs['params'],
                         {'sample_rate': '16000', 'channels': '1', 'format': 's16le'})
        self.assertEqual(post.call_args.kwargs['headers']['Content-Type'], 'application/octet-stream')

    def test_drops_unexpected_query_parameters(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post',
                          return_value=FakeResponse(200, {'text': 'x'})) as post:
            self.app.post('/api/transcribe?format=s16le&surprise=1', data=b'\x00\x01',
                          content_type='application/octet-stream')
        self.assertEqual(post.call_args.kwargs['params'], {'format': 's16le'})

    def test_empty_body_is_rejected_without_contacting_a_server(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'post') as post:
            response = self.app.post('/api/transcribe', data=b'',
                                     content_type='application/octet-stream')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['code'], 'bad_audio')
        post.assert_not_called()

    def test_fails_over_and_reports_the_server_that_worked(self):
        self.write_servers('http://a:9149', 'http://b:9149',
                           active={'url': 'http://a:9149', 'chosen_at': time.time()})

        def get(url, **kwargs):
            if 'a:' in url:
                raise pywhispr.requests.ConnectionError('refused')
            return FakeResponse(200, health())

        def post(url, **kwargs):
            if 'a:' in url:
                raise pywhispr.requests.ConnectionError('refused')
            return FakeResponse(200, {'text': 'from the second server'})

        with patch.object(pywhispr.requests, 'get', side_effect=get), \
             patch.object(pywhispr.requests, 'post', side_effect=post):
            response = self.post_audio()

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['text'], 'from the second server')
        self.assertEqual(data['server']['name'], 'srv1')

    def test_relays_model_loading_status_and_retry_after(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        loading = FakeResponse(503, {'error': {'code': 'model_loading', 'message': 'loading'}},
                               headers={'Retry-After': '5'})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post', return_value=loading):
            response = self.post_audio()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error']['code'], 'model_loading')
        self.assertEqual(response.headers['Retry-After'], '5')

    def test_no_server_available_returns_503_with_reasons(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get',
                          side_effect=pywhispr.requests.ConnectionError('refused')):
            response = self.post_audio()

        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertEqual(data['error']['code'], 'no_server')
        self.assertEqual(data['servers'][0]['error'], 'could not connect')

    def test_oversized_body_returns_json_413(self):
        self.write_servers('http://a:9149')
        oversized = b'\x00' * (app.config['MAX_CONTENT_LENGTH'] + 1)
        response = self.app.post('/api/transcribe', data=oversized,
                                 content_type='application/octet-stream')
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()['error']['code'], 'payload_too_large')


if __name__ == '__main__':
    unittest.main()
