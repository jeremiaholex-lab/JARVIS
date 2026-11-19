"""Run JARVIS as a private Telegram bot (full agent brain, read-only tools).

Usage:  python connect_telegram.py
Needs:   TELEGRAM_BOT_TOKEN in .env (create the bot with @BotFather, /newbot).
Optional: JARVIS_TELEGRAM_ALLOWED_CHATS=<your chat id> to keep it 1:1.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.channels.telegram import TelegramBot

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    TelegramBot().run()
