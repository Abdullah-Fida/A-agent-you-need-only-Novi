import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')
import logging
logging.basicConfig(level=logging.INFO)
from core.config import load_config
from telethon import TelegramClient
from telethon.sessions import StringSession

async def list_groups():
    config = load_config()
    session = StringSession(config.stealth_session_string or config.telegram_session_string)
    client = TelegramClient(session, config.telegram_api_id, config.telegram_api_hash)
    await client.connect()
    
    dialogs = await client.get_dialogs()
    print("Burner account is in the following chats:")
    for d in dialogs:
        print(f"- {d.name} (ID: {d.id}, IsGroup: {d.is_group}, IsChannel: {d.is_channel})")
        
    await client.disconnect()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(list_groups())
