"""Tests for the certificate-trust page and the CA download.

The download's headers are the fiddly part: iOS only offers to install a
configuration profile for the right mimetype served inline. As an attachment the
file lands in Files, where it cannot be installed from at all — a silent
regression that would only show up on a phone.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tls_certs
from app import app


class CertRouteTestCase(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.app = app.test_client()

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(tls_certs, 'CERT_DIR', os.path.join(self._tmp.name, 'certs'))
        patcher.start()
        self.addCleanup(patcher.stop)

        env = patch.dict(os.environ, {'PYWHISPR_TLS': 'on', 'PYWHISPR_TLS_HOSTS': 'moria.local'})
        env.start()
        self.addCleanup(env.stop)


class TestWithCertificates(CertRouteTestCase):
    def setUp(self):
        super().setUp()
        tls_certs.ensure_certificates(['moria.local', '192.168.1.10'])

    def test_page_lists_what_the_certificate_covers(self):
        response = self.app.get('/cert')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('moria.local', body)
        self.assertIn('192.168.1.10', body)

    def test_page_spells_out_the_trust_step(self):
        # Installing the profile without this leaves the microphone blocked, so
        # the instructions are load-bearing, not decoration.
        body = self.app.get('/cert').get_data(as_text=True)
        self.assertIn('Certificate Trust Settings', body)

    def test_page_shows_the_ca_fingerprint(self):
        info = tls_certs.certificate_info()
        self.assertIn(info.ca_fingerprint, self.app.get('/cert').get_data(as_text=True))

    def test_page_points_at_the_https_port_on_the_current_host(self):
        """Bookmarking the plain-HTTP address is the easy mistake to make."""
        body = self.app.get('/cert', headers={'Host': 'moria.local:5000'}).get_data(as_text=True)
        self.assertIn('https://moria.local:5443', body)

    def test_warns_when_the_hostname_is_not_in_the_certificate(self):
        """Better a warning here than an unexplained TLS error on the phone."""
        body = self.app.get('/cert', headers={'Host': 'elsewhere.local'}).get_data(as_text=True)
        self.assertIn('not valid for', body)
        self.assertIn('elsewhere.local', body)

    def test_no_warning_when_the_hostname_is_covered(self):
        body = self.app.get('/cert', headers={'Host': 'moria.local:5000'}).get_data(as_text=True)
        self.assertNotIn('not valid for', body)

    def test_bracketed_ipv6_host_is_understood(self):
        """Splitting on ':' would leave '[' as the hostname and warn wrongly."""
        tls_certs.ensure_certificates(['moria.local', '::1'])
        body = self.app.get('/cert', headers={'Host': '[::1]:5000'}).get_data(as_text=True)
        self.assertNotIn('not valid for', body)
        # And the link back out has to stay bracketed to be a usable URL.
        self.assertIn('https://[::1]:5443', body)

    def test_download_serves_the_ca_certificate(self):
        response = self.app.get('/cert/pywhispr-ca.crt')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'-----BEGIN CERTIFICATE-----', response.data)

    def test_download_serves_the_ca_and_not_the_leaf(self):
        """Trusting the leaf instead would break on every reissue."""
        with open(tls_certs.paths().ca_cert, 'rb') as handle:
            self.assertEqual(self.app.get('/cert/pywhispr-ca.crt').data, handle.read())

    def test_download_mimetype_is_what_ios_acts_on(self):
        response = self.app.get('/cert/pywhispr-ca.crt')
        self.assertEqual(response.mimetype, 'application/x-x509-ca-cert')

    def test_download_is_inline_not_an_attachment(self):
        disposition = self.app.get('/cert/pywhispr-ca.crt').headers.get('Content-Disposition', '')
        self.assertNotIn('attachment', disposition)


class TestWithoutCertificates(CertRouteTestCase):
    def test_page_explains_itself_when_tls_is_off(self):
        response = self.app.get('/cert')
        self.assertEqual(response.status_code, 200)
        self.assertIn('No certificate has been generated', response.get_data(as_text=True))

    def test_download_is_a_404(self):
        self.assertEqual(self.app.get('/cert/pywhispr-ca.crt').status_code, 404)


class TestStillOpen(CertRouteTestCase):
    def test_the_new_routes_need_no_credentials(self):
        # The app has no authentication; nothing may redirect to a login page.
        tls_certs.ensure_certificates(['moria.local'])
        for path in ('/cert', '/cert/pywhispr-ca.crt'):
            with self.subTest(path=path):
                self.assertEqual(self.app.get(path).status_code, 200)


if __name__ == '__main__':
    unittest.main()
