"""Tests for the server registry, liveness cache and failover ordering."""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pywhispr_client as pywhispr


def health(status='ready', **extra):
    """A fake /v1/health response body."""
    body = {'status': status, 'version': '0.2.5', 'backend': 'fake', 'sample_rate': 16000,
            'max_audio_seconds': 300, 'max_upload_bytes': 38400000,
            'queue_depth': 0, 'max_queue': 4, 'pcm_formats': ['f32le', 's16le']}
    body.update(extra)
    return body


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text_body=None):
        self.status_code = status_code
        self._body = body
        self._text_body = text_body
        self.headers = headers or {}

    def json(self):
        if self._text_body is not None:
            raise ValueError('not json')
        return self._body


class ConfigTestCase(unittest.TestCase):
    """Points the module at a scratch config file, so nothing touches /data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(pywhispr, 'CONFIG_PATH', os.path.join(self._tmp.name, 'config.json'))
        patcher.start()
        self.addCleanup(patcher.stop)
        # A stray PYWHISPR_SERVERS in the environment would seed the "empty" case.
        env = patch.dict(os.environ, {'PYWHISPR_SERVERS': ''})
        env.start()
        self.addCleanup(env.stop)

    def write_servers(self, *urls, ttl=60, active=None):
        config = pywhispr.load_config()
        config['servers'] = [{'id': f'id{i}', 'name': f'srv{i}', 'url': url} for i, url in enumerate(urls)]
        config['cache_ttl_seconds'] = ttl
        config['active'] = active
        pywhispr.save_config(config)
        return config


class TestUrlNormalisation(unittest.TestCase):
    def test_bare_host_gets_scheme_and_default_port(self):
        self.assertEqual(pywhispr.normalise_url('192.168.1.10'), 'http://192.168.1.10:9149')

    def test_explicit_port_is_kept(self):
        self.assertEqual(pywhispr.normalise_url('box.local:8080'), 'http://box.local:8080')

    def test_trailing_slash_is_stripped(self):
        self.assertEqual(pywhispr.normalise_url('http://host:9149/'), 'http://host:9149')

    def test_https_keeps_its_implicit_port(self):
        self.assertEqual(pywhispr.normalise_url('https://example.com'), 'https://example.com')

    def test_non_http_scheme_is_rejected(self):
        with self.assertRaises(pywhispr.ConfigError):
            pywhispr.normalise_url('ftp://host')

    def test_empty_is_rejected(self):
        with self.assertRaises(pywhispr.ConfigError):
            pywhispr.normalise_url('   ')

    def test_duplicate_urls_are_rejected(self):
        with self.assertRaises(pywhispr.ConfigError):
            pywhispr.normalise_servers([{'url': 'host'}, {'url': 'http://host:9149'}])

    def test_name_defaults_to_hostname(self):
        servers = pywhispr.normalise_servers([{'url': 'box.local:9149'}])
        self.assertEqual(servers[0]['name'], 'box.local')

    def test_ttl_is_clamped(self):
        self.assertEqual(pywhispr.clamp_ttl(1), pywhispr.MIN_TTL_SECONDS)
        self.assertEqual(pywhispr.clamp_ttl(99999), pywhispr.MAX_TTL_SECONDS)
        self.assertEqual(pywhispr.clamp_ttl('not a number'), pywhispr.DEFAULT_TTL_SECONDS)
        self.assertEqual(pywhispr.clamp_ttl(90), 90)


class TestConfigFile(ConfigTestCase):
    def test_missing_file_gives_blank_config(self):
        config = pywhispr.load_config()
        self.assertEqual(config['servers'], [])
        self.assertEqual(config['cache_ttl_seconds'], pywhispr.DEFAULT_TTL_SECONDS)
        self.assertIsNone(config['active'])

    def test_round_trip(self):
        self.write_servers('http://a:9149', 'http://b:9149', ttl=120)
        config = pywhispr.load_config()
        self.assertEqual([s['url'] for s in config['servers']], ['http://a:9149', 'http://b:9149'])
        self.assertEqual(config['cache_ttl_seconds'], 120)

    def test_corrupt_file_does_not_raise(self):
        with open(pywhispr.CONFIG_PATH, 'w', encoding='utf-8') as handle:
            handle.write('{not json at all')
        self.assertEqual(pywhispr.load_config()['servers'], [])

    def test_seeds_from_environment_on_first_run(self):
        with patch.dict(os.environ, {'PYWHISPR_SERVERS': 'desktop=192.168.1.10,laptop.local:9150'}):
            config = pywhispr.load_config()
        self.assertEqual([s['name'] for s in config['servers']], ['desktop', 'laptop.local'])
        self.assertEqual(config['servers'][0]['url'], 'http://192.168.1.10:9149')
        self.assertEqual(config['servers'][1]['url'], 'http://laptop.local:9150')


class TestProbe(ConfigTestCase):
    def test_ready_server(self):
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())):
            verdict = pywhispr.probe('http://a:9149')
        self.assertTrue(verdict['ok'])
        self.assertTrue(verdict['ready'])
        self.assertEqual(verdict['max_audio_seconds'], 300)

    def test_loading_server_is_not_ready_and_says_why(self):
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health('loading'))):
            verdict = pywhispr.probe('http://a:9149')
        self.assertTrue(verdict['ok'])
        self.assertFalse(verdict['ready'])
        self.assertIn('still loading', verdict['error'])

    def test_connection_error_is_reported_not_raised(self):
        with patch.object(pywhispr.requests, 'get',
                          side_effect=pywhispr.requests.ConnectionError('refused')):
            verdict = pywhispr.probe('http://a:9149')
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['error'], 'could not connect')

    def test_non_json_body_is_reported(self):
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, text_body='<html>')):
            verdict = pywhispr.probe('http://a:9149')
        self.assertFalse(verdict['ok'])
        self.assertIn('did not return JSON', verdict['error'])


class TestSelectServer(ConfigTestCase):
    def test_no_servers_configured_raises_with_no_verdicts(self):
        with self.assertRaises(pywhispr.NoServerAvailable) as caught:
            pywhispr.select_server()
        self.assertEqual(caught.exception.verdicts, [])
        self.assertIn('configured', str(caught.exception))

    def test_picks_the_first_ready_server_in_order(self):
        self.write_servers('http://a:9149', 'http://b:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())) as get:
            server = pywhispr.select_server()
        self.assertEqual(server['url'], 'http://a:9149')
        # Short-circuits: the second server is never probed.
        self.assertEqual(get.call_count, 1)

    def test_skips_unreachable_and_loading_servers(self):
        self.write_servers('http://down:9149', 'http://warming:9149', 'http://good:9149')

        def responses(url, **kwargs):
            if 'down' in url:
                raise pywhispr.requests.ConnectionError('refused')
            if 'warming' in url:
                return FakeResponse(200, health('loading'))
            return FakeResponse(200, health())

        with patch.object(pywhispr.requests, 'get', side_effect=responses):
            server = pywhispr.select_server()
        self.assertEqual(server['url'], 'http://good:9149')

    def test_cache_hit_does_not_probe_at_all(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        with patch.object(pywhispr.requests, 'get') as get:
            server = pywhispr.select_server()
        self.assertEqual(server['url'], 'http://a:9149')
        self.assertTrue(server['cached'])
        get.assert_not_called()

    def test_expired_cache_reprobes(self):
        stale = time.time() - 61
        self.write_servers('http://a:9149', ttl=60, active={'url': 'http://a:9149', 'chosen_at': stale})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())) as get:
            pywhispr.select_server()
        get.assert_called_once()

    def test_force_ignores_a_valid_cache(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())) as get:
            pywhispr.select_server(force=True)
        get.assert_called_once()

    def test_cached_server_removed_from_the_list_is_ignored(self):
        # Editing the list must take effect immediately, not after the TTL.
        self.write_servers('http://b:9149', active={'url': 'http://gone:9149', 'chosen_at': time.time()})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())):
            server = pywhispr.select_server()
        self.assertEqual(server['url'], 'http://b:9149')

    def test_selection_is_persisted_for_other_workers(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())):
            pywhispr.select_server()
        self.assertEqual(pywhispr.load_config()['active']['url'], 'http://a:9149')

    def test_no_ready_server_raises_with_per_server_verdicts(self):
        self.write_servers('http://down:9149', 'http://warming:9149')

        def responses(url, **kwargs):
            if 'down' in url:
                raise pywhispr.requests.ConnectionError('refused')
            return FakeResponse(200, health('loading'))

        with patch.object(pywhispr.requests, 'get', side_effect=responses):
            with self.assertRaises(pywhispr.NoServerAvailable) as caught:
                pywhispr.select_server()

        verdicts = caught.exception.verdicts
        self.assertEqual(len(verdicts), 2)
        self.assertEqual(verdicts[0]['error'], 'could not connect')
        self.assertIn('still loading', verdicts[1]['error'])

    def test_failure_clears_a_stale_cached_choice(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time() - 999})
        with patch.object(pywhispr.requests, 'get',
                          side_effect=pywhispr.requests.ConnectionError('refused')):
            with self.assertRaises(pywhispr.NoServerAvailable):
                pywhispr.select_server()
        self.assertIsNone(pywhispr.load_config()['active'])

    def test_invalidate_clears_the_cache(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        pywhispr.invalidate()
        self.assertIsNone(pywhispr.load_config()['active'])


class TestProbeAll(ConfigTestCase):
    def test_reports_every_server_with_the_active_one_flagged(self):
        self.write_servers('http://a:9149', 'http://b:9149',
                           active={'url': 'http://b:9149', 'chosen_at': time.time()})

        def responses(url, **kwargs):
            if 'a:' in url:
                raise pywhispr.requests.Timeout('slow')
            return FakeResponse(200, health())

        with patch.object(pywhispr.requests, 'get', side_effect=responses):
            verdicts = pywhispr.probe_all()

        self.assertEqual(len(verdicts), 2)
        self.assertFalse(verdicts[0]['ready'])
        self.assertEqual(verdicts[0]['error'], 'timed out')
        self.assertFalse(verdicts[0]['active'])
        self.assertTrue(verdicts[1]['ready'])
        self.assertTrue(verdicts[1]['active'])
        self.assertEqual(verdicts[0]['name'], 'srv0')

    def test_empty_list_probes_nothing(self):
        with patch.object(pywhispr.requests, 'get') as get:
            self.assertEqual(pywhispr.probe_all(), [])
        get.assert_not_called()


class TestTranscribe(ConfigTestCase):
    def test_happy_path_returns_upstream_text(self):
        self.write_servers('http://a:9149')
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post',
                          return_value=FakeResponse(200, {'text': 'hello world'})) as post:
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {'format': 's16le'})

        self.assertTrue(result.ok)
        self.assertEqual(result.body['text'], 'hello world')
        self.assertEqual(result.server['url'], 'http://a:9149')
        # Content-Length must be explicit: PyWhispr rejects chunked bodies (411).
        self.assertEqual(post.call_args.kwargs['headers']['Content-Length'], '5')

    def test_fails_over_to_the_next_server_on_a_connection_error(self):
        self.write_servers('http://a:9149', 'http://b:9149',
                           active={'url': 'http://a:9149', 'chosen_at': time.time()})

        def get(url, **kwargs):
            if 'a:' in url:
                raise pywhispr.requests.ConnectionError('refused')
            return FakeResponse(200, health())

        def post(url, **kwargs):
            if 'a:' in url:
                raise pywhispr.requests.ConnectionError('refused')
            return FakeResponse(200, {'text': 'from b'})

        with patch.object(pywhispr.requests, 'get', side_effect=get), \
             patch.object(pywhispr.requests, 'post', side_effect=post):
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {})

        self.assertTrue(result.ok)
        self.assertEqual(result.body['text'], 'from b')
        self.assertEqual(result.server['url'], 'http://b:9149')

    def test_does_not_retry_a_client_error(self):
        # Bad audio will not transcribe any better somewhere else.
        self.write_servers('http://a:9149', 'http://b:9149',
                           active={'url': 'http://a:9149', 'chosen_at': time.time()})
        bad = FakeResponse(400, {'error': {'code': 'bad_audio', 'message': 'empty'}})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post', return_value=bad) as post:
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {})

        self.assertEqual(result.status, 400)
        self.assertEqual(post.call_count, 1)

    def test_does_not_retry_the_same_server_twice(self):
        # Only one server, and it is 503 busy: report it rather than looping.
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        busy = FakeResponse(503, {'error': {'code': 'busy', 'message': 'too many'}},
                            headers={'Retry-After': '2'})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post', return_value=busy) as post:
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {})

        self.assertEqual(result.status, 503)
        self.assertEqual(result.body['error']['code'], 'busy')
        self.assertEqual(result.retry_after, '2')
        self.assertEqual(post.call_count, 1)

    def test_relays_model_loading_with_its_retry_after(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        loading = FakeResponse(503, {'error': {'code': 'model_loading', 'message': 'loading'}},
                               headers={'Retry-After': '5'})
        with patch.object(pywhispr.requests, 'get', return_value=FakeResponse(200, health())), \
             patch.object(pywhispr.requests, 'post', return_value=loading):
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {})

        self.assertEqual(result.body['error']['code'], 'model_loading')
        self.assertEqual(result.retry_after, '5')

    def test_unreachable_only_server_reports_a_502(self):
        self.write_servers('http://a:9149', active={'url': 'http://a:9149', 'chosen_at': time.time()})
        with patch.object(pywhispr.requests, 'get',
                          side_effect=pywhispr.requests.ConnectionError('refused')), \
             patch.object(pywhispr.requests, 'post',
                          side_effect=pywhispr.requests.ConnectionError('refused')):
            result = pywhispr.transcribe(b'audio', 'application/octet-stream', {})

        self.assertEqual(result.status, 502)
        self.assertEqual(result.body['error']['code'], 'server_unreachable')

    def test_no_server_configured_raises(self):
        with self.assertRaises(pywhispr.NoServerAvailable):
            pywhispr.transcribe(b'audio', 'application/octet-stream', {})


if __name__ == '__main__':
    unittest.main()
