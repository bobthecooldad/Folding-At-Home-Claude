"""Crypto used by the Folding@home account / node protocol.

Mirrors the official web client (``src/crypto.js``, ``src/account.js``) and
the client (``src/fah/client/Account.cpp``):

* account login:  salt = SHA256(lower(email)),
                  L = PBKDF2-SHA256(passphrase, salt, 100000, 32 bytes),
                  password hash = base64(SHA256(L))
* account secret: the account RSA private key (PKCS#8) wrapped with
                  AES-256-CBC(key=L, iv=salt[0:16])
* signatures:     RSASSA-PKCS1-v1_5 / SHA-256 over compact JSON
* machine keys:   RSA-OAEP (SHA-256) encrypted AES-256 session keys
* messages:       AES-256-CBC, PKCS#7 padding, random 16 byte IV
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from typing import Any

from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 100_000
MAX_DECOMPRESSED = 64 * 1024 * 1024  # refuse "zip bombs" from the network
IV_SIZE = 16


class FahCryptoError(Exception):
    """Decryption / verification failed."""


# --------------------------------------------------------------- encoding
def js_bytes(text: str) -> bytes:
    """Bytes exactly as the web client's ``util.str2buf`` makes them.

    ``Uint8Array.from(str, c => c.codePointAt(0))`` keeps only the low 8 bits
    of every code point (identical to UTF-8 for plain ASCII).
    """
    return bytes(ord(c) & 0xFF for c in text)


def b64encode(data: bytes) -> str:
    """Standard base64 with padding (``btoa``)."""
    return base64.b64encode(data).decode()


def urlb64encode(data: bytes) -> str:
    """URL-safe base64 without padding (web client ``urlbase64_encode``)."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64decode(text: str) -> bytes:
    """Decode standard or URL-safe base64, with or without padding."""
    text = text.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text)


def compact_json(obj: Any) -> str:
    """``JSON.stringify`` equivalent (no spaces, unicode kept)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def sha256(data: bytes) -> bytes:
    """SHA-256 digest."""
    return hashlib.sha256(data).digest()


# ----------------------------------------------------------- account login
def login_salt(email: str) -> bytes:
    """Salt used for login and the account secret."""
    return sha256(js_bytes(email.lower()))


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """PBKDF2-SHA256 -> 32 byte AES key ``L``."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS
    )
    return kdf.derive(js_bytes(passphrase))


def password_hash(key: bytes) -> str:
    """Password sent to the API: base64(SHA256(L))."""
    return b64encode(sha256(key))


def unwrap_secret(wrapped_b64: str, key: bytes, salt: bytes) -> rsa.RSAPrivateKey:
    """Decrypt the account secret into the account RSA private key."""
    try:
        der = aes_decrypt(key, salt[:IV_SIZE], b64decode(wrapped_b64))
        private = serialization.load_der_private_key(der, password=None)
    except Exception as err:  # noqa: BLE001
        raise FahCryptoError("Could not unlock the account key") from err
    if not isinstance(private, rsa.RSAPrivateKey):
        raise FahCryptoError("Account key is not an RSA key")
    return private


def wrap_secret(private: rsa.RSAPrivateKey, key: bytes, salt: bytes) -> str:
    """Inverse of :func:`unwrap_secret` (used by tests / the mock server)."""
    der = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return b64encode(aes_encrypt(key, salt[:IV_SIZE], der))


# --------------------------------------------------------------- RSA keys
def private_to_b64(private: rsa.RSAPrivateKey) -> str:
    """PKCS#8 DER, base64 (what the web client keeps in localStorage)."""
    return b64encode(
        private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def private_from_b64(data: str) -> rsa.RSAPrivateKey:
    """Load the stored account key."""
    key = serialization.load_der_private_key(b64decode(data), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise FahCryptoError("Not an RSA key")
    return key


def public_from_b64(data: str) -> rsa.RSAPublicKey:
    """Load an SPKI public key (account or machine)."""
    key = serialization.load_der_public_key(b64decode(data))
    if not isinstance(key, rsa.RSAPublicKey):
        raise FahCryptoError("Not an RSA key")
    return key


def public_to_b64(public: rsa.RSAPublicKey) -> str:
    """SPKI DER, base64."""
    return b64encode(
        public.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def pubkey_id(public: rsa.RSAPublicKey) -> str:
    """F@h id of a key: urlbase64(SHA256(modulus))."""
    n = public.public_numbers().n
    return urlb64encode(sha256(n.to_bytes((n.bit_length() + 7) // 8, "big")))


def sign(private: rsa.RSAPrivateKey, text: str) -> str:
    """RSASSA-PKCS1-v1_5 / SHA-256 signature, URL-safe base64."""
    return urlb64encode(private.sign(js_bytes_utf8(text), asym_padding.PKCS1v15(), hashes.SHA256()))


def verify(public: rsa.RSAPublicKey, signature_b64: str, text: str) -> bool:
    """Verify a signature."""
    try:
        public.verify(
            b64decode(signature_b64), js_bytes_utf8(text), asym_padding.PKCS1v15(), hashes.SHA256()
        )
    except Exception:  # noqa: BLE001
        return False
    return True


def js_bytes_utf8(text: str) -> bytes:
    """Bytes of a JSON string for signing.

    The web client signs ``str2buf(JSON.stringify(...))``; for the ASCII
    payloads used by the protocol this equals UTF-8.
    """
    return js_bytes(text) if text.isascii() else text.encode()


def rsa_decrypt(private: rsa.RSAPrivateKey, data: bytes) -> bytes:
    """RSA-OAEP / SHA-256 decrypt."""
    try:
        return private.decrypt(
            data,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as err:  # noqa: BLE001
        raise FahCryptoError("RSA decrypt failed") from err


def rsa_encrypt(public: rsa.RSAPublicKey, data: bytes) -> bytes:
    """RSA-OAEP / SHA-256 encrypt (tests / mock machine)."""
    return public.encrypt(
        data,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


# -------------------------------------------------------------------- AES
def aes_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC with PKCS#7 padding."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC with PKCS#7 padding."""
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = dec.update(data) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except Exception as err:  # noqa: BLE001
        raise FahCryptoError("AES decrypt failed") from err


def _gunzip_limited(data: bytes, limit: int) -> bytes:
    """gunzip, refusing output larger than ``limit`` bytes."""
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = dec.decompress(data, limit + 1)
    if len(out) > limit or dec.unconsumed_tail:
        raise FahCryptoError("Decompressed message too large")
    return out


def random_bytes(size: int) -> bytes:
    """Cryptographically secure random bytes."""
    return os.urandom(size)


def encrypt_message(key: bytes, obj: Any) -> tuple[str, str]:
    """Encrypt a JSON message -> (iv, payload), both URL-safe base64."""
    iv = random_bytes(IV_SIZE)
    payload = aes_encrypt(key, iv, compact_json(obj).encode())
    return urlb64encode(iv), urlb64encode(payload)


def decrypt_message(key: bytes, iv_b64: str, payload_b64: str, compression: str | None) -> Any:
    """Decrypt (and gunzip) a JSON message."""
    iv = b64decode(iv_b64)
    if len(iv) != IV_SIZE:
        raise FahCryptoError(f"Invalid IV length {len(iv)}")
    data = aes_decrypt(key, iv, b64decode(payload_b64))
    if compression:
        if compression != "gzip":
            raise FahCryptoError(f"Unsupported compression {compression}")
        data = _gunzip_limited(data, MAX_DECOMPRESSED)
    return json.loads(data)
