import os, asyncio, time, json, mimetypes, secrets
from io import BytesIO
from pathlib import Path
from aiohttp import web
from dotenv import load_dotenv
import aiosqlite
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# -------------------- ENV --------------------
load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID         = os.getenv("ADMIN_ID", "").strip()
ADMIN_BOT_TOKEN  = os.getenv("ADMIN_BOT_TOKEN", "").strip()   # второй бот для уведомлений (необяз.)
ADMIN_CHAT_ID    = os.getenv("ADMIN_CHAT_ID", "").strip() or ADMIN_ID
ADMIN_SECRET     = os.getenv("ADMIN_SECRET", "").strip()      # ключ для админ-API (лучше задать)
PORT             = int(os.getenv("PORT", "8000"))

BASE_DIR   = Path(__file__).resolve().parent
WEB_DIR    = BASE_DIR / "web"
DATA_ROOT  = Path("/data")
TMP_ROOT   = Path("/tmp")

# DB и загрузки — с фоллбеком на /tmp если /data недоступна
DB_PATH    = (DATA_ROOT / "shop.db") if DATA_ROOT.exists() else (TMP_ROOT / "shop.db")
UPLOAD_DIR = (DATA_ROOT / "uploads") if DATA_ROOT.exists() else (TMP_ROOT / "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан")

# -------------------- TELEGRAM --------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()  # ВАЖНО: до декораторов!

# отдельный бот для уведомлений (если не задан — шлём с основного)
admin_bot = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if ADMIN_BOT_TOKEN else bot

async def notify_admin(text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await admin_bot.send_message(int(ADMIN_CHAT_ID), text)
    except Exception as e:
        print("notify_admin error:", e)

# -------------------- DB --------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  title TEXT,
  description TEXT,
  price INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'UAH',
  image_url TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  category TEXT,
  availability TEXT NOT NULL DEFAULT 'in_stock'
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

DEFAULT_CATALOG = {
    # примеры выключены (is_active=0) — создай свои в админке
    "coffee_1kg": {"title": "Кофе в зёрнах 1 кг", "price": 1299, "currency": "UAH", "image_url": "", "is_active": 0, "category":"devices", "availability":"in_stock"},
    "mug_brand":  {"title": "Кружка бренда",       "price":  299, "currency": "UAH", "image_url": "", "is_active": 0, "category":"devices", "availability":"in_stock"},
}

async def ensure_some_products():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM products")
        n = (await cur.fetchone())[0]
        if n == 0:
            for sku, p in DEFAULT_CATALOG.items():
                await db.execute(
                    "INSERT OR REPLACE INTO products (sku,title,description,price,currency,image_url,is_active,category,availability) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sku, p["title"], "", p["price"], p["currency"], p.get("image_url",""), p.get("is_active",1), p.get("category",""), p.get("availability","in_stock"))
                )
            await db.commit()

# -------------------- HTTP HELPERS --------------------
def check_admin_secret(request: web.Request) -> bool:
    if not ADMIN_SECRET:
        return True  # если секрет не задан — не блокируем (на свой риск)
    key = request.headers.get("X-Admin-Secret") or request.query.get("key")
    return key == ADMIN_SECRET

async def api_health(_):
    return web.json_response({"ok": True, "ts": int(time.time())})

async def api_catalog(_):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT sku,title,description,price,currency,image_url,is_active,category,availability
            FROM products WHERE is_active=1 ORDER BY category NULLS LAST, title
        """)
        rows = await cur.fetchall()
    items = [
        dict(sku=r[0], title=r[1], description=r[2], price=r[3], currency=r[4],
             image_url=r[5], is_active=bool(r[6]), category=r[7], availability=r[8])
        for r in rows
    ]
    return web.json_response({"items": items})

async def api_products(request: web.Request):
    if not check_admin_secret(request):
        return web.Response(status=401, text="unauthorized")
    q = (request.query.get("q") or "").strip()
    sql = """
      SELECT sku,title,description,price,currency,image_url,is_active,category,availability
      FROM products
    """
    args = []
    if q:
        sql += " WHERE sku LIKE ? OR title LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY category NULLS LAST, title"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        rows = await cur.fetchall()
    items = [
        dict(sku=r[0], title=r[1], description=r[2], price=r[3], currency=r[4],
             image_url=r[5], is_active=bool(r[6]), category=r[7], availability=r[8])
        for r in rows
    ]
    return web.json_response({"items": items})

async def api_put_product(request: web.Request):
    if not check_admin_secret(request):
        return web.Response(status=401, text="unauthorized")
    sku = request.match_info["sku"].strip()
    try:
        body = await request.json()
    except:
        body = {}
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    price = int(body.get("price") or 0)
    currency = (body.get("currency") or "UAH").strip()
    image_url = (body.get("image_url") or "").strip()
    is_active = 1 if int(body.get("is_active") or 0) else 0
    category = (body.get("category") or "").strip()
    availability = (body.get("availability") or "in_stock").strip()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
          INSERT INTO products (sku,title,description,price,currency,image_url,is_active,category,availability)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(sku) DO UPDATE SET
            title=excluded.title,
            description=excluded.description,
            price=excluded.price,
            currency=excluded.currency,
            image_url=excluded.image_url,
            is_active=excluded.is_active,
            category=excluded.category,
            availability=excluded.availability
        """, (sku, title, description, price, currency, image_url, is_active, category, availability))
        await db.commit()
    return web.json_response({"ok": True, "sku": sku})

