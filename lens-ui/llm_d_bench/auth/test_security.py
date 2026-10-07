import pytest

from llm_d_bench.auth.security import (
    SecretCipher,
    generate_session_token,
    hash_password,
    hash_session_token,
    sign_internal,
    verify_internal,
    verify_password,
)


def test_password_hash_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_verify_password_is_false_for_malformed_hash():
    assert not verify_password("anything", "not-a-hash")


def test_session_tokens_are_unique_and_hashed_stably():
    first = generate_session_token()
    second = generate_session_token()
    assert first != second
    assert hash_session_token(first) == hash_session_token(first)
    assert len(hash_session_token(first)) == 64


def test_internal_signature_roundtrip():
    signature = sign_internal("secret", timestamp=1000, method="get", path="/api/x", principal_id="u1")
    assert verify_internal(
        "secret",
        signature=signature,
        timestamp=1000,
        method="GET",
        path="/api/x",
        principal_id="u1",
        now=1030,
    )


def test_internal_signature_rejects_tampering_and_staleness():
    signature = sign_internal("secret", timestamp=1000, method="POST", path="/api/x", principal_id="u1")
    assert not verify_internal(
        "secret", signature=signature, timestamp=1000, method="POST", path="/api/y", principal_id="u1", now=1000
    )
    assert not verify_internal(
        "secret", signature=signature, timestamp=1000, method="POST", path="/api/x", principal_id="u2", now=1000
    )
    assert not verify_internal(
        "secret", signature=signature, timestamp=1000, method="POST", path="/api/x", principal_id="u1", now=2000
    )
    assert not verify_internal(
        "other-secret", signature=signature, timestamp=1000, method="POST", path="/api/x", principal_id="u1", now=1000
    )


def test_secret_cipher_roundtrip_and_key_rotation():
    plaintext = "ldap-bind-credential"
    token = SecretCipher("new-key").encrypt(plaintext)
    assert token != plaintext
    assert SecretCipher("new-key").decrypt(token) == plaintext

    # A cipher with the old key as fallback can still decrypt after rotation.
    rotated = SecretCipher("rotated-key", old=["new-key"])
    assert rotated.decrypt(token) == plaintext
    assert SecretCipher("rotated-key").decrypt(rotated.encrypt("v")) == "v"


def test_secret_cipher_rejects_unknown_key():
    token = SecretCipher("key-a").encrypt("value")
    with pytest.raises(ValueError, match="could not be decrypted"):
        SecretCipher("key-b").decrypt(token)


def test_secret_cipher_requires_primary_key():
    with pytest.raises(ValueError, match="primary secret key"):
        SecretCipher("")
