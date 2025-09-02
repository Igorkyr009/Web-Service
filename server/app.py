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

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR  = BASE_DIR / "web"

DB_PATH    = os.getenv("DB_PATH", "/tmp/shop.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/var/data/uploads")

# uploads dir (Render: persistent disk is /var/data)
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

async def fetch_products(active_only: bool = True) -> List[Dict[str, Any]]:
    q = "SELECT sku,title,price,currency,image_url,description,is_active,category,stock_status FROM products"
    if active_only:
        q += " WHERE is_active=1"
    q += " ORDER BY rowid DESC"
    async with aiosqlite.connect(DB_PATH) as d:
        cur = await d.execute(q)
        rows = await cur.fetchall()
    cols = ["sku","title","price","currency","image_url","description","is_active","category","stock_status"]
    return [dict(zip(cols, r)) for r in rows]

async def fetch_product_by_sku(sku: str):
    async with aiosqlite.connect(DB_PATH) as d:
        cur = await d.execute(
            "SELECT sku,title,price,currency,image_url,description,is_active,category,stock_status FROM products WHERE sku=?",
            (sku,)
        )
        r = await cur.fetchone()
    if not r: return None
    cols = ["sku","title","price","currency","image_url","description","is_active","category","stock_status"]
    return dict(zip(cols, r))

async def upsert_product(p: Dict[str, Any]):
    async with aiosqlite.connect(DB_PATH) as d:
        await d.execute("""
          INSERT INTO products (sku,title,price,currency,image_url,description,is_active,category,stock_status)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(sku) DO UPDATE SET
            title=excluded.title,
            price=excluded.price,
            currency=excluded.currency,
            image_url=excluded.image_url,
            description=excluded.description,
            is_active=excluded.is_active,
            category=excluded.category,
            stock_status=excluded.stock_status
        """, (
            p["sku"], p["title"], int(p["price"]), p.get("currency","UAH"),
            p.get("image_url"), p.get("description"),
            1 if p.get("is_active") else 0,
            p.get("category","devices"),
            p.get("stock_status","in_stock")
        ))
        await d.commit()

async def delete_product(sku: str):
    async with aiosqlite.connect(DB_PATH) as d:
        await d.execute("DELETE FROM products WHERE sku=?", (sku,))
        await d.commit()

async def save_order(user, items: List[Tuple[str, str, int, int]], total: int, currency: str,
                     city: str, branch: str, receiver: str, phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as d:
        cur = await d.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user.id,
                f"@{user.username}" if getattr(user, "username", None) else None,
                f"{(getattr(user, 'first_name', '') or '').strip()} {(getattr(user, 'last_name','') or '').strip()}".strip(),
                total, currency, city, branch, receiver, phone,
                "new", int(time.time())
            )
        )
        order_id = cur.lastrowid
        for sku, title, price, qty in items:
            await d.execute(
                "INSERT INTO order_items (order_id,product_sku,product_title,price,qty) VALUES (?,?,?,?,?)",
                (order_id, sku, title, price, qty)
            )
        await d.commit()
    return int(order_id)

