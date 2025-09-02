import os, asyncio, json, time, secrets, mimetypes
from pathlib import Path
from typing import Dict, Any, List, Tuple

from aiohttp import web
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# ------------------- CONFIG -------------------
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR  = BASE_DIR / "web"

PORT        = int(os.getenv("PORT", "8000"))
DB_PATH     = os.getenv("DB_PATH", "/tmp/shop.db")
UPLOAD_DIR  = os.getenv("UPLOAD_DIR", "/data/uploads")  # если нет диска — упадем в /tmp
WEBAPP_URL  = os.getenv("WEBAPP_URL", "").strip()       # можно оставить пустым — отдадим локальный URL
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()    # задай в Render!
BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()  # если пусто — можно установить командой /setadmin

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан")

# подстрахуем каталог загрузки (если /data недоступен — используем /tmp)
try:
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    UPLOAD_DIR = "/tmp/uploads"
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

# ------------------- BOT -------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

# ------------------- DB -------------------
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  price INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'UAH',
  image_url TEXT,
  category TEXT NOT NULL DEFAULT 'devices', -- devices|liquids|cartridges
  is_active INTEGER NOT NULL DEFAULT 1,     -- 1 = показывать в каталоге
  availability TEXT NOT NULL DEFAULT 'in_stock', -- in_stock|preorder
  created_at INTEGER NOT NULL
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
  status TEXT NOT NULL DEFAULT 'new', -- new|processing|shipped|done|canceled
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
        # если пустой каталог — создадим пару примеров (можно потом удалить в админке)
        cur = await db.execute("SELECT COUNT(*) FROM products")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            now = int(time.time())
            demo = [
                ("device_1", "Vape Device X", "Надійний девайс.", 1899, "UAH", "", "devices", 1, "in_stock"),
                ("liquid_1", "Рідина Mango 30ml", "Соковите манго.", 349, "UAH", "", "liquids", 1, "in_stock"),
                ("cart_1", "Картридж 1.0Ω", "Сумісний з X.", 249, "UAH", "", "cartridges", 1, "preorder"),
            ]
            for sku, title, desc, price, curcy, img, cat, active, avail in demo:
                await db.execute(
                    "INSERT INTO products (sku,title,description,price,currency,image_url,category,is_active,availability,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sku,title,desc,price,curcy,img,cat,active,avail,now)
                )
            await db.commit()

async def list_products() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT sku,title,description,price,currency,image_url,category,is_active,availability "
            "FROM products ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    keys = ["sku","title","description","price","currency","image_url","category","is_active","availability"]
    return [dict(zip(keys, r)) for r in rows]

async def upsert_product(data: Dict[str, Any]):
    required = ["sku","title","price","category","availability","is_active"]
    for k in required:
        if k not in data:
            raise web.HTTPBadRequest(text=f"Missing field: {k}")
    async with aiosqlite.connect(DB_PATH) as db:
        # проверяем существование
        cur = await db.execute("SELECT id FROM products WHERE sku=?", (data["sku"],))
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE products SET title=?, description=?, price=?, currency=?, image_url=?, category=?, is_active=?, availability=? WHERE sku=?",
                (
                    data.get("title",""),
                    data.get("description",""),
                    int(data.get("price",0)),
                    data.get("currency","UAH") or "UAH",
                    data.get("image_url",""),
                    data.get("category","devices"),
                    1 if str(data.get("is_active","1")) in ("1","true","True") else 0,
                    data.get("availability","in_stock"),
                    data["sku"],
                )
            )
        else:
            await db.execute(
                "INSERT INTO products (sku,title,description,price,currency,image_url,category,is_active,availability,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    data["sku"],
                    data.get("title",""),
                    data.get("description",""),
                    int(data.get("price",0)),
                    data.get("currency","UAH") or "UAH",
                    data.get("image_url",""),
                    data.get("category","devices"),
                    1 if str(data.get("is_active","1")) in ("1","true","True") else 0,
                    data.get("availability","in_stock"),
                    int(time.time()),
                )
            )
        await db.commit()

async def delete_product(sku: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE sku=?", (sku,))
        await db.commit()

async def save_order(user, items: List[Tuple[str,str,int,int]], total: int, currency: str,
                     city: str, branch: str, receiver: str, phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(user.id),
                (f"@{user.username}" if getattr(user,"username",None) else None),
                f"{(user.first_name or '')} {(user.last_name or '')}".strip(),
                int(total), currency, city, branch, receiver, phone, "new", int(time.time())
            )
        )
        oid = cur.lastrowid
        for sku, title, price, qty in items:
            await db.execute(
                "INSERT INTO order_items (order_id, product_sku, product_title, price, qty) VALUES (?,?,?,?,?)",
                (oid, sku, title, int(price), int(qty))
            )
        await db.commit()
    return int(oid)

