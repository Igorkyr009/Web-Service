# /server/app.py
import os, asyncio, json, time, uuid
from io import BytesIO
from pathlib import Path
from typing import Dict, Any

import aiosqlite
from aiohttp import web
from PIL import Image
# вверху файла рядом с другими импортами
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# после загрузки .env
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID   = os.getenv("ADMIN_CHAT_ID", "").strip() or os.getenv("ADMIN_ID","").strip()

# основной бот (магазин) у тебя уже есть: bot = Bot(BOT_TOKEN, ...)
# создаём (по возможности) отдельного админ-бота
admin_bot = None
if ADMIN_BOT_TOKEN:
    admin_bot = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
else:
    # если отдельного бота нет — будем слать с основного
    admin_bot = bot

async def notify_admin_text(text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await admin_bot.send_message(int(ADMIN_CHAT_ID), text)
    except Exception as e:
        print("notify_admin error:", e)
@dp.message(F.web_app_data)
async def on_webapp_data(m: Message):
    data = json.loads(m.web_app_data.data)
    if data.get("type") != "checkout":
        return await m.answer("Невідомий тип даних із вітрини.")

    # содержимое корзины (как у тебя было)
    items = []
    total = 0
    currency = "UAH"
    for it in data.get("items", []):
        sku = str(it.get("sku")); qty = int(it.get("qty", 1))
        p = CATALOG.get(sku)
        if not p or qty <= 0: continue
        items.append((sku, p["title"], p["price"], qty))
        total += p["price"] * qty
        currency = p["currency"]

    if not items:
        return await m.answer("Корзина пуста.")

    city     = (data.get("city") or "").strip()
    branch   = (data.get("branch") or "").strip()
    receiver = (data.get("receiver") or "").strip()
    phone    = (data.get("phone") or "").strip()
    username = (data.get("username") or "").strip()  # НОВОЕ (обязателен на фронте)

    # сохраним заказ в БД (как и раньше)
    order_id = await save_order(
        m.from_user, items, total, currency, city, branch, receiver, phone
    )

    # ответ покупателю в чат
    await m.answer(f"✅ Замовлення #{order_id} створено! Ми звʼяжемося з вами щодо доставки.")

    # Уведомление админу (в отдельного админ-бота/чат)
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
    await notify_admin_text(admin_msg)

# HEIC/HEIF (необязательно; если не соберётся — просто будет OFF)
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    print("HEIF support: ON")
except Exception as e:
    print(f"HEIF support: OFF ({e})")

# ---------- ПУТИ ----------
BASE_DIR   = Path(__file__).parent               # /server
DB_PATH    = os.getenv("DB_PATH", "/tmp/shop.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/uploads")
PUBLIC_DIR = os.getenv("PUBLIC_DIR", str(BASE_DIR / "web"))

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(PUBLIC_DIR).mkdir(parents=True, exist_ok=True)

# ---------- СХЕМА ----------
CREATE_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  category TEXT,
  is_active INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT 'in_stock', -- in_stock | preorder
  image_url TEXT
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

def db():
    # ВАЖНО: без await — иначе словишь "threads can only be started once"
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as d:
        await d.executescript(CREATE_SQL)
        await d.commit()

# ---------- УТИЛИТЫ ----------
def row_to_product(row) -> Dict[str, Any]:
    return {
        "sku": row[0],
        "title": row[1],
        "description": row[2],
        "price": row[3],
        "currency": row[4],
        "category": row[5],
        "is_active": bool(row[6]),
        "availability": row[7],
        "image_url": row[8],
    }

def ok(data=None, **kw):
    base = {"ok": True}
    if data is not None:
        base.update(data if isinstance(data, dict) else {"data": data})
    base.update(kw)
    return web.json_response(base)

def err(msg: str, code=400):
    return web.json_response({"ok": False, "error": msg}, status=code)

# ---------- API: ТОВАРЫ ----------
async def api_products(request: web.Request):
    """GET /api/products — список для админки (все товары)"""
    q = request.rel_url.query
    where, params = [], []
    if "q" in q:
        where.append("(sku LIKE ? OR title LIKE ?)")
        v = f"%{q['q']}%"; params += [v, v]
    if "category" in q:
        where.append("category = ?"); params.append(q["category"])
    sql = "SELECT sku,title,description,price,currency,category,is_active,availability,image_url FROM products"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, title"
    async with db() as d:
        cur = await d.execute(sql, params)
        rows = await cur.fetchall()
    return ok({"items": [row_to_product(r) for r in rows]})

async def api_catalog(request: web.Request):
    """GET /api/catalog — активные товары для витрины"""
    q = request.rel_url.query
    where, params = ["is_active = 1"], []
    if "category" in q:
        where.append("category = ?"); params.append(q["category"])
    if "q" in q:
        where.append("(sku LIKE ? OR title LIKE ?)")
        v = f"%{q['q']}%"; params += [v, v]
    sql = f"""
      SELECT sku,title,description,price,currency,category,is_active,availability,image_url
      FROM products
      WHERE {' AND '.join(where)}
      ORDER BY category, title
    """
    async with db() as d:
        cur = await d.execute(sql, params)
        rows = await cur.fetchall()
    return ok({"items": [row_to_product(r) for r in rows]})

async def api_upsert_product(request: web.Request):
    """PUT /api/products/{sku} — создать/обновить товар (UPSERT)"""
    sku = request.match_info.get("sku", "").strip()
    if not sku:
        return err("empty sku")
    try:
        body = await request.json()
    except:
        return err("bad json")

    fields = {
        "title": None, "description": None, "price": None, "currency": None,
        "category": None, "is_active": None, "availability": None, "image_url": None,
    }
    for k in list(fields.keys()):
        if k in body:
            fields[k] = body[k]

    async with db() as d:
        cur = await d.execute("SELECT COUNT(1) FROM products WHERE sku=?", (sku,))
        exists = (await cur.fetchone())[0] > 0

        if exists:
            set_parts, values = [], []
            for k,v in fields.items():
                if v is not None:
                    set_parts.append(f"{k}=?"); values.append(v)
            if set_parts:
                values.append(sku)
                await d.execute(f"UPDATE products SET {', '.join(set_parts)} WHERE sku=?", values)
        else:
            title = fields["title"] or sku
            price = int(fields["price"] or 0)
            currency = fields["currency"] or "UAH"
            category = fields["category"]
            is_active = int(fields["is_active"] if fields["is_active"] is not None else 1)
            availability = fields["availability"] or "in_stock"
            image_url = fields["image_url"]
            description = fields["description"]
            await d.execute("""
              INSERT INTO products (sku,title,description,price,currency,category,is_active,availability,image_url)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (sku, title, description, price, currency, category, is_active, availability, image_url))
        await d.commit()
    return ok()

# ---------- API: UPLOAD ----------
async def api_upload(request: web.Request):
    """POST /api/upload — image/* → центр-кроп 800×800 JPEG → путь /uploads/xxx.jpg"""
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name not in ("file","image","photo"):
        return err("no file")
    data = await field.read(decode=False)
    if not data:
        return err("empty file")
    try:
        im = Image.open(BytesIO(data))
    except Exception as e:
        return err(f"bad image: {e}")
    if im.mode not in ("RGB","L"):
        im = im.convert("RGB")
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top  = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    im = im.resize((800, 800), Image.LANCZOS)

    name = f"{uuid.uuid4().hex}.jpg"
    out_path = Path(UPLOAD_DIR) / name
    try:
        im.save(out_path, "JPEG", quality=88, optimize=True, progressive=True)
    except Exception as e:
        return err(f"save error: {e}", 500)
    return ok({"url": f"/uploads/{name}"})

# ---------- СТРАНИЦЫ ----------
async def serve_index(request: web.Request):
    path = Path(PUBLIC_DIR) / "index.html"
    return web.FileResponse(path) if path.exists() else web.Response(status=404, text="index.html not found")

async def serve_admin(request: web.Request):
    path = Path(PUBLIC_DIR) / "admin.html"
    return web.FileResponse(path) if path.exists() else web.Response(status=404, text="admin.html not found")

async def root_redirect(request: web.Request):
    raise web.HTTPFound("/index.html")

async def health(request: web.Request):
    return ok({"status":"ok"})

# ---------- APP ----------
def make_app() -> web.Application:
    app = web.Application()
    # API
    app.add_routes([
        web.get("/api/products",  api_products),
        web.get("/api/catalog",   api_catalog),
        web.put("/api/products/{sku}", api_upsert_product),
        web.post("/api/upload",   api_upload),
        web.get("/health",        health),
    ])
    # статика с загруженными
    app.router.add_static("/uploads/", path=UPLOAD_DIR, show_index=False)
    # страницы
    app.add_routes([
        web.get("/",           root_redirect),
        web.get("/index.html", serve_index),
        web.get("/admin.html", serve_admin),
    ])
    return app

async def main():
    await init_db()
    app = make_app()
    port = int(os.getenv("PORT", "10000"))  # Render подставляет $PORT
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"HTTP on :{port}", flush=True)
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())


