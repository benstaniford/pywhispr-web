"""Self-signed TLS material, generated on first start and kept on the volume.

Recording needs a secure context: browsers only expose ``getUserMedia`` over
HTTPS or on localhost, so from a phone this app is useless over plain HTTP.
Rather than require a reverse proxy for a single-user LAN service, the container
issues its own certificates and serves TLS itself.

Two certificates, not one:

* A **root CA**, valid for ten years, is the only thing that ever goes on a
  phone. It lives on the ``/data`` volume, so it survives image upgrades and
  container recreation — a CA regenerated on every deploy would mean
  re-trusting after every release, which would make the feature pointless.
* A **leaf**, valid for a year, carries the hostnames. Because the phone trusts
  the CA rather than the leaf, the leaf can be reissued freely when it expires
  or when the host list changes, and nobody has to re-trust anything.

iOS is the constraint that shapes the leaf. Since iOS 13 a certificate is
rejected outright unless it has a ``subjectAltName`` (the CN is ignored
entirely, and browsing by IP needs an ``iPAddress`` entry), an
``extendedKeyUsage`` of ``serverAuth``, a SHA-256 signature, and a lifetime of
825 days or less. A stock ``openssl req -x509`` satisfies none of those, which
is why this is generated here in Python where the rules can be unit-tested.

Generation happens once, in the supervisor, before any Gunicorn master loads —
``preload_app`` means the config file needs the files to already exist. There is
no Flask import here on purpose: all of this is testable on its own.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import ipaddress
import logging
import os
import socket
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger(__name__)

CERT_DIR = os.environ.get('PYWHISPR_CERT_DIR', '/data/certs')

CA_NAME = 'PyWhispr Web CA'
LEAF_NAME = 'PyWhispr Web'

CA_DAYS = 3650
# Well inside Apple's 825-day ceiling for server certificates. The leaf is
# cheap to reissue, so there is nothing to gain from pushing it.
LEAF_DAYS = 365
# Reissue before it actually expires, so a long-running container does not serve
# an expired certificate while waiting for its next restart.
RENEW_WITHIN_DAYS = 30

KEY_SIZE = 2048

# Always present, so the health check and the container tests can reach TLS
# regardless of what the user configured.
IMPLICIT_HOSTS = ('localhost', '127.0.0.1', '::1')


class CertPaths(NamedTuple):
    """Where the generated material ended up."""

    ca_cert: str
    ca_key: str
    cert: str
    key: str


class CertInfo(NamedTuple):
    """What the /cert page needs to describe the current certificates."""

    hosts: list[str]
    not_after: dt.datetime
    ca_not_after: dt.datetime
    ca_fingerprint: str


def tls_enabled() -> bool:
    """Whether to serve TLS at all.

    On by default: HTTPS is what makes the app work from a phone. ``off`` is the
    escape hatch for anyone already terminating TLS in a reverse proxy.
    """
    return os.environ.get('PYWHISPR_TLS', 'on').strip().lower() not in ('off', '0', 'false', 'no')


def configured_hosts() -> list[str]:
    """The names and addresses the leaf should be valid for.

    Deliberately does not try to discover a usable LAN address. Inside a bridge
    network the container only sees a ``172.x`` address that no phone can reach,
    so guessing would produce a certificate that looks fine and fails in use.
    An unset ``PYWHISPR_TLS_HOSTS`` is surfaced as a warning on the /cert page
    instead.
    """
    raw = os.environ.get('PYWHISPR_TLS_HOSTS', '')
    hosts = [entry.strip() for entry in raw.split(',') if entry.strip()]

    for implicit in (*IMPLICIT_HOSTS, socket.gethostname()):
        if implicit and implicit not in hosts:
            hosts.append(implicit)

    return hosts


def paths() -> CertPaths:
    """The paths the certificates live at, whether or not they exist yet."""
    return CertPaths(
        ca_cert=os.path.join(CERT_DIR, 'ca.crt'),
        ca_key=os.path.join(CERT_DIR, 'ca.key'),
        cert=os.path.join(CERT_DIR, 'server.crt'),
        key=os.path.join(CERT_DIR, 'server.key'),
    )


def paths_if_available() -> CertPaths | None:
    """The paths, but only when TLS is on and the material is actually there.

    Used by the Gunicorn config, which must not fail to load just because
    something upstream skipped generation.
    """
    if not tls_enabled():
        return None
    found = paths()
    if not (os.path.exists(found.cert) and os.path.exists(found.key)):
        return None
    return found


def ca_pem_path() -> str | None:
    """The CA certificate to hand out for trusting, if there is one."""
    found = paths()
    return found.ca_cert if os.path.exists(found.ca_cert) else None


# -- reading ------------------------------------------------------------------


def _load(path: str) -> x509.Certificate | None:
    try:
        with open(path, 'rb') as handle:
            # A leaf file holds the chain, so read only the first certificate.
            return x509.load_pem_x509_certificate(handle.read())
    except (FileNotFoundError, ValueError):
        return None


def _load_ca_key(path: str) -> rsa.RSAPrivateKey | None:
    try:
        with open(path, 'rb') as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
    except (FileNotFoundError, ValueError, TypeError):
        return None
    return key if isinstance(key, rsa.RSAPrivateKey) else None


def _san_hosts(cert: x509.Certificate) -> list[str]:
    """The SAN entries as the strings the user typed, DNS names and IPs alike."""
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [str(name) for name in san.get_values_for_type(x509.DNSName)] + \
           [str(address) for address in san.get_values_for_type(x509.IPAddress)]


def _expires_at(cert: x509.Certificate) -> dt.datetime:
    """not_valid_after as an aware UTC datetime, across cryptography versions."""
    getter = getattr(cert, 'not_valid_after_utc', None)
    if getter is not None:
        return getter
    return cert.not_valid_after.replace(tzinfo=dt.timezone.utc)


def certificate_info() -> CertInfo | None:
    """Describe the certificates on disk, or None if they are not there."""
    found = paths()
    leaf = _load(found.cert)
    ca = _load(found.ca_cert)
    if leaf is None or ca is None:
        return None

    return CertInfo(
        hosts=_san_hosts(leaf),
        not_after=_expires_at(leaf),
        ca_not_after=_expires_at(ca),
        ca_fingerprint=ca.fingerprint(hashes.SHA256()).hex(':', 1).upper(),
    )


# -- writing ------------------------------------------------------------------


def _write(path: str, data: bytes, mode: int) -> None:
    """Write atomically, so a crash mid-write cannot leave a truncated key.

    The temp file is created with the final mode rather than chmod'ed
    afterwards, so a private key is never briefly world-readable.
    """
    tmp_path = f'{path}.{os.getpid()}.tmp'
    handle = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)


def _san(hosts: list[str]) -> x509.SubjectAlternativeName:
    """Build the SAN, classifying each entry as an address or a DNS name."""
    entries: list[x509.GeneralName] = []
    for host in hosts:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            entries.append(x509.DNSName(host))
    return x509.SubjectAlternativeName(entries)


def _serial() -> int:
    return x509.random_serial_number()


def _validity(days: int) -> tuple[dt.datetime, dt.datetime]:
    # Backdated slightly so a client whose clock runs behind ours — a phone that
    # has just woken up, say — does not see a not-yet-valid certificate.
    now = dt.datetime.now(dt.timezone.utc)
    return now - dt.timedelta(minutes=5), now + dt.timedelta(days=days)


def _make_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    not_before, not_after = _validity(CA_DAYS)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        # pathlen 0: this CA signs leaves only, never another CA.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _make_leaf(hosts: list[str], ca_cert: x509.Certificate,
               ca_key: rsa.RSAPrivateKey) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    not_before, not_after = _validity(LEAF_DAYS)

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, LEAF_NAME)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # Both of the next two are load-bearing on iOS: without a SAN the
        # certificate is rejected regardless of its CN, and without serverAuth
        # it will not be used for TLS.
        .add_extension(_san(hosts), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _leaf_is_current(found: CertPaths, hosts: list[str]) -> bool:
    """Whether the leaf on disk still covers what we need."""
    leaf = _load(found.cert)
    if leaf is None or not os.path.exists(found.key):
        return False
    if sorted(_san_hosts(leaf)) != sorted(_normalise(hosts)):
        return False
    return _expires_at(leaf) - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=RENEW_WITHIN_DAYS)


def _normalise(hosts: list[str]) -> list[str]:
    """How the host strings will read back out of a SAN, so the two compare.

    ``::1`` comes back from x509 as the address object's own spelling, which is
    not always the string that went in.
    """
    normalised = []
    for host in hosts:
        try:
            normalised.append(str(ipaddress.ip_address(host)))
        except ValueError:
            normalised.append(host)
    return normalised


def _ensure_unlocked(hosts: list[str]) -> CertPaths:
    found = paths()

    ca_cert = _load(found.ca_cert)
    ca_key = _load_ca_key(found.ca_key)
    if ca_cert is None or ca_key is None:
        log.info('Generating a new certificate authority in %s', CERT_DIR)
        ca_cert, ca_key = _make_ca()
        # Key first: a CA certificate without its key is unusable, and this way
        # a crash between the two is repaired on the next start.
        _write(found.ca_key, _key_pem(ca_key), 0o600)
        _write(found.ca_cert, _pem(ca_cert), 0o644)

    if _leaf_is_current(found, hosts):
        return found

    log.info('Issuing a server certificate for: %s', ', '.join(hosts))
    leaf, leaf_key = _make_leaf(hosts, ca_cert, ca_key)
    _write(found.key, _key_pem(leaf_key), 0o600)
    # Leaf then CA: Gunicorn hands this straight to load_cert_chain(), and
    # shipping the chain saves debugging a client that has the CA but builds the
    # path differently.
    _write(found.cert, _pem(leaf) + _pem(ca_cert), 0o644)

    return found


def ensure_certificates(hosts: list[str] | None = None) -> CertPaths | None:
    """Make sure usable certificates exist, generating only what is missing.

    Idempotent: an existing CA is always reused, and the leaf is reissued only
    when it is absent, close to expiry, or no longer covers ``hosts``. Returns
    None when TLS is switched off.
    """
    if not tls_enabled():
        log.info('PYWHISPR_TLS is off; not generating certificates')
        return None

    if hosts is None:
        hosts = configured_hosts()

    os.makedirs(CERT_DIR, mode=0o700, exist_ok=True)

    # Two containers can share the volume, and generation must not race. The
    # lock lives on a sibling file rather than on the certificates themselves,
    # because os.replace() swaps the inode out from under any lock held on them.
    lock_path = os.path.join(CERT_DIR, '.certs.lock')
    with open(lock_path, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _ensure_unlocked(hosts)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _describe() -> int:
    """Print what is on disk. For poking around inside the container."""
    info = certificate_info()
    if info is None:
        print(f'No certificates in {CERT_DIR}')
        print(f'TLS is {"on" if tls_enabled() else "off"}')
        return 1

    print(f'Certificate directory: {CERT_DIR}')
    print(f'Valid for:             {", ".join(info.hosts)}')
    print(f'Expires:               {info.not_after:%Y-%m-%d %H:%M UTC}')
    print(f'CA expires:            {info.ca_not_after:%Y-%m-%d %H:%M UTC}')
    print(f'CA SHA-256:            {info.ca_fingerprint}')
    if not os.environ.get('PYWHISPR_TLS_HOSTS'):
        print('\nPYWHISPR_TLS_HOSTS is unset, so this certificate only covers '
              'localhost and the\ncontainer hostname. Set it to the name or '
              'address your phone uses.')
    return 0


if __name__ == '__main__':
    raise SystemExit(_describe())