async def list_orders(limit: int = 100) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
    keys = ["id","tg_username","tg_name","total","currency","city","branch","receiver","phone","status","created_at"]
    return [dict(zip(keys, r)) for r in rows]

async def get_order(order_id: int) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at "
            "FROM orders WHERE id=?", (order_id,)
        )
        o = await cur.fetchone()
        if not o:
            raise web.HTTPNotFound(text="Order not found")
        cur = await db.execute(
            "SELECT product_sku,product_title,price,qty FROM order_items WHERE order_id=?", (order_id,)
        )
        items = await cur.fetchall()
    keys = ["id","tg_username","tg_name","total","currency","city","branch","receiver","phone","status","created_at"]
    order = dict(zip(keys, o))
    order["items"] = [{"sku":a, "title":b, "price":c, "qty":d} for a,b,c,d in items]
    return order

async def set_order_status(order_id: int, status: str):
    if status not in ("new","processing","shipped","done","canceled"):
        raise web.HTTPBadRequest(text="bad status")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        await db.commit()

# ------------------- ADMIN AUTH -------------------
def require_admin(request: web.Request):
    token = request.headers.get("X-Admin-Secret","").strip()
    if not ADMIN_SECRET or token != ADMIN_SECRET:
        raise web.HTTPUnauthorized(text="X-Admin-Secret required / mismatch")

# ------------------- HTTP HANDLERS -------------------
async def health(_):
    return web.json_response({"ok": True, "time": int(time.time())})

async def api_catalog(request: web.Request):
    items = await list_products()
    return web.json_response({"items": items})

async def api_admin_catalog_get(request: web.Request):
    require_admin(request)
    items = await list_products()
    return web.json_response({"items": items})

async def api_admin_catalog_upsert(request: web.Request):
    require_admin(request)
    data = await request.json()
    await upsert_product(data)
    return web.json_response({"ok": True})

async def api_admin_catalog_delete(request: web.Request):
    require_admin(request)
    sku = request.match_info["sku"]
    await delete_product(sku)
    return web.json_response({"ok": True})

async def api_admin_upload(request: web.Request):
    require_admin(request)
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "file":
        raise web.HTTPBadRequest(text="no file")
    filename = field.filename or f"u_{secrets.token_hex(4)}"
    # запретим .heic, т.к. не везде открывается
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".heic", ".heif"):
        raise web.HTTPBadRequest(text="HEIC не поддерживается. Сохраните как JPEG/PNG/WebP.")
    safe = f"{int(time.time())}_{secrets.token_hex(3)}{ext or '.jpg'}"
    path = Path(UPLOAD_DIR) / safe
    with open(path, "wb") as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    url = f"/uploads/{safe}"
    return web.json_response({"ok": True, "url": url})

async def api_admin_orders(request: web.Request):
    require_admin(request)
    items = await list_orders(200)
    return web.json_response({"orders": items})

async def api_admin_order_one(request: web.Request):
    require_admin(request)
    oid = int(request.match_info["order_id"])
    order = await get_order(oid)
    return web.json_response(order)

async def api_admin_order_status(request: web.Request):
    require_admin(request)
    oid = int(request.match_info["order_id"])
    data = await request.json()
    await set_order_status(oid, data.get("status","new"))
    return web.json_response({"ok": True})

# ------------------- STATIC -------------------
def guess_type(path: Path) -> str:
    t, _ = mimetypes.guess_type(str(path))
    return t or "application/octet-stream"

async def static_index(request: web.Request):
    # маршрутизация SPA по hash: index.html всегда
    path = WEB_DIR / "index.html"
    return web.Response(body=path.read_bytes(), content_type="text/html; charset=utf-8")

async def static_admin(request: web.Request):
    path = WEB_DIR / "admin.html"
    return web.Response(body=path.read_bytes(), content_type="text/html; charset=utf-8")

async def static_uploads(request: web.Request):
    name = request.match_info["name"]
    path = Path(UPLOAD_DIR) / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.Response(body=path.read_bytes(), content_type=guess_type(path))

# ------------------- BOT HANDLERS -------------------
ADMIN_ID_RUNTIME = ADMIN_CHAT_ID  # может быть пустой

async def notify_admin(text: str):
    global ADMIN_ID_RUNTIME
    if ADMIN_ID_RUNTIME and ADMIN_ID_RUNTIME.isdigit():
        try:
            await bot.send_message(int(ADMIN_ID_RUNTIME), text)
        except Exception:
            pass

@dp.message(Command("setadmin"))
async def cmd_setadmin(m: Message):
    global ADMIN_ID_RUNTIME
    ADMIN_ID_RUNTIME = str(m.chat.id)
    await m.answer(f"Адмін-чат встановлено: <code>{ADMIN_ID_RUNTIME}</code>")

@dp.message(Command("start"))
async def cmd_start(m: Message):
    base = WEBAPP_URL or f"http://{request_host_hint()}/index.html"
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Відкрити вітрину", web_app=WebAppInfo(url=f"{base}#/catalog"))
    kb.adjust(1)
    await m.answer("Вітаю! Відкрийте вітрину і обирайте товари:", reply_markup=kb.as_markup())

