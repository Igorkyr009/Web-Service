# app.py — два бота: покупательский (shop) + админ (admin). aiogram 3.7
import os, asyncio, time, json
from typing import List, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import aiosqlite
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
PAGE_SIZE = 6

def fmt_price(p: int, cur: str) -> str:
    return f"{p} {cur}"

async def fetch_products(page: int = 0, category: str | None = None):
    off = page * PAGE_SIZE
    sql = "SELECT sku,title,price,currency,COALESCE(image_url,''),COALESCE(description,''),COALESCE(category,'') FROM products WHERE is_active=1"
    params = []
    if category:
        sql += " AND category=?"
        params.append(category)
    sql += " ORDER BY title LIMIT ? OFFSET ?"
    params.extend([PAGE_SIZE, off])
    async with open_db() as d:
        cur = await d.execute(sql, tuple(params))
        rows = await cur.fetchall()
        cur2 = await d.execute("SELECT COUNT(*) FROM products WHERE is_active=1" + (" AND category=?" if category else ""), ((category,) if category else ()))
        (total,) = await cur2.fetchone()
    return rows, total

def catalog_keyboard(rows, page, total, category=None):
    kb = InlineKeyboardBuilder()
    for sku, title, price, cur, *_ in rows:
        kb.button(text=f"{title} • {fmt_price(price, cur)}", callback_data=f"prod:view:{sku}")
    nav = []
    if page > 0:
        nav.append(("« Назад", f"cat:page:{page-1}:{category or ''}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(("Вперёд »", f"cat:page:{page+1}:{category or ''}"))
    if nav:
        kb.adjust(1)
        for text, cd in nav:
            kb.button(text=text, callback_data=cd)
    kb.adjust(1)
    kb.button(text="🧺 Корзина", callback_data="cart:open")
    return kb.as_markup()

def product_keyboard(sku):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ В корзину", callback_data=f"cart:add:{sku}")
    kb.button(text="🧺 Корзина", callback_data="cart:open")
    kb.button(text="« К каталогу", callback_data="cat:page:0:")
    kb.adjust(1)
    return kb.as_markup()

# ---------- ENV ----------
load_dotenv()
SHOP_TOKEN      = "8324679528:AAEqvd8T0-oB5GywVNT6EKxGAiCRT6RLkrs"
ADMIN_BOT_TOKEN = "8389668734:AAFeEvBK36YDhgYfc4-YsDAKUN3kSO3J_uI"
WEBAPP_URL      = "https://tg-shop-webapp.vercel.app/index.html"
DB_PATH         = os.getenv("DB_PATH", "/data/shop.db").strip()  # локально можно "shop.db"

if not SHOP_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN пуст. Вставь токен покупательского бота в .env.")
print("WEBAPP_URL =", WEBAPP_URL or "<пусто>")
print("DB_PATH    =", DB_PATH)

bot_shop  = Bot(SHOP_TOKEN,      default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp_shop   = Dispatcher()
bot_admin = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if ADMIN_BOT_TOKEN else None
dp_admin  = Dispatcher() if ADMIN_BOT_TOKEN else None

# ---------- DB ----------
CREATE_SQL = """

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  is_active INTEGER NOT NULL DEFAULT 1
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
  np_ttn TEXT,
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
# новые поля и таблица корзины
SCHEMA_ALTERS = [
    ("products", "description", "TEXT"),
    ("products", "image_url", "TEXT"),
    ("products", "category", "TEXT"),
]

CREATE_CART_SQL = """
CREATE TABLE IF NOT EXISTS cart_items (
  user_id INTEGER NOT NULL,
  sku TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, sku),
  FOREIGN KEY (sku) REFERENCES products(sku) ON DELETE CASCADE
);
"""

async def ensure_schema():
    async with open_db() as d:
        # добавить недостающие колонки в products
        for table, col, typ in SCHEMA_ALTERS:
            cur = await d.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in await cur.fetchall()]
            if col not in cols:
                await d.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # создать корзину
        await d.execute(CREATE_CART_SQL)
        await d.commit()

def open_db():
    return aiosqlite.connect(DB_PATH)  # возвращает контекст-менеджер


async def init_db():
    async with open_db() as d:
        await d.executescript(CREATE_SQL)
        await d.commit()
    await ensure_schema()


# helpers settings
async def set_setting(key:str, value:str):
    async with open_db() as d:

        await d.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await d.commit()

async def get_setting(key:str) -> Optional[str]:
    async with open_db() as d:

        cur = await d.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---------- SHOP BOT handlers ----------
from aiogram.types import WebAppInfo, MenuButtonWebApp

@dp_shop.message(Command("start"))
async def shop_start(m: Message):
    if WEBAPP_URL:
        kb = InlineKeyboardBuilder()
        kb.button(text="🛍 Открыть витрину", web_app=WebAppInfo(url=WEBAPP_URL))
        kb.adjust(1)
        await m.answer("Добро пожаловать! Нажми кнопку, чтобы открыть витрину:", reply_markup=kb.as_markup())
    else:
        await m.answer("Витрина временно недоступна. Укажи WEBAPP_URL в .env.")

@dp_shop.message(Command("webapp"))
async def shop_webapp(m: Message):
    if not WEBAPP_URL:
        return await m.answer("WEBAPP_URL пуст. Добавь ссылку в .env.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Открыть витрину (внутри Telegram)", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.button(text="🌐 Открыть в браузере", url=WEBAPP_URL)
    kb.adjust(1)
    await m.answer("Открой витрину:", reply_markup=kb.as_markup())

@dp_shop.message(Command("debug"))
async def shop_debug(m: Message):
    await m.answer(f"WEBAPP_URL сейчас: {WEBAPP_URL}\nDB_PATH: {DB_PATH}")

# Приём данных из WebApp
@dp_shop.message(F.web_app_data)
@dp_admin.message(Command("setimg"))
async def admin_setimg(m: Message, command: CommandObject):
    # /setimg <sku> <image_url>
    try:
        sku, url = command.args.split(maxsplit=1)
    except Exception:
        return await m.answer("Формат: /setimg <sku> <image_url>")
    async with open_db() as d:
        await d.execute("UPDATE products SET image_url=? WHERE sku=?", (url.strip(), sku.strip()))
        await d.commit()
    await m.answer(f"Картинка для {sku} обновлена.")

@dp_admin.message(Command("setdesc"))
async def admin_setdesc(m: Message, command: CommandObject):
    # /setdesc <sku> | <Описание>
    if "|" not in (command.args or ""):
        return await m.answer("Формат: /setdesc <sku> | <Описание>")
    sku, desc = [p.strip() for p in command.args.split("|", 1)]
    async with open_db() as d:
        await d.execute("UPDATE products SET description=? WHERE sku=?", (desc, sku))
        await d.commit()
    await m.answer(f"Описание {sku} обновлено.")

@dp_admin.message(Command("setcat"))
async def admin_setcat(m: Message, command: CommandObject):
    # /setcat <sku> | <Категория>
    if "|" not in (command.args or ""):
        return await m.answer("Формат: /setcat <sku> | <Категория>")
    sku, cat = [p.strip() for p in command.args.split("|", 1)]
    async with open_db() as d:
        await d.execute("UPDATE products SET category=? WHERE sku=?", (cat, sku))
        await d.commit()
    await m.answer(f"Категория {sku} → {cat}")
@dp_shop.message(Command("catalog"))
async def shop_catalog(m: Message):
    rows, total = await fetch_products(page=0)
    if not rows:
        return await m.answer("Каталог пуст.")
    await m.answer("Каталог:", reply_markup=catalog_keyboard(rows, 0, total))

@dp_shop.callback_query(F.data.startswith("cat:page:"))
async def cat_page(q: CallbackQuery):
    _, _, page_str, category = q.data.split(":", 3)
    page = int(page_str)
    category = category or None
    rows, total = await fetch_products(page=page, category=category)
    try:
        await q.message.edit_text("Каталог:", reply_markup=catalog_keyboard(rows, page, total, category))
    except Exception:
        await q.message.answer("Каталог:", reply_markup=catalog_keyboard(rows, page, total, category))
    await q.answer()
@dp_shop.callback_query(F.data.startswith("prod:view:"))
async def prod_view(q: CallbackQuery):
    sku = q.data.split(":", 2)[2]
    async with open_db() as d:
        cur = await d.execute("SELECT title,price,currency,COALESCE(image_url,''),COALESCE(description,'') FROM products WHERE sku=?", (sku,))
        row = await cur.fetchone()
    if not row:
        await q.answer("Товар не найден", show_alert=True); return
    title, price, cur, img, desc = row
    caption = f"<b>{title}</b>\n{fmt_price(price, cur)}\n\n{desc or 'Описание будет скоро.'}"
    try:
        if img:
            await q.message.answer_photo(photo=img, caption=caption, reply_markup=product_keyboard(sku))
        else:
            await q.message.answer(caption, reply_markup=product_keyboard(sku))
    except Exception:
        await q.message.answer(caption, reply_markup=product_keyboard(sku))
    await q.answer()
async def cart_summary(user_id: int):
    async with open_db() as d:
        cur = await d.execute("""
          SELECT c.sku, p.title, p.price, p.currency, c.qty
          FROM cart_items c
          JOIN products p ON p.sku=c.sku
          WHERE c.user_id=?
        """, (user_id,))
        items = await cur.fetchall()
    total = sum(p * q for _, _, p, _, q in items)
    currency = items[0][3] if items else "UAH"
    return items, total, currency

@dp_shop.callback_query(F.data.startswith("cart:add:"))
async def cart_add(q: CallbackQuery):
    sku = q.data.split(":", 2)[2]
    async with open_db() as d:
        # если товара нет/отключён — не добавляем
        cur = await d.execute("SELECT 1 FROM products WHERE sku=? AND is_active=1", (sku,))
        if not await cur.fetchone():
            await q.answer("Товар недоступен", show_alert=True); return
        await d.execute("INSERT INTO cart_items (user_id, sku, qty) VALUES (?,?,1) ON CONFLICT(user_id,sku) DO UPDATE SET qty=qty+1", (q.from_user.id, sku))
        await d.commit()
    items, total, curcy = await cart_summary(q.from_user.id)
    await q.answer("Добавлено в корзину")
    await q.message.answer(f"В корзине позиций: {len(items)} • Итого: {fmt_price(total, curcy)}", reply_markup=InlineKeyboardBuilder()
                           .button(text="🧺 Открыть корзину", callback_data="cart:open")
                           .as_markup())

@dp_shop.message(Command("cart"))
@dp_shop.callback_query(F.data == "cart:open")
async def cart_open(ev):
    user_id = ev.from_user.id if isinstance(ev, CallbackQuery) else ev.from_user.id
    items, total, curcy = await cart_summary(user_id)
    if not items:
        text = "Корзина пуста."
        rm = InlineKeyboardBuilder().button(text="« В каталог", callback_data="cat:page:0:").as_markup()
    else:
        lines = [f"• {t} × {q} = {p*q} {curcy}" for _, t, p, _, q in items]
        text = "🧺 <b>Корзина</b>\n" + "\n".join(lines) + f"\n\nИтого: <b>{fmt_price(total, curcy)}</b>"
        kb = InlineKeyboardBuilder()
        kb.button(text="🧾 Оформить", callback_data="cart:checkout")
        kb.button(text="🗑 Очистить", callback_data="cart:clear")
        kb.button(text="« В каталог", callback_data="cat:page:0:")
        kb.adjust(1)
        rm = kb.as_markup()

    if isinstance(ev, CallbackQuery):
        await ev.message.answer(text, reply_markup=rm)
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=rm)

@dp_shop.callback_query(F.data == "cart:clear")
async def cart_clear(q: CallbackQuery):
    async with open_db() as d:
        await d.execute("DELETE FROM cart_items WHERE user_id=?", (q.from_user.id,))
        await d.commit()
    await q.answer("Корзина очищена")
    await cart_open(q)
class Checkout(StatesGroup):
    city = State()
    branch = State()
    receiver = State()
    phone = State()

@dp_shop.callback_query(F.data == "cart:checkout")
async def cart_checkout(q: CallbackQuery, state: FSMContext):
    items, total, curcy = await cart_summary(q.from_user.id)
    if not items:
        await q.answer("Корзина пуста", show_alert=True); return
    await state.set_state(Checkout.city)
    await q.message.answer("Город (місто):")
    await q.answer()

@dp_shop.message(Checkout.city)
async def ask_branch(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip())
    await state.set_state(Checkout.branch)
    await m.answer("Отделение Новой Почты (відділення):")

@dp_shop.message(Checkout.branch)
async def ask_receiver(m: Message, state: FSMContext):
    await state.update_data(branch=m.text.strip())
    await state.set_state(Checkout.receiver)
    await m.answer("ФИО получателя:")

@dp_shop.message(Checkout.receiver)
async def ask_phone(m: Message, state: FSMContext):
    await state.update_data(receiver=m.text.strip())
    await state.set_state(Checkout.phone)
    await m.answer("Телефон (+380…):")

@dp_shop.message(Checkout.phone)
async def finish_checkout(m: Message, state: FSMContext):
    data = await state.get_data()
    phone = m.text.strip()
    city, branch, receiver = data.get("city",""), data.get("branch",""), data.get("receiver","")

    # собрать корзину
    items, total, curcy = await cart_summary(m.from_user.id)
    if not items:
        await m.answer("Корзина пуста."); return

    # сохранить заказ (как в on_webapp_data)
    async with open_db() as d:
        cur = await d.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m.from_user.id, f"@{m.from_user.username}" if m.from_user.username else None,
             f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip(),
             total, curcy, city, branch, receiver, phone, "new", int(time.time()))
        )
        await d.commit()
        order_id = cur.lastrowid
        for sku, title, price, _, qty in items:
            await d.execute("INSERT INTO order_items (order_id,product_sku,product_title,price,qty) VALUES (?,?,?,?,?)",
                            (order_id, sku, title, price, qty))
        await d.execute("DELETE FROM cart_items WHERE user_id=?", (m.from_user.id,))
        await d.commit()

    await state.clear()

    # подтверждение и уведомление админу
    await m.answer(f"✅ Заказ #{order_id} создан! Мы свяжемся по доставке НП.")
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {curcy}" for _, t, p, _, q in items])
    admin_msg = (
        f"🆕 Новый заказ #{order_id}\n"
        f"Покупатель: {m.from_user.first_name} {m.from_user.last_name or ''} "
        f"({('@'+m.from_user.username) if m.from_user.username else '—'})\n"
        f"ID: {m.from_user.id}\n"
        f"{items_txt}\nИтого: {total} {curcy}\n"
        f"Город: {city}\nОтделение: {branch}\n"
        f"Получатель: {receiver} / {phone}"
    )
    await notify_admin(admin_msg)
@dp_shop.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗂 Каталог", callback_data="cat:page:0:")
    kb.button(text="🧺 Корзина", callback_data="cart:open")
    if WEBAPP_URL:
        kb.button(text="🛍 Витрина (WebApp)", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)
    await m.answer("Добро пожаловать! Выберите действие:", reply_markup=kb.as_markup())



async def on_webapp_data(m: Message):
    # ожидаем {"type":"checkout","items":[{"sku":"...","qty":1},...], "city":..., "branch":..., "receiver":..., "phone":...}
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Не получилось прочитать данные из витрины.")
    if data.get("type") != "checkout":
        return await m.answer("Получены данные витрины, но тип неизвестен.")

    # подгрузим актуальные цены из БД
    async with open_db() as d:

        total = 0
        currency = "UAH"
        items_to_save: List[Tuple[str,str,int,int]] = []
        for it in data.get("items", []):
            sku = str(it.get("sku"))
            qty = int(it.get("qty", 1))
            cur = await d.execute("SELECT title, price, currency FROM products WHERE sku=? AND is_active=1", (sku,))
            row = await cur.fetchone()
            if not row or qty <= 0:
                continue
            title, price, currency = row
            total += price * qty
            items_to_save.append((sku, title, price, qty))

        if not items_to_save:
            return await m.answer("Корзина пуста или товары недоступны.")

        city     = (data.get("city") or "").strip()
        branch   = (data.get("branch") or "").strip()
        receiver = (data.get("receiver") or "").strip()
        phone    = (data.get("phone") or "").strip()

        # сохранить заказ
        cur = await d.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m.from_user.id,
             f"@{m.from_user.username}" if m.from_user.username else None,
             f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip(),
             total, currency, city, branch, receiver, phone, "new", int(time.time()))
        )
        await d.commit()
        order_id = cur.lastrowid
        for sku, title, price, qty in items_to_save:
            await d.execute(
                "INSERT INTO order_items (order_id, product_sku, product_title, price, qty) VALUES (?,?,?,?,?)",
                (order_id, sku, title, price, qty)
            )
        await d.commit()

    await m.answer(f"✅ Заказ #{order_id} создан! Мы свяжемся по доставке НП.")

    # уведомить админа
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _, t, p, q in items_to_save])
    msg = (f"🆕 Новый заказ #{order_id}\n"
           f"Покупатель: {m.from_user.first_name} {m.from_user.last_name or ''} "
           f"({('@'+m.from_user.username) if m.from_user.username else '—'})\n"
           f"ID: {m.from_user.id}\n"
           f"{items_txt}\nИтого: {total} {currency}\n"
           f"Город: {city}\nОтделение: {branch}\n"
           f"Получатель: {receiver} / {phone}")
    await notify_admin(msg)

async def notify_admin(text: str):
    # шлём в админ-бот, если задан токен и зарегистрирован чат, иначе — игнор
    chat_id = await get_setting("ADMIN_CHAT_ID")
    if bot_admin and chat_id:
        try:
            await bot_admin.send_message(int(chat_id), text)
            return
        except Exception as e:
            print("notify_admin через админ-бот: ошибка:", e)
    # запасной вариант — в личку себе из shop-бота (если кто-то установил)
    fallback = await get_setting("SHOP_ADMIN_CHAT_ID")
    if fallback:
        try:
            await bot_shop.send_message(int(fallback), text)
        except Exception:
            pass

# ---------- ADMIN BOT handlers ----------
HELP_TEXT = (
    "Команды админ-бота:\n"
    "/setme — назначить этот чат админским (для уведомлений)\n"
    "/orders — последние 10 заказов\n"
    "/order <id> — детали заказа\n"
    "/status <id> <new|paid|packed|shipped|done|cancelled> — сменить статус\n"
    "/ttn <id> <номер> — сохранить ТТН НП\n"
    "/products — список товаров\n"
    "/addproduct <sku> | <Название> | <цена> [| UAH] — добавить товар\n"
    "/setprice <sku> <цена> — обновить цену\n"
    "/settitle <sku> | <Новое название> — обновить название\n"
    "/toggle <sku> — включить/выключить товар"
)

if dp_admin:

    @dp_admin.message(Command("start"))
    async def admin_start(m: Message):
        await m.answer("Админ-бот. " + HELP_TEXT)

    @dp_admin.message(Command("setme"))
    async def admin_setme(m: Message):
        await set_setting("ADMIN_CHAT_ID", str(m.chat.id))
        await m.answer(f"Ок, этот чат сохранён для уведомлений: <code>{m.chat.id}</code>")

    @dp_admin.message(Command("orders"))
    async def admin_orders(m: Message):
        async with open_db() as d:

            cur = await d.execute(
                "SELECT id,total,currency,city,branch,receiver,phone,status,created_at FROM orders ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            return await m.answer("Заказов нет.")
        lines = []
        for oid, total, curr, city, branch, recv, phone, status, ts in rows:
            lines.append(f"#{oid} • {total} {curr} • {status} • {time.strftime('%d.%m %H:%M', time.localtime(ts))}\n"
                         f"{city} / {branch}\n{recv} / {phone}\n———")
        await m.answer("\n".join(lines))

    @dp_admin.message(Command("order"))
    async def admin_order(m: Message, command: CommandObject):
        if not command.args:
            return await m.answer("Формат: /order <id>")
        oid = int(command.args.strip())
        async with open_db() as d:

            cur = await d.execute("SELECT id,total,currency,city,branch,receiver,phone,status,created_at FROM orders WHERE id=?", (oid,))
            o = await cur.fetchone()
            if not o:
                return await m.answer("Не найдено.")
            cur = await d.execute("SELECT product_title, price, qty FROM order_items WHERE order_id=?", (oid,))
            items = await cur.fetchall()
        items_txt = "\n".join([f"• {t} × {q} = {p*q}" for t,p,q in items])
        await m.answer(
            f"Заказ #{o[0]} • {o[1]} {o[2]} • {o[7]} • {time.strftime('%d.%m %H:%M', time.localtime(o[8]))}\n"
            f"{items_txt}\nГород: {o[3]}\nОтделение: {o[4]}\nПолучатель: {o[5]} / {o[6]}"
        )

    @dp_admin.message(Command("status"))
    async def admin_status(m: Message, command: CommandObject):
        try:
            oid, new_status = command.args.split(maxsplit=1)
            oid = int(oid)
        except Exception:
            return await m.answer("Формат: /status <id> <new|paid|packed|shipped|done|cancelled>")
        async with open_db() as d:

            await d.execute("UPDATE orders SET status=? WHERE id=?", (new_status.strip(), oid))
            await d.commit()
        await m.answer(f"Статус заказа #{oid} → {new_status}")

    @dp_admin.message(Command("ttn"))
    async def admin_ttn(m: Message, command: CommandObject):
        try:
            oid, ttn = command.args.split(maxsplit=1)
            oid = int(oid)
        except Exception:
            return await m.answer("Формат: /ttn <id> <номер>")
        async with open_db() as d:

            await d.execute("UPDATE orders SET np_ttn=? WHERE id=?", (ttn.strip(), oid))
            await d.commit()
        await m.answer(f"TTN для заказа #{oid} сохранён.")

    @dp_admin.message(Command("products"))
    async def admin_products(m: Message):
        async with open_db() as d:

            cur = await d.execute("SELECT sku,title,price,currency,is_active FROM products ORDER BY title")
            rows = await cur.fetchall()
        if not rows:
            return await m.answer("Каталог пуст.")
        txt = "\n".join([f"{'✅' if r[4] else '⛔️'} <b>{r[1]}</b> [{r[0]}] — {r[2]} {r[3]}" for r in rows])
        await m.answer(txt)

    @dp_admin.message(Command("addproduct"))
    async def admin_addproduct(m: Message, command: CommandObject):
        # /addproduct sku | Название | 999 [| UAH]
        if not command.args or "|" not in command.args:
            return await m.answer("Формат: /addproduct <sku> | <Название> | <цена> [| Валюта]")
        parts = [p.strip() for p in command.args.split("|")]
        if len(parts) < 3:
            return await m.answer("Нужно минимум: sku | Название | цена")
        sku, title, price = parts[:3]
        currency = parts[3] if len(parts) >= 4 else "UAH"
        try:
            price = int(price)
        except:
            return await m.answer("Цена должна быть целым числом (в копейках/гривнах без копеек — как решишь).")
        async with open_db() as d:

            await d.execute("INSERT INTO products (sku,title,price,currency,is_active) VALUES (?,?,?,?,1) ON CONFLICT(sku) DO UPDATE SET title=excluded.title, price=excluded.price, currency=excluded.currency, is_active=1", (sku, title, price, currency))
            await d.commit()
        await m.answer(f"Товар [{sku}] добавлен/обновлён: {title} — {price} {currency}")

    @dp_admin.message(Command("setprice"))
    async def admin_setprice(m: Message, command: CommandObject):
        try:
            sku, price = command.args.split(maxsplit=1)
            price = int(price)
        except Exception:
            return await m.answer("Формат: /setprice <sku> <цена>")
        async with open_db() as d:

            await d.execute("UPDATE products SET price=? WHERE sku=?", (price, sku))
            await d.commit()
        await m.answer(f"Цена {sku} → {price}")

    @dp_admin.message(Command("settitle"))
    async def admin_settitle(m: Message, command: CommandObject):
        if "|" not in (command.args or ""):
            return await m.answer("Формат: /settitle <sku> | <Новое название>")
        sku, title = [p.strip() for p in command.args.split("|", 1)]
        async with open_db() as d:

            await d.execute("UPDATE products SET title=? WHERE sku=?", (title, sku))
            await d.commit()
        await m.answer(f"Название {sku} → {title}")

    @dp_admin.message(Command("toggle"))
    async def admin_toggle(m: Message, command: CommandObject):
        sku = (command.args or "").strip()
        if not sku:
            return await m.answer("Формат: /toggle <sku>")
        async with open_db() as d:

            cur = await d.execute("SELECT is_active FROM products WHERE sku=?", (sku,))
            row = await cur.fetchone()
            if not row:
                return await m.answer("SKU не найден.")
            newv = 0 if row[0] else 1
            await d.execute("UPDATE products SET is_active=? WHERE sku=?", (newv, sku))
            await d.commit()
        await m.answer(f"{'Включен' if newv else 'Отключен'} товар {sku}")

# ---------- MENU BUTTON для shop ----------
async def setup_menu_button():
    if WEBAPP_URL:
        try:
            await bot_shop.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🛍 Открыть витрину", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception as e:
            print("set_chat_menu_button error:", e)

# ---------- RUN ----------
async def main():
    await init_db()
    await setup_menu_button()
    tasks = [asyncio.create_task(dp_shop.start_polling(bot_shop))]
    if dp_admin and bot_admin:
        tasks.append(asyncio.create_task(dp_admin.start_polling(bot_admin)))
    print("Shop bot и Admin bot запущены. Нажми Ctrl+C для остановки.")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
