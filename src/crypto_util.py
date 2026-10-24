"""Fixed-text encryption helper (Fernet / AES-128-CBC + HMAC, via `cryptography`).

The repository carries only ciphertext. The key lives in a secret; the workflow
decrypts at runtime. `encrypt-file` is run once locally by the owner to produce
content/standing_text.enc; the plaintext never enters the repository.
"""
import base64
import sys

from cryptography.fernet import Fernet


def generate_key():
    return Fernet.generate_key().decode()


def encrypt_text(key, plaintext):
    if not plaintext.strip():
        raise ValueError("empty plaintext")
    return Fernet(key.encode()).encrypt(plaintext.encode()).decode()


def decrypt_text(key, ciphertext):
    if not key or not ciphertext.strip():
        raise ValueError("key and ciphertext are required")
    try:
        return Fernet(key.encode()).decrypt(ciphertext.strip().encode()).decode()
    except Exception:
        # Deliberately generic: never leak key or ciphertext material in errors.
        raise ValueError("fixed text decryption failed")


def main(argv):
    if len(argv) == 2 and argv[0] == "encrypt-file":
        with open(argv[1], "rb") as handle:
            plaintext = handle.read()
        sys.stdout.write(encrypt_text(sys.stdin.read().strip(), plaintext.decode()))
        sys.stdout.write("\n")
        return 0
    sys.stderr.write("usage: python3 src/crypto_util.py encrypt-file PLAIN.txt < KEY\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