async def fetch_orders(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as d:
        cur = await d.execute(
            "SELECT id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        )
        orders = await cur.fetchall()
        out = []
        for o in orders:
            oid = o[0]
            cur2 = await d.execute(
                "SELECT product_sku,product_title,price,qty FROM order_items WHERE order_id=?",
                (oid,)
            )
            items = await cur2.fetchall()
            out.append({
                "id": oid,
                "tg_username": o[1],
                "tg_name": o[2],
                "total": o[3],
                "currency": o[4],
                "city": o[5],
                "branch": o[6],
                "receiver": o[7],
                "phone": o[8],
                "status": o[9],
                "created_at": o[10],
                "items": [{"sku":i[0],"title":i[1],"price":i[2],"qty":i[3]} for i in items]
            })
        return out

# -------------------- Telegram Bot --------------------
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

async def notify_admin_text(text: str):
    # try secondary bot first
    if ADMIN_BOT_TOKEN and ADMIN_CHAT_ID:
        try:
            other = Bot(ADMIN_BOT_TOKEN)
            await other.send_message(int(ADMIN_CHAT_ID), text)
            await other.session.close()
            return
        except Exception:
            pass
    # fallback to main bot
    if ADMIN_ID:
        try:
            await bot.send_message(int(ADMIN_ID), text)
        except Exception:
            pass

@dp.message(Command("start"))
async def cmd_start(m: Message):
    # В /start — только витрина для всех
    kb = [
        [{"text": "🛍 Вітрина", "web_app": {"url": f"{request_base()}/index.html"}}]
    ]
    await m.answer("Привіт! Відкрий міні-магазин нижче 👇", reply_markup={"inline_keyboard": kb})

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    # Кнопка адмінки только админу
    try:
        is_admin = (ADMIN_ID and str(m.from_user.id) == str(ADMIN_ID))
    except Exception:
        is_admin = False
    if not is_admin:
        return await m.answer("⛔️ Доступ заборонено.")
    kb = [
        [{"text": "🛒 Адмінка", "web_app": {"url": f"{request_base()}/admin.html"}}]
    ]
    await m.answer("Панель адміністратора:", reply_markup={"inline_keyboard": kb})

@dp.message(Command("setadmin"))
async def cmd_setadmin(m: Message):
    # закрепить текущего пользователя как админа
    global ADMIN_ID
    ADMIN_ID = str(m.from_user.id)
    await m.answer(f"Адмін встановлений: <code>{ADMIN_ID}</code>")

@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    # данные из мини-аппа Telegram
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Не вдалося прочитати дані з вітрини.")
    if data.get("type") != "checkout":
        return await m.answer("Невідомий тип даних від вітрини.")

    items_in  = data.get("items", [])
    items: List[Tuple[str,str,int,int]] = []
    total = 0
    currency = "UAH"

    for it in items_in:
        sku = str(it.get("sku"))
        qty = int(it.get("qty", 1))
        row = await fetch_product_by_sku(sku)
        if not row or qty <= 0 or not row.get("is_active"):
            continue
        items.append((row["sku"], row["title"], int(row["price"]), qty))
        total += int(row["price"]) * qty
        currency = row.get("currency","UAH")

    if not items:
        return await m.answer("Кошик порожній.")

    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()

    if not m.from_user.username:
        return await m.answer("Для оформлення замовлення потрібен нікнейм у Telegram (username). Додайте його в налаштуваннях Telegram.")

    order_id = await save_order(m.from_user, items, total, currency, city, branch, receiver, phone)

    await m.answer(f"✅ Замовлення №{order_id} успішно оформлено! Ми з вами зв’яжемося для підтвердження.")

    lines = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _,t,p,q in items])
    txt = (
        f"🆕 Нове замовлення №{order_id}\n"
        f"Покупець: {m.from_user.first_name or ''} {m.from_user.last_name or ''} "
        f"({('@'+m.from_user.username) if m.from_user.username else '—'})\n"
        f"ID: {m.from_user.id}\n"
        f"{lines}\nРазом: {total} {currency}\n"
        f"Місто: {city}\nВідділення: {branch}\n"
        f"Отримувач: {receiver} / {phone}"
    )
    await notify_admin_text(txt)

# -------------------- HTTP helpers --------------------
_request_base: str = ""
def request_base() -> str:
    return _request_base or os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# -------------------- HTTP API --------------------
def require_admin(request: web.Request):
    secret = request.headers.get("X-Admin-Secret") or request.query.get("secret")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise web.HTTPUnauthorized(text="Admin secret required")

async def api_catalog(request: web.Request):
    category = request.query.get("category")
    items = await fetch_products(active_only=True)
    if category:
        items = [i for i in items if (i.get("category") or "").lower() == category.lower()]
    return web.json_response({"items": items})

async def api_orders(request: web.Request):
    require_admin(request)
    limit = int(request.query.get("limit","50"))
    data = await fetch_orders(limit=limit)
    return web.json_response({"orders": data})

