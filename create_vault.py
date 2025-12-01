import os
import json
from getpass import getpass
from cryptography.fernet import Fernet

# Files live next to this script, OS-agnostic
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VAULT_FILE = os.path.join(BASE_DIR, "vault.bin")
KEY_FILE = os.path.join(BASE_DIR, "vault.key")


def main():
    print("=== BoxCast / Discord Vault Creator ===")

    if os.path.exists(VAULT_FILE):
        ans = input("vault.bin already exists. Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborting, existing vault kept.")
            return

    # Ask for secrets
    client_id = input("Enter BoxCast CLIENT_ID: ").strip()
    client_secret = getpass("Enter BoxCast CLIENT_SECRET (hidden): ").strip()
    discord_webhook = input("Enter Discord Webhook URL: ").strip()

    secrets = {
        "client_id": client_id,
        "client_secret": client_secret,
        "discord_webhook": discord_webhook,
    }

    # Generate a new key if none exists yet
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read().strip()
        print(f"Using existing key file: {KEY_FILE}")
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
        print(f"New key generated and saved to: {KEY_FILE}")
        print("TIP: On Linux/RasPi, run:  chmod 600 vault.key")

    fernet = Fernet(key)
    data = json.dumps(secrets).encode("utf-8")
    token = fernet.encrypt(data)

    with open(VAULT_FILE, "wb") as f:
        f.write(token)

    print(f"Vault written to: {VAULT_FILE}")
    print("Done.")


if __name__ == "__main__":
    main()
