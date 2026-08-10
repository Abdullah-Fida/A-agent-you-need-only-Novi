import os
import sys

# Optional: Add directory to path to ensure smooth imports if needed
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

print("==============================================")
print("     TELEGRAM STRING SESSION GENERATOR        ")
print("==============================================\n")
print("This script will log into your Telegram account and generate")
print("a secure 'Session String'. This string acts like a permanent")
print("login token so the bot never has to ask for a code again.\n")

if len(sys.argv) == 4:
    api_id_input = sys.argv[1]
    api_hash = sys.argv[2]
    phone = sys.argv[3]
else:
    api_id_input = input("Enter your Telegram API_ID: ").strip()
    api_hash = input("Enter your Telegram API_HASH: ").strip()
    phone = None

try:
    api_id = int(api_id_input)
except ValueError:
    print("API_ID must be a number! Exiting.")
    sys.exit(1)

print("\nConnecting to Telegram...")

# Use StringSession() without an argument to create a new session
client = TelegramClient(StringSession(), api_id, api_hash)
client.start(phone=lambda: phone if phone else input("Enter your phone number: "))

print("\n==============================================")
print("LOGIN SUCCESSFUL!")
print("==============================================")
print("\nCopy the exact text below (the long random string) and paste it into your .env file")
print("as STEALTH_SESSION_STRING (for the stealth marketer) or TELEGRAM_SESSION_STRING (for the main bot).")
print("\nHere is your Session String:\n")

# client.session.save() exports the session state as a string
session_string = client.session.save()
print(session_string)

print("\n==============================================")
print("Keep this string perfectly secure. Treat it like a password.")
client.disconnect()