async def api_upload(request: web.Request):
    if not check_admin_secret(request):
        return web.Response(status=401, text="unauthorized")
    reader = await request.multipart()
    part = await reader.next()
    if not part or part.name != "file":
        return web.Response(status=400, text="file part missing")

    raw = await part.read()
    # Обрезаем в квадрат 800x800 (JPEG)
    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:
        return web.Response(status=415, text="unsupported image format")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top  = (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((800, 800))
    out = BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    out.seek(0)

    name = f"{int(time.time())}_{secrets.token_hex(4)}.jpg"
    path = UPLOAD_DIR / name
    with open(path, "wb") as f:
        f.write(out.read())

    return web.json_response({"ok": True, "url": f"/uploads/{name}"})

async def api_test_notify(request: web.Request):
    if not check_admin_secret(request):
        return web.Response(status=401, text="unauthorized")
    text = request.query.get("text", "ping")
    await notify_admin(f"TEST: {text}")
    return web.json_response({"ok": True})

# статика
async def file_handler(request: web.Request):
    rel = request.match_info.get("path", "").strip("/") or "index.html"
    target = (WEB_DIR / rel).resolve()
    if not str(target).startswith(str(WEB_DIR)):
        return web.Response(status=403, text="forbidden")
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        return web.Response(status=404, text="not found")
    mime, _ = mimetypes.guess_type(str(target))
    return web.FileResponse(path=target, headers={"Content-Type": mime or "text/html; charset=utf-8"})

# -------------------- TELEGRAM HANDLERS --------------------
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()  # если хочешь переопределить домен вручную

async def setup_menu_button():
    url = (WEBAPP_URL or f"http://localhost:{PORT}/index.html") + "#/catalog"
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🛍 Вітрина", web_app=WebAppInfo(url=url)))
        print("Menu set to:", url)
    except Exception as e:
        print("set_chat_menu_button error:", e)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    base = WEBAPP_URL or f"http://localhost:{PORT}/index.html"
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Відкрити вітрину", web_app=WebAppInfo(url=f"{base}#/catalog"))
    kb.button(text="🧾 Оформлення",      web_app=WebAppInfo(url=f"{base}#/checkout"))
    kb.adjust(1)
    await m.answer("Вітаю! Оберіть дію:", reply_markup=kb.as_markup())

@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Ваш User ID: <code>{m.from_user.id}</code>\nChat ID: <code>{m.chat.id}</code>\nUsername: @{m.from_user.username or '—'}")

@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Не вдалося прочитати дані з вітрини.")

    if data.get("type") != "checkout":
        return await m.answer("Невідомий тип даних із вітрини.")

    items_in = data.get("items", [])
    if not items_in:
        return await m.answer("Кошик порожній.")

    # подтянем цены/названия из БД
    items = []
    total = 0
    currency = "UAH"
    async with aiosqlite.connect(DB_PATH) as db:
        for it in items_in:
            sku = str(it.get("sku"))
            qty = int(it.get("qty", 1))
            if qty <= 0:
                continue
            cur = await db.execute("SELECT title,price,currency FROM products WHERE sku=?", (sku,))
            row = await cur.fetchone()
            if not row:
                continue
            title, price, curcy = row
            items.append((sku, title, int(price), qty))
            total += int(price) * qty
            currency = curcy or currency

    if not items:
        return await m.answer("Кошик порожній.")

    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()
    username = (data.get("username") or "").strip()

    # сохранение заказа
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.from_user.id,
                f"@{m.from_user.username}" if m.from_user.username else None,
                f"{(m.from_user.first_name or '').strip()} {(m.from_user.last_name or '').strip()}".strip(),
                total, currency, city, branch, receiver, phone, "new", int(time.time())
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

    await m.answer(f"✅ Замовлення #{order_id} створено! Ми звʼяжемося щодо доставки.")

    # уведомление админу
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _, t, p, q in items])
    buyer_un = username or (('@'+m.from_user.username) if m.from_user.username else '—')
    buyer_name = f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip()
    admin_msg = (
        f"🆕 <b>Замовлення #{order_id}</b>\n"
        f"Клієнт: {buyer_name} ({buyer_un})\n"
        f"UserID: <code>{m.from_user.id}</code>\n\n"
        f"{items_txt}\n<b>Разом:</b> {total} {currency}\n\n"
        f"<b>Доставка (НП)</b>\n"
        f"Місто: {city}\nВідділення: {branch}\n"
        f"Отримувач: {receiver}\nТелефон: {phone}"
    )
    await notify_admin(admin_msg)

# -------------------- APP RUN --------------------
async def aiohttp_app():
    app = web.Application()
    # API
    app.add_routes([
        web.get('/health', api_health),
        web.get('/api/catalog', api_catalog),
        web.get('/api/products', api_products),
        web.put('/api/products/{sku}', api_put_product),
        web.post('/api/upload', api_upload),
        web.get('/api/test-notify', api_test_notify),
    ])
    # статика: /uploads/*
    app.router.add_static('/uploads', path=str(UPLOAD_DIR), name='uploads')
    # фронтенд (index.html, admin.html и т.д.)
    app.add_routes([
        web.get('/', lambda r: web.HTTPFound('/index.html')),
        web.get('/{path:.*}', file_handler),
    ])
    return app

async def main():
    print(f"DB_PATH    = {DB_PATH}")
    print(f"UPLOAD_DIR = {UPLOAD_DIR}")

    await init_db()
    await ensure_some_products()
    await setup_menu_button()

    app = await aiohttp_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"HTTP on :{PORT}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())




