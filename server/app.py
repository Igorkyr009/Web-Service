import os, asyncio, time, json, logging, uuid
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web"

# ---------- ENV ----------
load_dotenv()
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_SECRET     = os.getenv("ADMIN_SECRET", "").strip()
DB_PATH          = os.getenv("DB_PATH", "/data/shop.db").strip()
PORT             = int(os.getenv("PORT", "8000"))
WEBAPP_URL       = os.getenv("WEBAPP_URL", "").strip()             # ...index.html#catalog
WEBAPP_URL_HOME  = os.getenv("WEBAPP_URL_HOME", WEBAPP_URL).strip()# ...index.html#home
UPLOAD_DIR       = os.getenv("UPLOAD_DIR", "/data/uploads").strip()
# второй бот для уведомлений
ADMIN_BOT_TOKEN  = os.getenv("ADMIN_BOT_TOKEN", "").strip()

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN пуст")

# безопасная подготовка путей (fallback в /tmp если нет прав)
def _safe_prepare(db_path: str, upload_dir: str):
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(f"⚠️ No permission for {db_path}. Fallback to /tmp/shop.db")
        db_path = "/tmp/shop.db"
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        Path(upload_dir).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(f"⚠️ No permission for {upload_dir}. Fallback to /tmp/uploads")
        upload_dir = "/tmp/uploads"
        Path(upload_dir).mkdir(parents=True, exist_ok=True)
    return db_path, upload_dir

DB_PATH, UPLOAD_DIR = _safe_prepare(DB_PATH, UPLOAD_DIR)

print("DB_PATH         =", DB_PATH)
print("UPLOAD_DIR      =", UPLOAD_DIR)
print("WEBAPP_URL      =", WEBAPP_URL or "<empty>")
print("WEBAPP_URL_HOME =", WEBAPP_URL_HOME or "<empty>")

# ---------- Aiogram (Router) ----------
bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
rt  = Router()
dp.include_router(rt)

# опционально второй бот для уведомлений
admin_bot: Optional[Bot] = None
if ADMIN_BOT_TOKEN:
    try:
        admin_bot = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        print("Admin bot enabled")
    except Exception as e:
        print("Admin bot init error:", e)
        admin_bot = None

# ---------- DB ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS products(
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  is_active INTEGER NOT NULL DEFAULT 1,
  description TEXT,
  image_url TEXT,
  category TEXT
);

CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id INTEGER NOT NULL,
  tg_username TEXT,
  tg_name TEXT,
  total INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  city TEXT,
  branch TEXT,
  receiver TEXT,
  phone TEXT,
  buyer_username TEXT,  -- новый столбец (введённый пользователем @username)
  status TEXT DEFAULT 'new',
  np_ttn TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  product_sku TEXT NOT NULL,
  product_title TEXT NOT NULL,
  price INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""

def db():
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as d:
        await d.executescript(CREATE_SQL)
        # миграции на случай старых таблиц
        try:
            await d.execute("ALTER TABLE orders ADD COLUMN buyer_username TEXT")
        except Exception:
            pass
        try:
            await d.execute("ALTER TABLE products ADD COLUMN availability TEXT NOT NULL DEFAULT 'in_stock'")
        except Exception:
            pass
        await d.commit()

async def admin_chat_id() -> Optional[int]:
    async with db() as d:
        c = await d.execute("SELECT value FROM settings WHERE key='ADMIN_CHAT_ID'")
        r = await c.fetchone()
    return int(r[0]) if r else None

async def notify_admin(text: str):
    chat = await admin_chat_id()
    if not chat:
        return
    # если задан второй бот — шлём им; иначе основным
    try:
        if admin_bot:
            await admin_bot.send_message(chat, text)
        else:
            await bot.send_message(chat, text)
    except Exception as e:
        print("notify_admin:", e)

# ---------- BOT ----------
async def setup_menu_button():
    if not WEBAPP_URL:
        print("setup_menu_button: WEBAPP_URL is empty")
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🛍 Вітрина",
                web_app=WebAppInfo(url=WEBAPP_URL)   # нижняя кнопка -> каталог
            )
        )
        print("Menu set to:", WEBAPP_URL)
    except Exception as e:
        print("Menu set error:", e)

