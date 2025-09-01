# /server/app.py
import os, asyncio, json, time, uuid
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, List, Tuple

import aiosqlite
from aiohttp import web
from PIL import Image
import pillow_heif


# ===================== ENV & DIRS =====================
DB_PATH     = os.getenv("DB_PATH", "./data/shop.db")
UPLOAD_DIR  = os.getenv("UPLOAD_DIR", "./uploads")
PUBLIC_DIR  = os.getenv("PUBLIC_DIR", "./public")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(PUBLIC_DIR).mkdir(parents=True, exist_ok=True)

pillow_heif.register_heif_opener()  # HEIC/HEIF поддержка


# ===================== DB SCHEMA =====================
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

async def db() -> aiosqlite.Connection:
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with await db() as d:
        await d.executescript(CREATE_SQL)
        await d.commit()


# ===================== HELPERS =====================
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


# ===================== API: PRODUCTS =====================
async def api_products(request: web.Request):
    """GET /api/products : все товары (для админки)"""
    q = request.rel_url.query
    where, params = [], []
    if "q" in q:
        where.append("(sku LIKE ? OR title LIKE ?)")
        v = f"%{q['q']}%"
        params += [v, v]
    if "category" in q:
        where.append("category = ?"); params.append(q["category"])
    sql = "SELECT sku,title,description,price,currency,category,is_active,availability,image_url FROM products"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, title"
    async with await db() as d:
        cur = await d.execute(sql, params)
        rows = await cur.fetchall()
    return ok({"items": [row_to_product(r) for r in rows]})

async def api_catalog(request: web.Request):
    """GET /api/catalog : активные товары (для витрины)"""
    q = request.rel_url.query
    where = ["is_active = 1"]
    params = []
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
    async with await db() as d:
        cur = await d.execute(sql, params)
        rows = await cur.fetchall()
    return ok({"items": [row_to_product(r) for r in rows]})

async def api_upsert_product(request: web.Request):
    """PUT /api/products/{sku} : создать/обновить товар (UPSERT)"""
    sku = request.match_info.get("sku", "").strip()
    if not sku:
        return err("empty sku")

    try:
        body = await request.json()
    except:
        return err("bad json")

    fields = {
        "title": None,
        "description": None,
        "price": None,
        "currency": None,
        "category": None,
        "is_active": None,
        "availability": None,
        "image_url": None,
    }
    for k in list(fields.keys()):
        if k in body:
            fields[k] = body[k]

    # upsert
    async with await db() as d:
        # exists?
        cur = await d.execute("SELECT COUNT(1) FROM products WHERE sku = ?", (sku,))
        exists = (await cur.fetchone())[0] > 0

        if exists:
            set_parts, values = [], []
            for k,v in fields.items():
                if v is not None:
                    set_parts.append(f"{k}=?"); values.append(v)
            if not set_parts:
                return ok()
            values.append(sku)
            await d.execute(f"UPDATE products SET {', '.join(set_parts)} WHERE sku=?", values)
        else:
            # defaults
            title = fields["title"] or sku
            price = int(fields["price"] or 0)
            currency = fields["currency"] or "UAH"
            category = fields["category"] or None
            is_active = int(fields["is_active"] if fields["is_active"] is not None else 1)
            availability = fields["availability"] or "in_stock"
            image_url = fields["image_url"] or None
            description = fields["description"] or None
            await d.execute("""
              INSERT INTO products (sku,title,description,price,currency,category,is_active,availability,image_url)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (sku, title, description, price, currency, category, is_active, availability, image_url))
        await d.commit()

    return ok()

# ===================== API: UPLOAD IMAGE =====================
async def api_upload(request: web.Request):
    """POST /api/upload : принимает image/*, центр-кроп в квадрат 800x800, JPEG, возвращает URL."""
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


# ===================== STATIC PAGES =====================
async def serve_index(request: web.Request):
    path = Path(PUBLIC_DIR) / "index.html"
    if not path.exists():
        return web.Response(status=404, text="index.html not found")
    return web.FileResponse(path)

async def serve_admin(request: web.Request):
    path = Path(PUBLIC_DIR) / "admin.html"
    if not path.exists():
        return web.Response(status=404, text="admin.html not found")
    return web.FileResponse(path)

async def root_redirect(request: web.Request):
    raise web.HTTPFound("/index.html")

async def health(request: web.Request):
    return ok({"status":"ok"})


# ===================== APP =====================
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
    # Static
    app.router.add_static("/uploads/", path=UPLOAD_DIR, show_index=False)
    # Pages
    app.add_routes([
        web.get("/",          root_redirect),
        web.get("/index.html", serve_index),
        web.get("/admin.html", serve_admin),
    ])
    return app


async def main():
    await init_db()
    app = make_app()
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"Web service started on 0.0.0.0:{port}")
    # keep alive
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())


