"""Fernet-based encryption for user secrets (golden_key)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretCipher:
    """Wraps a Fernet key for encrypting/decrypting short strings.

    Fernet uses AES-128-CBC + HMAC-SHA256. Keys must be 32 url-safe base64 bytes
    (i.e. 44 chars). Generate one with
    ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
    """

    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as e:
            raise SystemExit(
                "Invalid ENCRYPTION_KEY: must be a 32-byte url-safe base64 string "
                "(Fernet.generate_key() output). "
                f"Error: {e}"
            )

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as e:
            raise ValueError(
                "Could not decrypt secret — wrong ENCRYPTION_KEY or corrupted data."
            ) from e