def request_host_hint() -> str:
    # подсказка для локалки и Render
    host = os.getenv("RENDER_EXTERNAL_URL","").replace("https://","").replace("http://","")
    if host:
        return host
    return f"localhost:{PORT}"

async def setup_menu_button():
    base = WEBAPP_URL or f"http://{request_host_hint()}/index.html"
    url = f"{base}#/catalog"
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🛍 Вітрина", web_app=WebAppInfo(url=url))
        )
        print("Menu set to:", url)
    except Exception as e:
        print("set_chat_menu_button error:", e)

@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    """
    Ждем JSON:
    {
      "type":"checkout",
      "items":[{"sku":"device_1","qty":2}, ...],
      "city":"Київ", "branch":"Відділення №25",
      "receiver":"Іван Іванов", "phone":"+380...",
      "username":"@nick",
      "agree18": true,
      "acceptRules": true
    }
    """
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Не вдалося прочитати дані з вітрини.")

    if data.get("type") != "checkout":
        return await m.answer("Отримано дані вітрини, але тип невідомий.")

    # валидации
    if not data.get("agree18") or not data.get("acceptRules"):
        return await m.answer("Потрібно підтвердити 18+ і правила.")
    username = (data.get("username") or "").strip()
    if not username.startswith("@"):
        return await m.answer("Вкажіть ваш нік у Telegram (починається з @).")

    # грузим цены и названия из БД по sku
    async with aiosqlite.connect(DB_PATH) as db:
        items: List[Tuple[str,str,int,int]] = []
        total = 0
        currency = "UAH"
        for it in data.get("items", []):
            sku = str(it.get("sku"))
            qty = int(it.get("qty", 1))
            cur = await db.execute("SELECT title,price,currency FROM products WHERE sku=?", (sku,))
            row = await cur.fetchone()
            if not row or qty <= 0:
                continue
            title, price, currency = row
            items.append((sku, title, int(price), qty))
            total += int(price) * qty

    if not items:
        return await m.answer("Корзина порожня.")

    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()

    # подменим user с никнеймом (если в чате открыли без username)
    user = m.from_user
    if username and not getattr(user, "username", None):
        class FakeUser:
            def __init__(self, u):
                self.id = u.id
                self.username = username.lstrip("@")
                self.first_name = u.first_name
                self.last_name  = u.last_name
        user = FakeUser(m.from_user)

    order_id = await save_order(user, items, total, currency, city, branch, receiver, phone)

    # уведомление админу
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _, t, p, q in items])
    admin_msg = (
        f"🆕 <b>Замовлення #{order_id}</b>\n"
        f"Клієнт: <b>{receiver}</b> / {phone}\n"
        f"TG: @{user.username if getattr(user,'username',None) else '—'} (ID: {user.id})\n"
        f"{items_txt}\n"
        f"<b>Всього:</b> {total} {currency}\n"
        f"Місто: {city}\nВідділення: {branch}\nСтатус: new"
    )
    await notify_admin(admin_msg)

    await m.answer(f"✅ Замовлення #{order_id} створено! Ми зв'яжемося щодо доставки.")

# ------------------- HTTP SERVER -------------------
async def make_app() -> web.Application:
    app = web.Application()
    # API
    app.router.add_get("/health", health)
    app.router.add_get("/api/catalog", api_catalog)

    app.router.add_get("/api/admin/catalog", api_admin_catalog_get)
    app.router.add_post("/api/admin/catalog", api_admin_catalog_upsert)
    app.router.add_delete("/api/admin/catalog/{sku}", api_admin_catalog_delete)

    app.router.add_post("/api/admin/upload", api_admin_upload)

    app.router.add_get("/api/admin/orders", api_admin_orders)
    app.router.add_get("/api/admin/orders/{order_id}", api_admin_order_one)
    app.router.add_post("/api/admin/orders/{order_id}/status", api_admin_order_status)

    # STATIC
    app.router.add_get("/", static_index)
    app.router.add_get("/index.html", static_index)
    app.router.add_get("/admin.html", static_admin)

    # uploads
    app.router.add_get("/uploads/{name}", static_uploads)

    # раздача ассетов (css/js/img) если добавишь в /web
    # app.router.add_static("/assets/", path=str(WEB_DIR / "assets"), show_index=True)

    return app

async def start_http():
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP on :{PORT}")
    await asyncio.Event().wait()

async def main():
    await init_db()
    await setup_menu_button()
    # параллельно HTTP и бот
    await asyncio.gather(
        start_http(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    print("DB_PATH        =", DB_PATH)
    print("UPLOAD_DIR     =", UPLOAD_DIR)
    print("WEBAPP_URL     =", WEBAPP_URL or "(auto)")
    asyncio.run(main())





