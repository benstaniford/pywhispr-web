"""Tests for the generated TLS material.

Most of these assert Apple's rules rather than ours. iOS silently refuses to
trust a certificate that lacks a SAN, a serverAuth EKU, or that lives too long,
and the failure surfaces on a phone as an unexplained browser warning — a long
way from the code that caused it. So the rules are pinned here.
"""

import datetime as dt
import ipaddress
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tls_certs


def load(path):
    with open(path, 'rb') as handle:
        return x509.load_pem_x509_certificate(handle.read())


class CertTestCase(unittest.TestCase):
    """Points the module at a scratch directory, so nothing touches /data."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cert_dir = os.path.join(self._tmp.name, 'certs')

        patcher = patch.object(tls_certs, 'CERT_DIR', self.cert_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        # A stray PYWHISPR_TLS/PYWHISPR_TLS_HOSTS would change what gets built.
        env = patch.dict(os.environ, {'PYWHISPR_TLS': 'on', 'PYWHISPR_TLS_HOSTS': ''})
        env.start()
        self.addCleanup(env.stop)


class TestAppleRequirements(CertTestCase):
    """The constraints iOS 13+ enforces on a server certificate."""

    def setUp(self):
        super().setUp()
        self.paths = tls_certs.ensure_certificates(['pywhispr.local', '192.168.1.10'])
        self.leaf = load(self.paths.cert)
        self.ca = load(self.paths.ca_cert)

    def test_san_carries_dns_names(self):
        san = self.leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn('pywhispr.local', san.get_values_for_type(x509.DNSName))

    def test_san_carries_ip_addresses_as_addresses(self):
        """An IP has to be an iPAddress entry; iOS will not read it as a DNS name."""
        san = self.leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn(ipaddress.ip_address('192.168.1.10'), san.get_values_for_type(x509.IPAddress))

    def test_leaf_has_server_auth_eku(self):
        eku = self.leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, list(eku))

    def test_signed_with_sha256(self):
        self.assertIsInstance(self.leaf.signature_hash_algorithm, hashes.SHA256)
        self.assertIsInstance(self.ca.signature_hash_algorithm, hashes.SHA256)

    def test_leaf_lifetime_is_inside_apples_ceiling(self):
        """Apple rejects anything over 825 days."""
        lifetime = tls_certs._expires_at(self.leaf) - dt.datetime.now(dt.timezone.utc)
        self.assertLessEqual(lifetime.days, 825)
        self.assertGreater(lifetime.days, 300)

    def test_key_is_at_least_2048_bits(self):
        public_key = self.leaf.public_key()
        self.assertIsInstance(public_key, rsa.RSAPublicKey)
        self.assertGreaterEqual(public_key.key_size, 2048)

    def test_certificate_is_valid_now(self):
        now = dt.datetime.now(dt.timezone.utc)
        not_before = getattr(self.leaf, 'not_valid_before_utc', None) \
            or self.leaf.not_valid_before.replace(tzinfo=dt.timezone.utc)
        self.assertLess(not_before, now)


class TestChain(CertTestCase):
    def setUp(self):
        super().setUp()
        self.paths = tls_certs.ensure_certificates(['localhost'])
        self.leaf = load(self.paths.cert)
        self.ca = load(self.paths.ca_cert)

    def test_ca_is_a_ca_and_the_leaf_is_not(self):
        ca_constraints = self.ca.extensions.get_extension_for_class(x509.BasicConstraints).value
        leaf_constraints = self.leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        self.assertTrue(ca_constraints.ca)
        self.assertEqual(ca_constraints.path_length, 0)
        self.assertFalse(leaf_constraints.ca)

    def test_leaf_is_signed_by_the_ca(self):
        ca_public_key = self.ca.public_key()
        self.assertIsInstance(ca_public_key, rsa.RSAPublicKey)
        # Raises InvalidSignature if the leaf did not come from this CA.
        ca_public_key.verify(
            self.leaf.signature,
            self.leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            self.leaf.signature_hash_algorithm,
        )
        self.assertEqual(self.leaf.issuer, self.ca.subject)

    def test_cert_file_holds_the_full_chain(self):
        """Leaf then CA, because Gunicorn passes this to load_cert_chain()."""
        with open(self.paths.cert, 'rb') as handle:
            chain = x509.load_pem_x509_certificates(handle.read())
        self.assertEqual(len(chain), 2)
        self.assertFalse(chain[0].extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(chain[1].subject, self.ca.subject)


class TestIdempotence(CertTestCase):
    """The CA must survive, or every device has to trust it again."""

    def test_second_call_changes_nothing(self):
        first = tls_certs.ensure_certificates(['localhost'])
        ca_serial = load(first.ca_cert).serial_number
        leaf_serial = load(first.cert).serial_number

        second = tls_certs.ensure_certificates(['localhost'])

        self.assertEqual(load(second.ca_cert).serial_number, ca_serial)
        self.assertEqual(load(second.cert).serial_number, leaf_serial)

    def test_new_host_reissues_the_leaf_but_keeps_the_ca(self):
        first = tls_certs.ensure_certificates(['localhost'])
        ca_serial = load(first.ca_cert).serial_number
        leaf_serial = load(first.cert).serial_number

        second = tls_certs.ensure_certificates(['localhost', 'moria.local'])

        self.assertEqual(load(second.ca_cert).serial_number, ca_serial,
                         'the CA must be reused, or trust has to be re-established')
        self.assertNotEqual(load(second.cert).serial_number, leaf_serial)
        san = load(second.cert).extensions \
            .get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn('moria.local', san.get_values_for_type(x509.DNSName))

    def test_host_order_alone_does_not_reissue(self):
        first = tls_certs.ensure_certificates(['localhost', 'moria.local'])
        leaf_serial = load(first.cert).serial_number

        second = tls_certs.ensure_certificates(['moria.local', 'localhost'])

        self.assertEqual(load(second.cert).serial_number, leaf_serial)

    def test_expiring_leaf_is_reissued(self):
        """A long-running container must not serve a leaf until it dies."""
        with patch.object(tls_certs, 'LEAF_DAYS', 5):
            first = tls_certs.ensure_certificates(['localhost'])
            short_serial = load(first.cert).serial_number

        second = tls_certs.ensure_certificates(['localhost'])

        self.assertNotEqual(load(second.cert).serial_number, short_serial)
        self.assertGreater(
            tls_certs._expires_at(load(second.cert)) - dt.datetime.now(dt.timezone.utc),
            dt.timedelta(days=tls_certs.RENEW_WITHIN_DAYS))


class TestFilePermissions(CertTestCase):
    def test_private_keys_are_not_readable_by_others(self):
        paths = tls_certs.ensure_certificates(['localhost'])
        for key_path in (paths.key, paths.ca_key):
            mode = stat.S_IMODE(os.stat(key_path).st_mode)
            self.assertEqual(mode, 0o600, f'{key_path} is mode {mode:o}')

    def test_certificates_are_readable(self):
        paths = tls_certs.ensure_certificates(['localhost'])
        for cert_path in (paths.cert, paths.ca_cert):
            self.assertTrue(stat.S_IMODE(os.stat(cert_path).st_mode) & stat.S_IRUSR)


class TestHostSelection(CertTestCase):
    def test_localhost_is_always_covered(self):
        """The health check and container tests reach TLS on localhost."""
        with patch.dict(os.environ, {'PYWHISPR_TLS_HOSTS': 'moria.local'}):
            hosts = tls_certs.configured_hosts()
        self.assertIn('moria.local', hosts)
        self.assertIn('localhost', hosts)
        self.assertIn('127.0.0.1', hosts)

    def test_configured_hosts_come_first(self):
        with patch.dict(os.environ, {'PYWHISPR_TLS_HOSTS': ' moria , 192.168.1.10 '}):
            hosts = tls_certs.configured_hosts()
        self.assertEqual(hosts[:2], ['moria', '192.168.1.10'])

    def test_no_duplicates_when_localhost_is_configured(self):
        with patch.dict(os.environ, {'PYWHISPR_TLS_HOSTS': 'localhost'}):
            hosts = tls_certs.configured_hosts()
        self.assertEqual(hosts.count('localhost'), 1)


class TestSwitchedOff(CertTestCase):
    def test_nothing_is_generated_when_tls_is_off(self):
        with patch.dict(os.environ, {'PYWHISPR_TLS': 'off'}):
            self.assertIsNone(tls_certs.ensure_certificates(['localhost']))
            self.assertIsNone(tls_certs.paths_if_available())
        self.assertFalse(os.path.exists(self.cert_dir))

    def test_paths_if_available_is_none_before_generation(self):
        self.assertIsNone(tls_certs.paths_if_available())

    def test_paths_if_available_finds_generated_material(self):
        tls_certs.ensure_certificates(['localhost'])
        found = tls_certs.paths_if_available()
        self.assertIsNotNone(found)
        self.assertTrue(os.path.exists(found.cert))


class TestCertificateInfo(CertTestCase):
    def test_info_is_none_without_certificates(self):
        self.assertIsNone(tls_certs.certificate_info())

    def test_info_describes_what_was_generated(self):
        tls_certs.ensure_certificates(['moria.local', '192.168.1.10'])
        info = tls_certs.certificate_info()

        self.assertIn('moria.local', info.hosts)
        self.assertIn('192.168.1.10', info.hosts)
        self.assertGreater(info.not_after, dt.datetime.now(dt.timezone.utc))
        # The CA has to outlive the leaf, or trusting it would need redoing.
        self.assertGreater(info.ca_not_after, info.not_after)

    def test_fingerprint_matches_the_ca_on_disk(self):
        paths = tls_certs.ensure_certificates(['localhost'])
        expected = load(paths.ca_cert).fingerprint(hashes.SHA256()).hex(':', 1).upper()
        self.assertEqual(tls_certs.certificate_info().ca_fingerprint, expected)


class TestRecovery(CertTestCase):
    def test_a_missing_ca_key_regenerates_the_authority(self):
        """A crash between writing the key and the certificate must self-heal."""
        first = tls_certs.ensure_certificates(['localhost'])
        os.unlink(first.ca_key)

        second = tls_certs.ensure_certificates(['localhost'])

        self.assertTrue(os.path.exists(second.ca_key))
        self.assertEqual(load(second.cert).issuer, load(second.ca_cert).subject)

    def test_a_corrupt_certificate_is_replaced(self):
        first = tls_certs.ensure_certificates(['localhost'])
        with open(first.cert, 'w', encoding='utf-8') as handle:
            handle.write('not a certificate\n')

        second = tls_certs.ensure_certificates(['localhost'])

        self.assertEqual(load(second.cert).issuer, load(second.ca_cert).subject)


if __name__ == '__main__':
    unittest.main()
