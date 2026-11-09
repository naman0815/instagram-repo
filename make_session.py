#!/usr/bin/env python3
"""One-time local helper: log in to Telegram and print a session string.

Reads TG_API_ID / TG_API_HASH from the environment or a local .env file.
"""
import os
from pathlib import Path


def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main():
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    load_env()
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        client.start()  # interactive: phone number, then login code
        session = client.session.save()

    print("\nWARNING: This string is a login to your Telegram account.")
    print("Store it only as a GitHub secret (TG_SESSION). Never commit or share it.\n")
    print(session)


if __name__ == "__main__":
    main()