@rt.message(Command("start"))
async def start(m: Message):
    if not WEBAPP_URL_HOME:
        return await m.answer("WEBAPP_URL_HOME порожній.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Відкрити вітрину", web_app=WebAppInfo(url=WEBAPP_URL_HOME))
    kb.adjust(1)
    await m.answer("Привіт! Обери категорію:", reply_markup=kb.as_markup())

@rt.message(Command("setadmin"))
async def setadmin(m: Message):
    async with db() as d:
        await d.execute(
            "INSERT INTO settings(key,value) VALUES('ADMIN_CHAT_ID',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(m.chat.id),)
        )
        await d.commit()
    await m.answer("Цей чат призначено адмінським ✅\n(Не забудь натиснути /start у другому боті, якщо він увімкнений).")

@rt.message(Command("orders"))
async def orders(m: Message):
    if m.chat.id != await admin_chat_id():
        return await m.answer("Недостатньо прав.")
    async with db() as d:
        c = await d.execute(
            "SELECT id,total,currency,city,branch,receiver,phone,buyer_username,status "
            "FROM orders ORDER BY id DESC LIMIT 20"
        )
        rows = await c.fetchall()
    if not rows:
        return await m.answer("Замовлень ще немає.")
    out=[]
    for oid,total,cur,city,branch,recv,phone,uname,st in rows:
        out.append(
            f"#{oid} • {total} {cur}\n"
            f"{city or '-'}, {branch or '-'}\n"
            f"{recv or '-'} / {phone or '-'} / {uname or '—'}\n"
            f"Статус: {st}\n———"
        )
    await m.answer("\n".join(out))

@rt.message(F.web_app_data)
async def on_webapp(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Bad WebApp data.")
    if data.get("type") != "checkout":
        return await m.answer("Unknown type.")

    # собираем позиции по каталогу
    items=[]; total=0; currency="UAH"
    async with db() as d:
        for it in data.get("items", []):
            sku=str(it.get("sku")); qty=int(it.get("qty",1))
            c=await d.execute(
                "SELECT title,price,currency FROM products WHERE sku=? AND is_active=1",
                (sku,)
            )
            r=await c.fetchone()
            if not r or qty<=0: continue
            title, price, cur = r
            items.append((sku, title, price, qty))
            total += price * qty
            currency = cur
    if not items:
        return await m.answer("Порожня корзина.")

    city=(data.get("city") or "").strip()
    branch=(data.get("branch") or "").strip()
    receiver=(data.get("receiver") or "").strip()
    phone=(data.get("phone") or "").strip()
    buyer_username=(data.get("username") or "").strip()  # НОВОЕ поле из фронта

    async with db() as d:
        c = await d.execute(
            "INSERT INTO orders(tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,buyer_username,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.from_user.id,
             f"@{m.from_user.username}" if m.from_user.username else None,
             f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip(),
             total, currency, city, branch, receiver, phone, buyer_username, "new", int(time.time()))
        )
        await d.commit()
        order_id = c.lastrowid
        for sku,title,price,qty in items:
            await d.execute(
                "INSERT INTO order_items(order_id,product_sku,product_title,price,qty) VALUES (?,?,?,?,?)",
                (order_id, sku, title, price, qty)
            )
        await d.commit()

    # покупателю
    await m.answer(f"✅ Замовлення #{order_id} створено! Ми зв'яжемося з вами найближчим часом.")

    # админу — детальная сводка
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _, t, p, q in items])
    admin_msg = (
        f"🆕 Замовлення #{order_id}\n"
        f"{items_txt}\n"
        f"Разом: {total} {currency}\n"
        f"Доставка: {city} • {branch}\n"
        f"Отримувач: {receiver}\n"
        f"Телефон: {phone}\n"
        f"Юзернейм (з форми): {buyer_username or '—'}\n"
        f"ТГ профіль: {('@'+m.from_user.username) if m.from_user.username else '—'} (id {m.from_user.id})"
    )
    await notify_admin(admin_msg)

# ---------- HTTP ----------
@web.middleware
async def cors_mw(req, handler):
    if req.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(req)
    resp.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, X-Admin-Secret",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
    })
    return resp

