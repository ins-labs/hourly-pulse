# content/

`standing_text.enc` holds the owner's approved fixed pulse text, encrypted
(Fernet). The plaintext never enters this repository.

Produce it once, locally, after generating a key:

```sh
python3 -c "from src.crypto_util import generate_key; print(generate_key())"   # store the printed key ONLY as the PULSE_TEXT_KEY secret
python3 src/crypto_util.py encrypt-file PLAIN.txt < KEY_FILE > content/standing_text.enc
```

`PLAIN.txt` is the owner-approved fixed text; delete it and `KEY_FILE` locally
afterwards. Rotate by repeating with a new key and updating the secret.
