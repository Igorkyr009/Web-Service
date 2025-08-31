import os
import asyncio
import json
import time
from pathlib import Path
from typing import List, Tuple, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ===================== 1) ОКРУЖЕНИЕ =====================
BOT_TOKEN   = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
WEBAPP_URL  = os.getenv("WEBAPP_URL", "").strip()  # напр. https://web-service-1-4kcb.onrender.com/index.html
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
DB_PATH     = os.getenv("DB_PATH", "./data/shop.db")

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в переменных окружения")

# создаём папку под БД
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# ===================== 2) BOT и DP (до хендлеров!) =====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

# ===================== 3) МИНИ-КАТАЛОГ (если надо посчитать total) =====================
# Если цены уже считает твоя витрина — можно не использовать.
CATALOG = {
    # sku : {title, price, currency}
    "xros_4nano": {"title": "XROS 4 NANO", "price": 1399, "currency": "UAH"},
    "coffee_1kg": {"title": "Кава в зернах 1 кг", "price": 1299, "currency": "UAH"},
}

# ===================== 4) SQL =====================
CREATE_SQL = """
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def save_order(
    user,
    items: List[Tuple[str, str, int, int]],
    total: int,
    currency: str,
    city: str, branch: str, receiver: str, phone: str
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user.id,
                f"@{user.username}" if user.username else None,
                f"{user.first_name or ''} {user.last_name or ''}".strip(),
                total, currency,
                city, branch, receiver, phone,
                "new", int(time.time())
            )
        )
        await db.commit()
        order_id = cur.lastrowid
        for sku, title, price, qty in items:
            await db.execute(
                "INSERT INTO order_items (order_id, product_sku, product_title, price, qty) VALUES (?,?,?,?,?)",
                (order_id, sku, title, price, qty)
            )
        await db.commit()
    return int(order_id)

# ===================== 5) СЕРВИСНЫЕ =====================
async def setup_menu_button():
    if not WEBAPP_URL:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🛍 Вітрина", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as e:
        print("set_chat_menu_button error:", e)

async def notify_admins(text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception as e:
            print(f"send admin error chat_id={aid}:", e)

# ===================== 6) ХЕНДЛЕРЫ =====================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    if WEBAPP_URL:
        kb = {
            "inline_keyboard": [[
                {"text": "🛒 Каталог",     "web_app": {"url": f"{WEBAPP_URL}#catalog"}},
                {"text": "🧾 Оформлення", "web_app": {"url": f"{WEBAPP_URL}#checkout"}},
            ]]
        }
        await m.answer(
            "Привіт! Відкрий вітрину або переходь до оформлення:",
            reply_markup=kb
        )
    else:
        await m.answer("WEBAPP_URL не налаштовано. Додай посилання у змінні оточення.")

@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    """
    Ждём JSON из WebApp вида:
    {
      "type":"checkout",
      "items":[{"sku":"xros_4nano","qty":1}, ...],
      "city":"Київ", "branch":"Відділення №...", "receiver":"Ім'я Прізвище",
      "phone":"+380...", "username":"@нік" (опц.)
    }
    """
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("⚠️ Не вдалося прочитати дані замовлення.")

    if data.get("type") != "checkout":
        return await m.answer("Дані з WebApp отримано, але тип не розпізнано.")

    # Сбор корзины
    items_in: List[Tuple[str, int]] = []
    for it in data.get("items", []):
        try:
            sku = str(it.get("sku"))
            qty = int(it.get("qty", 1))
        except Exception:
            continue
        if qty > 0:
            items_in.append((sku, qty))

    if not items_in:
        return await m.answer("Корзина порожня.")

    # Собираем позиции из CATALOG (если хочешь — подтягивай из своей БД/АПІ)
    items: List[Tuple[str, str, int, int]] = []  # sku, title, price, qty
    total = 0
    currency = "UAH"
    for sku, qty in items_in:
        p = CATALOG.get(sku)
        if not p:
            # неизвестный sku — добавим как 0 грн
            items.append((sku, sku, 0, qty))
            continue
        items.append((sku, p["title"], p["price"], qty))
        total += p["price"] * qty
        currency = p.get("currency", "UAH")

    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()
    username = (data.get("username") or f"@{m.from_user.username}" if m.from_user.username else "").strip()

    # Сохраняем
    order_id = await save_order(m.from_user, items, total, currency, city, branch, receiver, phone)

    # Ответ покупателю
    await m.answer(f"✅ Замовлення #{order_id} створено!\n"
                   f"Сума: <b>{total} {currency}</b>\n"
                   f"Ми напишемо вам у Telegram для підтвердження.\n"
                   f"{('Ваш нік: ' + username) if username else ''}")

    # Уведомление админам
    lines = [f"🆕 Нове замовлення #{order_id}",
             f"Покупець: {m.from_user.first_name} {m.from_user.last_name or ''}".strip(),
             f"Username: {username or '—'}",
             "",
             "Товари:"]
    for _, title, price, qty in items:
        lines.append(f" • {title} × {qty} = {price*qty} {currency}")
    lines += [f"— — —",
              f"Разом: {total} {currency}",
              "",
              f"Місто: {city}",
              f"Відділення: {branch}",
              f"Отримувач: {receiver}",
              f"Телефон: {phone}",
              f"Telegram ID: {m.from_user.id}"]
    await notify_admins("\n".join(lines))

# ===================== 7) ЗАПУСК =====================
async def main():
    await init_db()
    await setup_menu_button()
    print("Bot started. Press Ctrl+C to stop.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