async def health(_): return web.json_response({"ok": True})

async def catalog(_):
    async with db() as d:
        c = await d.execute(
            "SELECT sku,title,price,currency,COALESCE(image_url,''),COALESCE(description,''),"
            "COALESCE(category,''),COALESCE(availability,'in_stock'),is_active "
            "FROM products ORDER BY is_active DESC, title"
        )
        rows = await c.fetchall()
    items = [{
        "sku": r[0], "title": r[1], "price": r[2], "currency": r[3],
        "image_url": r[4], "description": r[5], "category": r[6],
        "availability": r[7], "is_active": bool(r[8])
    } for r in rows]
    return web.json_response({"items": items})

def need_admin(req):
    if not ADMIN_SECRET or req.headers.get("X-Admin-Secret") != ADMIN_SECRET:
        raise web.HTTPUnauthorized(text="bad secret")

async def upsert_product(req):
    need_admin(req)
    data = await req.json()
    for k in ("sku","title","price"):
        if not data.get(k):
            raise web.HTTPBadRequest(text=f"missing {k}")

    sku = str(data["sku"]).strip()
    title = str(data["title"]).strip()
    price = int(data["price"])
    currency=(data.get("currency") or "UAH").strip()
    image_url=(data.get("image_url") or "").strip()
    description=(data.get("description") or "").strip()
    category=(data.get("category") or "").strip()
    is_active = 1 if data.get("is_active", True) else 0
    availability=(data.get("availability") or "in_stock").strip()
    if availability not in ("in_stock","preorder"):
        return web.json_response({"error":"availability must be in_stock or preorder"}, status=400)

    async with db() as d:
        await d.execute(
            """INSERT INTO products(sku,title,price,currency,image_url,description,category,is_active,availability)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sku) DO UPDATE SET
                 title=excluded.title, price=excluded.price, currency=excluded.currency,
                 image_url=excluded.image_url, description=excluded.description,
                 category=excluded.category, is_active=excluded.is_active,
                 availability=excluded.availability""",
            (sku,title,price,currency,image_url,description,category,is_active,availability)
        )
        await d.commit()
    return web.json_response({"ok": True, "sku": sku})

# ---- загрузка изображений ----
ALLOWED_EXT = {".jpg",".jpeg",".png",".webp"}
MAX_FILE = 8 * 1024 * 1024  # 8MB

async def upload_image(request: web.Request):
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name not in ("file", "image"):
        return web.json_response({"error":"field 'file' is required"}, status=400)

    orig = (field.filename or "").lower()
    _, ext = os.path.splitext(orig)
    if ext not in ALLOWED_EXT:
        return web.json_response({"error":"allow: jpg, jpeg, png, webp"}, status=400)

    name = uuid.uuid4().hex + ext
    path = os.path.join(UPLOAD_DIR, name)

    size = 0
    try:
        with open(path, "wb") as f:
            while True:
                chunk = await field.read_chunk(1 << 20)  # 1MB
                if not chunk: break
                size += len(chunk)
                if size > MAX_FILE:
                    try: os.remove(path)
                    except OSError: pass
                    return web.json_response({"error":"file too big"}, status=413)
                f.write(chunk)
    except Exception as e:
        try: os.remove(path)
        except OSError: pass
        return web.json_response({"error":f"save failed: {e}"}, status=500)

    url = f"/uploads/{name}"
    return web.json_response({"url": url})

async def root(_):
    return web.FileResponse(STATIC_DIR / "index.html")

def make_app():
    app = web.Application(middlewares=[cors_mw], client_max_size=20*1024*1024)
    app.add_routes([
        web.get("/health", health),
        web.get("/api/catalog", catalog),
        web.post("/api/admin/products", upsert_product),
        web.options("/api/admin/products", upsert_product),
        web.post("/api/upload", upload_image),
        web.options("/api/upload", upload_image),
        web.get("/", root),
    ])
    app.router.add_static("/uploads/", path=UPLOAD_DIR, show_index=False)
    app.router.add_static("/", path=str(STATIC_DIR), show_index=False)
    return app

async def main():
    await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook:", e)

    await setup_menu_button()

    app = make_app()
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    print(f"HTTP on :{PORT}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())



