"""
Optional built-in TLS, so S3/WebDAV clients that insist on HTTPS can connect
directly (without a fronting reverse proxy). In production you'd normally
terminate TLS at your own proxy instead.

If the cert/key files don't exist, a self-signed pair is generated (via the
`cryptography` package if present, else the `openssl` CLI). Self-signed certs
are untrusted: enable "accept/trust untrusted certificate" in your client.
"""

import datetime
import os
import ssl
import subprocess


def _gen_cryptography(cert_path, key_path):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "funambridge")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                           False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _gen_openssl(cert_path, key_path):
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key_path, "-out", cert_path, "-days", "3650",
         "-subj", "/CN=funambridge"],
        check=True, capture_output=True)


def ensure_cert(cert_path, key_path):
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    d = os.path.dirname(cert_path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        _gen_cryptography(cert_path, key_path)
        return
    except ImportError:
        pass
    try:
        _gen_openssl(cert_path, key_path)
    except (OSError, subprocess.CalledProcessError) as e:
        raise RuntimeError(
            "no se pudo generar un certificado autofirmado: instala "
            "'cryptography' (pip install cryptography) o 'openssl', o aporta "
            "tu propio cert/clave en config. Detalle: %s" % e)


def server_context(cert_path, key_path):
    ensure_cert(cert_path, key_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx
