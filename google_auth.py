import hashlib
import hmac
import html
import os
import secrets
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg2
from psycopg2.extras import RealDictCursor
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

DB_PATH = os.getenv("DB_PATH", "bahrain_deals.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")


def is_pg() -> bool:
    return bool(DATABASE_URL)


def _clean_pg_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    filtered = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "channel_binding"
    ]
    if not any(k == "sslmode" for k, _ in filtered):
        filtered.append(("sslmode", "require"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment))


class DBConn:
    def __init__(self):
        if is_pg():
            self.kind = "pg"
            self.conn = psycopg2.connect(_clean_pg_dsn(DATABASE_URL))
        else:
            self.kind = "sqlite"
            self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

    def cursor(self):
        return DBCursor(self.conn, self.kind)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


class DBCursor:
    def __init__(self, raw_conn, kind: str):
        self.kind = kind
        if kind == "pg":
            self.cur = raw_conn.cursor(cursor_factory=RealDictCursor)
        else:
            self.cur = raw_conn.cursor()

    def _adapt_sql(self, sql: str) -> str:
        if self.kind == "pg":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params=()):
        self.cur.execute(self._adapt_sql(sql), params)
        return self

    def fetchone(self):
        row = self.cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        rows = self.cur.fetchall()
        return [dict(r) for r in rows]


def get_db():
    return DBConn()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash or not password:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    _, salt, hex_hash = parts
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(dk.hex(), hex_hash)


def init_auth_tables():
    conn = get_db()
    cur = conn.cursor()
    if conn.kind == "pg":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                google_id TEXT UNIQUE,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                name TEXT,
                picture TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE users ALTER COLUMN google_id DROP NOT NULL")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'users_email_key'
                ) THEN
                    ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);
                END IF;
            END $$;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS saved_deals (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                deal_key TEXT NOT NULL,
                title TEXT,
                price TEXT,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, deal_key)
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_id TEXT UNIQUE,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                name TEXT,
                picture TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("PRAGMA table_info(users)")
        cols = {r["name"] for r in cur.fetchall()}
        if "password_hash" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS saved_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                deal_key TEXT NOT NULL,
                title TEXT,
                price TEXT,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, deal_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
    conn.commit()
    conn.close()


def upsert_user(google_id: str, email: str, name: str, picture: str) -> int:
    email_norm = (email or "").strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE google_id = ? OR email = ?", (google_id, email_norm))
    existing = cur.fetchone()
    if existing:
        cur.execute(
            """
            UPDATE users
            SET google_id = COALESCE(google_id, ?),
                email = ?,
                name = COALESCE(?, name),
                picture = COALESCE(?, picture)
            WHERE id = ?
            """,
            (google_id, email_norm, name, picture, existing["id"]),
        )
        conn.commit()
        conn.close()
        return existing["id"]

    cur.execute(
        """
        INSERT INTO users (google_id, email, name, picture)
        VALUES (?, ?, ?, ?)
        """,
        (google_id, email_norm, name, picture),
    )
    conn.commit()
    cur.execute("SELECT id FROM users WHERE google_id = ?", (google_id,))
    row = cur.fetchone()
    conn.close()
    return row["id"]


def register_local_user(name: str, email: str, password: str) -> tuple[bool, str, int | None]:
    clean_name = (name or "").strip()
    clean_email = (email or "").strip().lower()
    if len(clean_name) < 2:
        return False, "من فضلك أدخل اسماً صحيحاً (حرفين على الأقل).", None
    if "@" not in clean_email or "." not in clean_email.split("@")[-1]:
        return False, "من فضلك أدخل بريداً إلكترونياً صحيحاً.", None
    if len(password or "") < 6:
        return False, "كلمة المرور يجب أن تكون 6 أحرف على الأقل.", None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE email = ?", (clean_email,))
    existing = cur.fetchone()
    if existing:
        conn.close()
        if existing.get("password_hash"):
            return False, "هذا البريد الإلكتروني مسجل بالفعل. جرّب تسجيل الدخول.", None
        return False, "هذا البريد مسجل عبر جوجل. استخدم زر (دخول بجوجل).", None

    pw_hash = hash_password(password)
    cur.execute(
        """
        INSERT INTO users (google_id, email, password_hash, name, picture)
        VALUES (NULL, ?, ?, ?, NULL)
        """,
        (clean_email, pw_hash, clean_name),
    )
    conn.commit()
    cur.execute("SELECT id FROM users WHERE email = ?", (clean_email,))
    created = cur.fetchone()
    conn.close()
    return True, "تم إنشاء الحساب بنجاح!", created["id"] if created else None


def authenticate_local_user(email: str, password: str) -> tuple[bool, str, int | None]:
    clean_email = (email or "").strip().lower()
    if not clean_email or not password:
        return False, "من فضلك أدخل البريد الإلكتروني وكلمة المرور.", None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE email = ?", (clean_email,))
    user_row = cur.fetchone()
    conn.close()

    if not user_row:
        return False, "البريد الإلكتروني أو كلمة المرور غير صحيحة.", None

    if not user_row.get("password_hash"):
        return False, "هذا الحساب مرتبط بجوجل ولا يملك كلمة مرور. استخدم (دخول بجوجل).", None

    if not verify_password(password, user_row["password_hash"]):
        return False, "البريد الإلكتروني أو كلمة المرور غير صحيحة.", None

    return True, "تم تسجيل الدخول بنجاح!", user_row["id"]


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, email, name, picture FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def render_auth_header(user: dict | None) -> str:
    if not user:
        return '<a href="/login/google" style="padding:6px 12px;border-radius:8px;background:#2563eb;color:#fff;text-decoration:none;white-space:nowrap;">دخول بجوجل</a>'
    safe_name = html.escape(user.get("name") or "مستخدم")
    return f'''
    <div style="display:flex;align-items:center;gap:8px;white-space:nowrap;">
        <a href="/wishlist" style="padding:6px 10px;border-radius:8px;background:#16a34a;color:#fff;text-decoration:none;">المفضلة</a>
        <span style="font-size:0.9rem;">{safe_name}</span>
        <a href="/logout" style="padding:6px 10px;border-radius:8px;background:#dc2626;color:#fff;text-decoration:none;">خروج</a>
    </div>
    '''


def setup_google_auth(app: FastAPI):
    init_auth_tables()

    is_https = (
        os.getenv("RENDER", "").lower() == "true"
        or os.getenv("HTTPS_ONLY", "").lower() == "true"
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=SECRET_KEY,
        same_site="lax",
        https_only=is_https,
    )

    oauth = OAuth()
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    @app.post("/auth/register")
    async def auth_register_post(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
    ):
        ok, msg, user_id = register_local_user(name, email, password)
        if not ok or not user_id:
            q = urlencode({"error": msg})
            return RedirectResponse(url=f"/register?{q}", status_code=302)
        request.session["user_id"] = user_id
        return RedirectResponse(url="/", status_code=302)

    @app.post("/auth/login")
    async def auth_login_post(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
    ):
        ok, msg, user_id = authenticate_local_user(email, password)
        if not ok or not user_id:
            q = urlencode({"error": msg})
            return RedirectResponse(url=f"/login?{q}", status_code=302)
        request.session["user_id"] = user_id
        return RedirectResponse(url="/", status_code=302)

    @app.get("/login/google")
    async def login_google(request: Request, next: str = "/"):
        if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
            return RedirectResponse(url="/login?error=google_not_configured", status_code=302)
        request.session["next_url"] = next if next.startswith("/") else "/"
        redirect_uri = str(request.url_for("auth_callback"))
        if is_https and redirect_uri.startswith("http://"):
            redirect_uri = "https://" + redirect_uri[len("http://") :]
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get("userinfo")
        if not user_info:
            user_info = await oauth.google.userinfo(token=token)

        google_id = user_info.get("sub")
        email = user_info.get("email", "")
        name = user_info.get("name") or email.split("@")[0]
        picture = user_info.get("picture", "")

        if not google_id:
            return RedirectResponse(url="/?auth_error=missing_sub", status_code=302)

        user_id = upsert_user(google_id, email, name, picture)
        request.session["user_id"] = user_id

        next_url = request.session.pop("next_url", "/")
        return RedirectResponse(url=next_url, status_code=302)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)

    @app.post("/api/wishlist/toggle")
    async def toggle_wishlist(request: Request):
        user = get_current_user(request)
        if not user:
            return JSONResponse({"ok": False, "error": "login_required"}, status_code=401)

        data = await request.json()
        deal_key = str(
            data.get("deal_key") or data.get("url") or data.get("title") or ""
        ).strip()
        if not deal_key:
            return JSONResponse({"ok": False, "error": "invalid_deal"}, status_code=400)

        title = str(data.get("title") or "")
        price = str(data.get("price") or "")
        url = str(data.get("url") or "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM saved_deals WHERE user_id = ? AND deal_key = ?",
            (user["id"], deal_key),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute("DELETE FROM saved_deals WHERE id = ?", (existing["id"],))
            saved = False
        else:
            cur.execute(
                """
                INSERT INTO saved_deals (user_id, deal_key, title, price, url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user["id"], deal_key, title, price, url),
            )
            saved = True

        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "saved": saved})

    @app.get("/wishlist", response_class=HTMLResponse)
    async def wishlist_page(request: Request):
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login/google?next=/wishlist", status_code=302)

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT deal_key, title, price, url, created_at FROM saved_deals WHERE user_id = ? ORDER BY id DESC",
            (user["id"],),
        )
        items = cur.fetchall()
        conn.close()

        cards_html = (
            "".join(
                f"""
            <div style="border:1px solid #333;border-radius:10px;padding:12px;margin-bottom:10px;">
                <div style="font-weight:bold;">{html.escape(item['title'] or 'عرض محفوظ')}</div>
                <div>السعر: {html.escape(item['price'] or '-')}</div>
                <div style="margin-top:8px;display:flex;gap:12px;">
                    {f'<a href="{html.escape(item["url"])}" target="_blank">فتح العرض</a>' if item['url'] else ''}
                    <a href="/logout">خروج</a>
                </div>
            </div>
            """
                for item in items
            )
            or "<p>مفيش عروض محفوظة في المفضلة لحد دلوقتي.</p>"
        )

        return HTMLResponse(
            f"""
            <html dir="rtl" lang="ar">
            <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>المفضلة - تخفيضات مشطشطة</title></head>
            <body style="font-family:sans-serif;max-width:720px;margin:24px auto;padding:0 16px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
                    <h2>عروضي المفضلة ({len(items)})</h2>
                    <div><a href="/">العودة للعروض</a> | <a href="/logout">تسجيل الخروج</a></div>
                </div>
                {cards_html}
            </body>
            </html>
            """
        )