#!/usr/bin/env python3

import datetime
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

CERT_FILE = Path("server.crt")
KEY_FILE = Path("server.key")
DAYS_VALID = 365

def generate_self_signed_cert() -> None:
    print("Generating a new 4096-bit RSA key...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u"doot-c2"),
    ])

    print(f"Creating a self-signed certificate valid for {DAYS_VALID} days...")
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc)
    ).not_valid_after(
        # Our certificate will be valid for 1 year
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=DAYS_VALID)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
        critical=False,
    ).sign(private_key, hashes.SHA256())

    print(f"Writing private key to {KEY_FILE.name}...")
    KEY_FILE.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    print(f"Writing certificate to {CERT_FILE.name}...")
    CERT_FILE.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )

    print(f"\nSuccess! The following files have been created:")
    print(f"  - {CERT_FILE}")
    print(f"  - {KEY_FILE}")
    print("\nYou can now run the D.O.O.T host to use these credentials.")

if __name__ == "__main__":
    generate_self_signed_cert()