async def api_product_upsert(request: web.Request):
    require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")

    required = ["sku","title","price"]
    for k in required:
        if not body.get(k):
            raise web.HTTPBadRequest(text=f"field '{k}' required")

    body.setdefault("currency","UAH")
    body["is_active"] = 1 if body.get("is_active") in (True,1,"1","true","on") else 0
    body.setdefault("category","devices")
    body.setdefault("stock_status","in_stock")
    await upsert_product(body)
    return web.json_response({"ok": True})

async def api_product_delete(request: web.Request):
    require_admin(request)
    sku = request.match_info.get("sku","")
    await delete_product(sku)
    return web.json_response({"ok": True})

async def api_upload(request: web.Request):
    require_admin(request)
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        raise web.HTTPBadRequest(text="file field required")

    filename = field.filename or "upload.bin"
    ext = (Path(filename).suffix or "").lower()
    allow = {".jpg",".jpeg",".png",".webp"}
    if ext not in allow:
        raise web.HTTPUnsupportedMediaType(text="Allowed: jpg, jpeg, png, webp")

    rnd = secrets.token_hex(8) + ext
    path = Path(UPLOAD_DIR) / rnd
    with path.open("wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk: break
            f.write(chunk)

    url = f"/uploads/{rnd}"
    return web.json_response({"url": url})

# === Public checkout (работает и вне Telegram) ===
async def api_checkout(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")

    items_in = data.get("items", [])
    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()
    tg_user  = (data.get("tg_username") or "").strip().lstrip("@")

    if not items_in:
        raise web.HTTPBadRequest(text="empty cart")

    # Собираем позиции строго из БД
    items: List[Tuple[str,str,int,int]] = []
    total = 0
    currency = "UAH"
    for it in items_in:
        sku = str(it.get("sku"))
        qty = int(it.get("qty", 1))
        row = await fetch_product_by_sku(sku)
        if not row or qty <= 0 or not row.get("is_active"):
            continue
        items.append((row["sku"], row["title"], int(row["price"]), qty))
        total += int(row["price"]) * qty
        currency = row.get("currency","UAH")

    if not items:
        raise web.HTTPBadRequest(text="no valid items")

    # Псевдо-пользователь для браузерного оформления
    u = type("U", (), {})()
    u.id = 0
    u.username = tg_user or None
    u.first_name = ""
    u.last_name  = ""

    order_id = await save_order(u, items, total, currency, city, branch, receiver, phone)

    lines = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _,t,p,q in items])
    uname = f"@{tg_user}" if tg_user else "—"
    txt = (
        f"🆕 Нове замовлення №{order_id}\n"
        f"Покупець: {receiver} ({uname})\n"
        f"ID: 0 (браузер)\n"
        f"{lines}\nРазом: {total} {currency}\n"
        f"Місто: {city}\nВідділення: {branch}\n"
        f"Отримувач: {receiver} / {phone}"
    )
    await notify_admin_text(txt)

    return web.json_response({"ok": True, "order_id": order_id})

# -------------------- Static pages --------------------
async def static_index(request: web.Request):
    return web.FileResponse(WEB_DIR / "index.html")

async def static_admin(request: web.Request):
    return web.FileResponse(WEB_DIR / "admin.html")

async def health(request: web.Request):
    return web.json_response({"ok": True})

# -------------------- Run everything --------------------
async def start_bot_and_http():
    app = web.Application()
    app.router.add_get("/health", health)

    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/orders",  api_orders)
    app.router.add_post("/api/product", api_product_upsert)
    app.router.add_delete("/api/product/{sku}", api_product_delete)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_post("/api/checkout", api_checkout)

    app.router.add_get("/", static_index)
    app.router.add_get("/index.html", static_index)
    app.router.add_get("/admin.html", static_admin)

    app.router.add_static("/uploads/", UPLOAD_DIR)
    app.router.add_static("/web/", str(WEB_DIR))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP on :{PORT}")

    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🛍 Вітрина", web_app=WebAppInfo(url=f"{base}/index.html"))
            )
            print("Menu set to:", f"{base}/index.html")
        except Exception as e:
            print("Menu set error:", e)

    await dp.start_polling(bot)

async def main():
    await init_db()
    await start_bot_and_http()

if __name__ == "__main__":
    asyncio.run(main())








