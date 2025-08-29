import os, asyncio, time, json, logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web"

# ---------- ENV
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_SECRET   = os.getenv("ADMIN_SECRET", "").strip()
DB_PATH        = os.getenv("DB_PATH", "/data/shop.db").strip()
PORT           = int(os.getenv("PORT", "8000"))
WEBAPP_URL     = os.getenv("WEBAPP_URL", "").strip()  # позже можно поставить https://<твой>.onrender.com/index.html
if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN пуст")

print("DB_PATH =", DB_PATH)

bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
async def delete_product(req):
    need_admin(req)
    data = await req.json()
    sku = str(data.get("sku","")).strip()
    if not sku:
        raise web.HTTPBadRequest(text="missing sku")
    async with db() as d:
        await d.execute("DELETE FROM products WHERE sku=?", (sku,))
        await d.commit()
    return web.json_response({"ok": True, "deleted": sku})
web.post("/api/admin/products/delete", delete_product),
web.options("/api/admin/products/delete", delete_product),

# ---------- DB
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS products(
  sku TEXT PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH', is_active INTEGER NOT NULL DEFAULT 1,
  description TEXT, image_url TEXT, category TEXT
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tg_user_id INTEGER NOT NULL,
  tg_username TEXT, tg_name TEXT, total INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'UAH',
  city TEXT, branch TEXT, receiver TEXT, phone TEXT, status TEXT DEFAULT 'new',
  np_ttn TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
  product_sku TEXT NOT NULL, product_title TEXT NOT NULL, price INTEGER NOT NULL, qty INTEGER NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""

def db():  # соединение
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as d:
        await d.executescript(CREATE_SQL)
        c = await d.execute("SELECT COUNT(*) FROM products")
        (cnt,) = await c.fetchone()
        if cnt == 0:
            await d.executemany(
                "INSERT INTO products(sku,title,price,currency,is_active,description,image_url,category) "
                "VALUES (?,?,?,?,1,?,?,?)",
                [
                    ("coffee_1kg","Кава в зернах 1 кг",1299,"UAH","Арабiка 100%","https://via.placeholder.com/600x400?text=Coffee","Кофе"),
                    ("mug_brand","Фірмова чашка",299,"UAH","Кераміка 330мл","https://via.placeholder.com/600x400?text=Mug","Мерч"),
                ]
            )
        await d.commit()

async def admin_chat_id() -> Optional[int]:
    async with db() as d:
        c = await d.execute("SELECT value FROM settings WHERE key='ADMIN_CHAT_ID'")
        r = await c.fetchone()
    return int(r[0]) if r else None

async def notify_admin(text: str):
    chat = await admin_chat_id()
    if not chat: return
    try:
        await bot.send_message(chat, text)
    except Exception as e:
        print("notify_admin:", e)

# ---------- BOT
@dp.message(Command("start"))
async def start(m: Message):
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🛍 Відкрити вітрину", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)
    await m.answer("Привіт! Відкрий вітрину та обери товари.", reply_markup=kb.as_markup())

@dp.message(Command("webapp"))
async def webapp_cmd(m: Message):
    if not WEBAPP_URL:
        return await m.answer("WEBAPP_URL ще не встановлено.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Вітрина", web_app=WebAppInfo(url=WEBAPP_URL))
    await m.answer("Відкрити:", reply_markup=kb.as_markup())

@dp.message(Command("setadmin"))
async def setadmin(m: Message):
    async with db() as d:
        await d.execute(
            "INSERT INTO settings(key,value) VALUES('ADMIN_CHAT_ID',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(m.chat.id),)
        )
        await d.commit()
    await m.answer("Цей чат призначено адмінським ✅")

@dp.message(Command("admin"))
async def admin_btn(m: Message):
    if m.chat.id != await admin_chat_id():
        return await m.answer("Недостатньо прав.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔧 Відкрити адмінку", web_app=WebAppInfo(url="/admin.html"))
    await m.answer("Адмін-панель:", reply_markup=kb.as_markup())

@dp.message(Command("orders"))
async def orders(m: Message):
    if m.chat.id != await admin_chat_id():
        return await m.answer("Недостатньо прав.")
    async with db() as d:
        c = await d.execute(
            "SELECT id,total,currency,city,branch,receiver,phone,status "
            "FROM orders ORDER BY id DESC LIMIT 20"
        )
        rows = await c.fetchall()
    if not rows: return await m.answer("Замовлень ще немає.")
    out=[]
    for oid,total,cur,city,branch,recv,phone,st in rows:
        out.append(f"#{oid} • {total} {cur}\n{city or '-'}, {branch or '-'}\n{recv or '-'} / {phone or '-'}\nСтатус: {st}\n———")
    await m.answer("\n".join(out))

@dp.message(F.web_app_data)
async def on_webapp(m: Message):
    try: data = json.loads(m.web_app_data.data)
    except Exception: return await m.answer("Bad WebApp data.")
    if data.get("type")!="checkout": return await m.answer("Unknown type.")
    # собираем позиции
    items=[]; total=0; currency="UAH"
    async with db() as d:
        for it in data.get("items",[]):
            sku=str(it.get("sku")); qty=int(it.get("qty",1))
            c=await d.execute("SELECT title,price,currency FROM products WHERE sku=? AND is_active=1",(sku,))
            r=await c.fetchone()
            if not r or qty<=0: continue
            title,price,cur=r; items.append((sku,title,price,qty)); total+=price*qty; currency=cur
    if not items: return await m.answer("Порожня корзина.")
    city=(data.get("city") or ""); branch=(data.get("branch") or "")
    receiver=(data.get("receiver") or ""); phone=(data.get("phone") or "")
    async with db() as d:
        c=await d.execute(
            "INSERT INTO orders(tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m.from_user.id, f"@{m.from_user.username}" if m.from_user.username else None,
             f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip(),
             total,currency,city,branch,receiver,phone,"new",int(time.time()))
        ); await d.commit()
        order_id=c.lastrowid
        for sku,title,price,qty in items:
            await d.execute("INSERT INTO order_items(order_id,product_sku,product_title,price,qty) VALUES (?,?,?,?,?)",
                            (order_id,sku,title,price,qty))
        await d.commit()
    await m.answer(f"✅ Замовлення #{order_id} створено!")
    await notify_admin(f"🆕 Замовлення #{order_id}\nРазом: {total} {currency}\n{city} • {branch}\n{receiver} / {phone}")

