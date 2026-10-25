"""Interop: our crypto vs. the official web client's WebCrypto code paths.

``tests/webcrypto/vectors.json`` was produced by ``vectors.mjs`` (Node.js
WebCrypto running the same calls as the F@h web client). Regenerate with
``node tests/webcrypto/vectors.mjs > tests/webcrypto/vectors.json``.
"""

import json
from pathlib import Path

from custom_components.foldingathome import fahcrypto as c

V = json.loads((Path(__file__).parent / "webcrypto" / "vectors.json").read_text())


def test_login_hash_and_unwrap():
    salt = c.login_salt(V["email"])
    key = c.derive_key(V["passphrase"], salt)
    assert c.password_hash(key) == V["password_hash"]
    private = c.unwrap_secret(V["secret"], key, salt)
    assert c.public_to_b64(private.public_key()) == V["pubkey"]


def test_wrong_passphrase_fails():
    salt = c.login_salt(V["email"])
    key = c.derive_key("wrong", salt)
    assert c.password_hash(key) != V["password_hash"]
    try:
        c.unwrap_secret(V["secret"], key, salt)
    except c.FahCryptoError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unwrap should fail")


def _private():
    salt = c.login_salt(V["email"])
    return c.unwrap_secret(V["secret"], c.derive_key(V["passphrase"], salt), salt)


def test_signature_and_id():
    private = _private()
    pub = c.public_from_b64(V["pubkey"])
    assert c.pubkey_id(pub) == V["id"]
    text = c.compact_json(V["payload"])
    # PKCS1 v1.5 is deterministic: identical signature to WebCrypto
    assert c.sign(private, text) == V["signature"]
    assert c.verify(pub, V["signature"], text)
    assert not c.verify(pub, V["signature"], text + " ")


def test_machine_key_and_message():
    private = _private()
    key = c.rsa_decrypt(private, c.b64decode(V["machine_key_encrypted"]))
    assert key == c.b64decode(V["machine_key"])
    msg = c.decrypt_message(key, V["iv"], V["message_payload"], None)
    assert c.compact_json(msg) == V["message"]
    iv, payload = c.encrypt_message(key, msg)
    assert c.decrypt_message(key, iv, payload, None) == msg


def test_gzip_message():
    import gzip

    key = c.random_bytes(32)
    iv = c.random_bytes(16)
    data = gzip.compress(c.compact_json({"big": "x" * 20000}).encode())
    payload = c.urlb64encode(c.aes_encrypt(key, iv, data))
    assert c.decrypt_message(key, c.urlb64encode(iv), payload, "gzip") == {"big": "x" * 20000}
