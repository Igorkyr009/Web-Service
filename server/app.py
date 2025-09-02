import os, asyncio, json, time, secrets
from pathlib import Path
from typing import List, Tuple, Dict, Any

from aiohttp import web
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.client.default import DefaultBotProperties

# -------------------- ENV --------------------
load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID         = os.getenv("ADMIN_ID", "").strip()
ADMIN_BOT_TOKEN  = os.getenv("ADMIN_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID    = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_SECRET     = os.getenv("ADMIN_SECRET", "").strip()
PORT             = int(os.getenv("PORT", "8000"))

# где лежит веб
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR  = BASE_DIR / "web"

# база и загрузки (Render)
DB_PATH    = os.getenv("DB_PATH", "/tmp/shop.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/var/data/uploads")  # постоянный диск Render

# если /var/data недоступен — падаем в /tmp
try:
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    UPLOAD_DIR = "/tmp/uploads"
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

print("DB_PATH        =", DB_PATH)
print("UPLOAD_DIR     =", UPLOAD_DIR)

# -------------------- DB --------------------
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  image_url TEXT,
  description TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  category TEXT DEFAULT 'devices',
  stock_status TEXT DEFAULT 'in_stock'
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id INTEGER NOT NULL,
  tg_username TEXT,
  tg_name TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'UAH',
  city TEXT,
  branch TEXT,
  receiver TEXT,
  phone TEXT,
  status TEXT DEFAULT 'new',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  product_sku TEXT NOT NULL,
  product_title TEXT NOT NULL,
  price INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as d:
        await d.executescript(CREATE_SQL)
        await d.commit()

asy







