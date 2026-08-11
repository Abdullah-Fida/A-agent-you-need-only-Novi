"""
Two-step Telegram session generator.

The normal login prompts for the OTP interactively, which is awkward when the
person running the command and the person holding the phone are not the same.
This splits it into two commands and keeps the half-finished session on disk
between them.

    python tools/stealth_login.py send stealth      # sends the code
    python tools/stealth_login.py code 12345        # completes login
    python tools/stealth_login.py code 12345 --password YOURPASS   # if 2FA on

Account choice:
    stealth  -> uses STEALTH_PHONE   (the burner)
    main     -> uses TELEGRAM_PHONE  (your personal account)

The finished session string is printed AND written into .env automatically.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError,
)

from utils.telegram_utils import device_kwargs


def client_kwargs(account: str, phone: str) -> dict:
    """
    Device parameters the finished session must be created with.

    Telegram binds the auth key to the device that created it. The stealth
    marketer connects as a specific Android handset, so the session has to be
    born as that same handset — otherwise the running bot looks like a
    different device reusing a stolen key, and Telegram revokes it. That is
    the "connects once, then never reconnects" failure.

    The main account's broadcaster passes no device parameters, so its login
    must not either.
    """
    return device_kwargs(phone) if account == "stealth" else {}

STATE = os.path.join(tempfile.gettempdir(), "novi_login_state.json")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")


def load_env():
    load_dotenv(ENV_PATH)
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        sys.exit("TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env")
    return int(api_id), api_hash


def write_env(var: str, value: str):
    """Replaces (or appends) a variable in .env without touching the rest."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{var}="):
            lines[i] = f"{var}={value}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{var}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    print(f"\n.env updated: {var} written to {ENV_PATH}")


def cmd_send(account: str):
    api_id, api_hash = load_env()
    var = "STEALTH_SESSION_STRING" if account == "stealth" else "TELEGRAM_SESSION_STRING"
    phone_var = "STEALTH_PHONE" if account == "stealth" else "TELEGRAM_PHONE"
    phone = os.getenv(phone_var, "").strip()
    if not phone:
        sys.exit(f"{phone_var} is not set in .env")

    dev = client_kwargs(account, phone)
    if dev:
        print(f"Creating session as: {dev['device_model']} / {dev['system_version']} "
              f"(app {dev['app_version']})")
        print("The bot connects as this same device — they must match.\n")

    client = TelegramClient(StringSession(), api_id, api_hash, **dev)
    client.connect()

    if client.is_user_authorized():
        s = client.session.save()
        client.disconnect()
        print("Already authorized. Session string:\n")
        print(s)
        write_env(var, s)
        return

    try:
        sent = client.send_code_request(phone)
    except FloodWaitError as e:
        client.disconnect()
        sys.exit(f"Telegram rate-limited this number. Wait {e.seconds}s and retry.")

    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump({
            "session": client.session.save(),   # partial session, keeps the auth key
            "phone": phone,
            "hash": sent.phone_code_hash,
            "var": var,
            "account": account,
        }, fh)

    client.disconnect()
    print("=" * 60)
    print(f"  Code sent to {phone}  ({account} account)")
    print("=" * 60)
    print("\nOpen Telegram on that number and read the login code.")
    print("Then run:\n")
    print(f"    python tools/stealth_login.py code 12345")
    print("\n(replace 12345 with the real code; add --password YOURPASS if 2FA is on)")


def cmd_code(code: str, password: str = ""):
    if not os.path.exists(STATE):
        sys.exit("No pending login. Run:  python tools/stealth_login.py send stealth")

    with open(STATE, encoding="utf-8") as fh:
        st = json.load(fh)

    api_id, api_hash = load_env()
    # Same device identity as the 'send' step, or the sign-in completes as a
    # different device than the one the code was requested from.
    client = TelegramClient(StringSession(st["session"]), api_id, api_hash,
                            **client_kwargs(st.get("account", "stealth"), st["phone"]))
    client.connect()

    try:
        client.sign_in(phone=st["phone"], code=code.strip(),
                       phone_code_hash=st["hash"])
    except SessionPasswordNeededError:
        if not password:
            client.disconnect()
            sys.exit("This account has 2FA enabled. Re-run with:\n"
                     f"    python tools/stealth_login.py code {code} --password YOURPASS")
        client.sign_in(password=password)
    except PhoneCodeInvalidError:
        client.disconnect()
        sys.exit("That code is not correct. Check it and run the code step again.")
    except PhoneCodeExpiredError:
        client.disconnect()
        os.remove(STATE)
        sys.exit("That code expired. Start over with the 'send' step.")

    me = client.get_me()
    session_string = client.session.save()
    client.disconnect()

    try:
        os.remove(STATE)
    except OSError:
        pass

    print("=" * 60)
    print(f"  LOGGED IN as {me.first_name} ({st['phone']})")
    print("=" * 60)
    print(f"\n{st['var']}:\n")
    print(session_string)
    write_env(st["var"], session_string)
    print("\nNow paste this same value into Render -> Environment -> "
          f"{st['var']}, then save.")
    print("\nIMPORTANT: never run the bot locally while Render is running the")
    print("same session — Telegram revokes a key used from two IPs at once.")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    action = sys.argv[1].lower()
    if action == "send":
        account = sys.argv[2].lower() if len(sys.argv) > 2 else "stealth"
        if account not in ("stealth", "main"):
            sys.exit("Account must be 'stealth' or 'main'.")
        cmd_send(account)
    elif action == "code":
        if len(sys.argv) < 3:
            sys.exit("Usage: python tools/stealth_login.py code 12345")
        code = sys.argv[2]
        password = ""
        if "--password" in sys.argv:
            password = sys.argv[sys.argv.index("--password") + 1]
        cmd_code(code, password)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