# ---------- HTTP (API + статика)
@web.middleware
async def cors_mw(req, handler):
    if req.method == "OPTIONS": resp = web.Response()
    else: resp = await handler(req)
    resp.headers.update({
        "Access-Control-Allow-Origin":"*",
        "Access-Control-Allow-Headers":"Content-Type, X-Admin-Secret",
        "Access-Control-Allow-Methods":"GET,POST,OPTIONS"
    })
    return resp

async def health(_): return web.json_response({"ok": True})

async def catalog(_):
    async with db() as d:
        c=await d.execute("SELECT sku,title,price,currency,COALESCE(image_url,''),COALESCE(description,''),COALESCE(category,''),is_active FROM products ORDER BY is_active DESC,title")
        rows=await c.fetchall()
    items=[{"sku":r[0],"title":r[1],"price":r[2],"currency":r[3],"image_url":r[4],"description":r[5],"category":r[6],"is_active":bool(r[7])} for r in rows]
    return web.json_response({"items":items})

def need_admin(req):
    if not ADMIN_SECRET or req.headers.get("X-Admin-Secret","") != ADMIN_SECRET:
        raise web.HTTPUnauthorized(text="bad secret")

async def upsert_product(req):
    need_admin(req)
    data=await req.json()
    for k in ("sku","title","price"):
        if not data.get(k): raise web.HTTPBadRequest(text=f"missing {k}")
    sku=str(data["sku"]).strip(); title=str(data["title"]).strip(); price=int(data["price"])
    currency=(data.get("currency") or "UAH").strip()
    image_url=(data.get("image_url") or "").strip()
    description=(data.get("description") or "").strip()
    category=(data.get("category") or "").strip()
    is_active=1 if data.get("is_active",True) else 0
    async with db() as d:
        await d.execute(
            """INSERT INTO products(sku,title,price,currency,image_url,description,category,is_active)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(sku) DO UPDATE SET
                 title=excluded.title, price=excluded.price, currency=excluded.currency,
                 image_url=excluded.image_url, description=excluded.description,
                 category=excluded.category, is_active=excluded.is_active""",
            (sku,title,price,currency,image_url,description,category,is_active)
        ); await d.commit()
    return web.json_response({"ok":True,"sku":sku})

async def root(_):
    return web.FileResponse(STATIC_DIR / "index.html")

def make_app():
    app = web.Application(middlewares=[cors_mw])
    app.add_routes([
        web.get("/health", health),
        web.get("/api/catalog", catalog),
        web.post("/api/admin/products", upsert_product),
        web.options("/api/catalog", catalog),
        web.options("/api/admin/products", upsert_product),
        web.get("/", root),
    ])
    app.router.add_static("/", path=str(STATIC_DIR), show_index=False)
    return app

async def main():
    await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook:", e)
    app = make_app()
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    print(f"HTTP on :{PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


