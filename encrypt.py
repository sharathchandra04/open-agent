"""CLI helper for the Telegram bot's optional encryption layer.

  python encrypt.py "prompt text"          -> prints enc:... to paste into Telegram
  python encrypt.py --decrypt "enc:..."    -> prints the plaintext
  python encrypt.py --secret PASS "text"   -> use PASS instead of env TELEGRAM_SECRET

Same format as telegram_bot.py (and encrypt.html):
  enc:v1:<base64(salt 16 bytes | iv 12 bytes | AES-GCM ciphertext+tag)>
  key = PBKDF2-HMAC-SHA256(passphrase, salt, 100_000 iterations, 32 bytes)

Needs the `cryptography` package: pip install -r requirements.txt
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_bot import decrypt_text, encrypt_text  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Encrypt a prompt for the Telegram bot, or decrypt a reply.")
    parser.add_argument("text", help="plaintext prompt, or an enc:... string with --decrypt")
    parser.add_argument("--decrypt", action="store_true",
                        help="decrypt the given enc:... string instead of encrypting")
    parser.add_argument("--secret", default=None,
                        help="passphrase (default: env TELEGRAM_SECRET)")
    args = parser.parse_args(argv)
    secret = args.secret or os.environ.get("TELEGRAM_SECRET")
    if not secret:
        print("encrypt.py: no secret: set TELEGRAM_SECRET or pass --secret",
              file=sys.stderr)
        return 1
    try:
        if args.decrypt:
            print(decrypt_text(args.text, secret))
        else:
            print(encrypt_text(args.text, secret))
    except Exception as exc:  # noqa: BLE001 - report and exit, don't print a traceback
        print(f"encrypt.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
