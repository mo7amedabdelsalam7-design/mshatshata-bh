import os
import sqlite3
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
import uvicorn

DB_FILE = "bahrain_deals.db"
AFFILIATE_PARAM = "utm_source"
AFFILIATE_CODE = ""
TELEGRAM_CHANNEL_URL = "https://t.me/+FN5Lg1E3z0MyYWY0"

app = FastAPI(title="تخفيضات مشطشطة — عروض البحرين")


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db_ready():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY,
            store TEXT DEFAULT 'noon_bh',
            name TEXT NOT NULL,
            category TEXT,
            image_url TEXT,
            product_url TEXT NOT NULL,
            current_price REAL NOT NULL,
            old_price REAL,
            discount_pct REAL DEFAULT 0,
            lowest_price REAL NOT NULL,
            highest_price REAL NOT NULL,
            avg_price REAL NOT NULL,
            is_real_deal INTEGER DEFAULT 0,
            in_stock INTEGER DEFAULT 1,
            last_alert_at REAL DEFAULT 0,
            last_alert_price REAL,
            last_price_change_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            price REAL NOT NULL,
            recorded_at REAL NOT NULL,
            FOREIGN KEY (sku) REFERENCES products(sku)
        )
    """)
    conn.commit()
    conn.close()


def apply_affiliate(raw_url: str) -> str:
    if not raw_url or not AFFILIATE_CODE.strip():
        return raw_url
    parts = urlsplit(raw_url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != AFFILIATE_PARAM]
    q.append((AFFILIATE_PARAM, AFFILIATE_CODE.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def format_time_ago(ts: Optional[float]) -> str:
    if not ts:
        return "الآن"
    diff = max(0, int(time.time() - ts))
    if diff < 60:
        return "منذ لحظات"
    if diff < 3600:
        return f"منذ {diff // 60} دقيقة"
    if diff < 86400:
        return f"منذ {diff // 3600} ساعة"
    return f"منذ {diff // 86400} يوم"


@app.on_event("startup")
def startup_event():
    ensure_db_ready()


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    if os.path.exists("logo.png"):
        return FileResponse("logo.png", media_type="image/png")
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
      <rect width="64" height="64" rx="16" fill="#CE1126"/>
      <path d="M0 0 H22 L34 10.6 L22 21.3 L34 32 L22 42.6 L34 53.3 L22 64 H0 Z" fill="#FFFFFF"/>
      <path d="M44 18 C50 24, 52 36, 42 46 C38 50, 33 51, 31 49 C35 45, 39 36, 37 26 C36 22, 40 16, 44 18 Z" fill="#FFD166"/>
      <path d="M43 17 C45 13, 49 11, 52 12" stroke="#2EC4B6" stroke-width="3" stroke-linecap="round" fill="none"/>
    </svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/logo.png", include_in_schema=False)
def serve_logo():
    if os.path.exists("logo.png"):
        return FileResponse("logo.png", media_type="image/png")
    return Response(status_code=404)


# ===== API Endpoints =====
@app.get("/api/summary")
def api_summary():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM products WHERE in_stock = 1")
    total_active = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM products WHERE in_stock = 1 AND is_real_deal = 1")
    real_deals = cur.fetchone()[0] or 0

    cur.execute("SELECT COALESCE(MAX(CAST(discount_pct AS REAL)), 0), MAX(updated_at) FROM products WHERE in_stock = 1")
    row = cur.fetchone()
    max_disc = round(row[0] or 0, 1)
    last_updated = row[1]

    cur.execute("""
        SELECT category, COUNT(*) as cnt, COALESCE(MAX(CAST(discount_pct AS REAL)), 0) as max_disc
        FROM products
        WHERE in_stock = 1 AND category IS NOT NULL AND category != ''
        GROUP BY category
        ORDER BY cnt DESC
    """)
    categories = [
        {"name": r["category"], "count": r["cnt"], "max_discount": round(r["max_disc"], 0)}
        for r in cur.fetchall()
    ]
    conn.close()
    return {
        "total_active": total_active,
        "real_deals": real_deals,
        "max_discount": max_disc,
        "last_updated_text": format_time_ago(last_updated),
        "categories": categories,
    }


@app.get("/api/products")
def api_products(
    q: str = "",
    category: str = "",
    store: str = "",
    real_only: int = 0,
    min_discount: float = 0,
    sort: str = "recent",
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
):
    conn = get_db()
    cur = conn.cursor()

    where = ["in_stock = 1"]
    params = []

    if q.strip():
        where.append("name LIKE ?")
        params.append(f"%{q.strip()}%")
    if category.strip():
        where.append("category = ?")
        params.append(category.strip())
    if store.strip():
        where.append("store = ?")
        params.append(store.strip())
    if real_only == 1:
        where.append("is_real_deal = 1")
    if min_discount > 0:
        where.append("CAST(discount_pct AS REAL) >= ?")
        params.append(min_discount)

    where_sql = " AND ".join(where)

    order_map = {
        "recent": "CAST(last_price_change_at AS REAL) DESC, CAST(updated_at AS REAL) DESC, sku ASC",
        "discount": "CAST(discount_pct AS REAL) DESC, CAST(current_price AS REAL) ASC, sku ASC",
        "price_asc": "CAST(current_price AS REAL) ASC, sku ASC",
        "price_desc": "CAST(current_price AS REAL) DESC, sku ASC",
        "real": "CAST(is_real_deal AS REAL) DESC, CAST(discount_pct AS REAL) DESC, sku ASC",
    }
    order_sql = order_map.get(sort, order_map["recent"])

    cur.execute(f"SELECT COUNT(*) FROM products WHERE {where_sql}", params)
    total = cur.fetchone()[0] or 0

    offset = (page - 1) * limit
    cur.execute(
        f"""
        SELECT sku, store, name, category, image_url, product_url,
               current_price, old_price, discount_pct,
               lowest_price, highest_price, avg_price,
               is_real_deal, last_price_change_at, updated_at
        FROM products
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    )
    items = []
    for r in cur.fetchall():
        d = dict(r)
        d["current_price"] = float(d["current_price"] or 0)
        d["old_price"] = float(d["old_price"]) if d["old_price"] is not None else None
        d["discount_pct"] = float(d["discount_pct"] or 0)
        d["lowest_price"] = float(d["lowest_price"] or d["current_price"])
        d["highest_price"] = float(d["highest_price"] or d["current_price"])
        d["avg_price"] = float(d["avg_price"] or d["current_price"])
        d["affiliate_url"] = apply_affiliate(d["product_url"])
        d["updated_ago"] = format_time_ago(d["last_price_change_at"] or d["updated_at"])
        d["is_lowest_now"] = 1 if d["current_price"] <= d["lowest_price"] else 0
        items.append(d)

    conn.close()
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
        "items": items,
    }


@app.get("/api/history/{sku}")
def api_history(sku: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE sku = ?", (sku,))
    prod = cur.fetchone()
    if not prod:
        conn.close()
        return JSONResponse(status_code=404, content={"error": "المنتج غير موجود"})

    cur.execute(
        "SELECT price, recorded_at FROM price_history WHERE sku = ? ORDER BY recorded_at ASC",
        (sku,),
    )
    hist = [
        {
            "price": float(r["price"] or 0),
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["recorded_at"])),
        }
        for r in cur.fetchall()
    ]
    conn.close()
    prod_dict = dict(prod)
    prod_dict["affiliate_url"] = apply_affiliate(prod_dict["product_url"])
    return {"product": prod_dict, "history": hist}


# ===== Shared Layout & Pages =====
def render_layout(page_title: str, active_nav: str, body_html: str, extra_script: str = "") -> str:
    has_custom_logo = os.path.exists("logo.png")
    logo_visual = (
        '<img src="/logo.png" alt="تخفيضات مشطشطة" class="brand-logo-img" />'
        if has_custom_logo
        else """
        <div class="bahrain-chili-mark" aria-hidden="true">
            <svg viewBox="0 0 64 64" width="38" height="38">
                <defs>
                    <linearGradient id="bhRed" x1="0" y1="0" x2="1" y2="1">
                        <stop offset="0%" stop-color="#E11D38"/>
                        <stop offset="100%" stop-color="#A80B1E"/>
                    </linearGradient>
                </defs>
                <rect width="64" height="64" rx="16" fill="url(#bhRed)"/>
                <path d="M0 0 H20 L31 10.6 L20 21.3 L31 32 L20 42.6 L31 53.3 L20 64 H0 Z" fill="#FFFFFF"/>
                <path d="M43 17 C50 23, 52 36, 42 46 C38 50, 32 51, 30 49 C35 44, 39 35, 37 25 C36 21, 39 15, 43 17 Z" fill="#FFD166"/>
                <path d="M42 16 C44 12, 48 10, 52 11" stroke="#34D399" stroke-width="3" stroke-linecap="round" fill="none"/>
            </svg>
        </div>
        """
    )

    def nav_cls(key: str) -> str:
        return "nav-link active" if active_nav == key else "nav-link"

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl" data-theme="dark">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{page_title} | تخفيضات مشطشطة 🌶️</title>
    <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&display=swap" rel="stylesheet" />
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bh-red: #CE1126;
            --bh-red-bright: #EF233C;
            --bh-red-soft: rgba(206, 17, 38, 0.14);
            --bh-gold: #F59E0B;
            --bh-green: #10B981;
            --radius-xl: 22px;
            --radius-lg: 16px;
            --radius-md: 12px;
            --transition: all 0.22s ease;
        }}

        html[data-theme="dark"] {{
            --bg-main: #0B0D12;
            --bg-surface: #131722;
            --bg-card: #181D2B;
            --bg-card-hover: #1E2536;
            --bg-input: #0F131C;
            --border-color: rgba(255, 255, 255, 0.08);
            --border-strong: rgba(206, 17, 38, 0.38);
            --text-primary: #F8FAFC;
            --text-secondary: #94A3B8;
            --text-muted: #64748B;
            --shadow-card: 0 12px 30px rgba(0, 0, 0, 0.35);
            --header-bg: #11151F;
        }}

        html[data-theme="light"] {{
            --bg-main: #F6F7FB;
            --bg-surface: #FFFFFF;
            --bg-card: #FFFFFF;
            --bg-card-hover: #FFF8F8;
            --bg-input: #F1F3F9;
            --border-color: rgba(15, 23, 42, 0.09);
            --border-strong: rgba(206, 17, 38, 0.35);
            --text-primary: #0F172A;
            --text-secondary: #475569;
            --text-muted: #64748B;
            --shadow-card: 0 10px 26px rgba(15, 23, 42, 0.07);
            --header-bg: #FFFFFF;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Cairo', sans-serif;
        }}

        body {{
            background: var(--bg-main);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            transition: background 0.25s ease, color 0.25s ease;
        }}

        a {{
            color: inherit;
            text-decoration: none;
        }}

        .container {{
            width: 100%;
            max-width: 1340px;
            margin: 0 auto;
            padding: 0 20px;
        }}

        /* ===== Top Navbar (Non-sticky) ===== */
        .navbar {{
            position: relative;
            z-index: 50;
            background: var(--header-bg);
            border-bottom: 1px solid var(--border-color);
        }}

        .navbar-inner {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            min-height: 68px;
            flex-wrap: wrap;
            padding: 10px 0;
        }}

        .brand-link {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }}

        .brand-logo-img {{
            height: 40px;
            width: auto;
            border-radius: 10px;
            object-fit: contain;
        }}

        .bahrain-chili-mark {{
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 6px 18px rgba(206, 17, 38, 0.35);
            border-radius: 14px;
        }}

        .brand-text {{
            display: flex;
            flex-direction: column;
            line-height: 1.15;
        }}

        .brand-title {{
            font-size: 1.15rem;
            font-weight: 900;
            letter-spacing: -0.3px;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .brand-title span {{
            color: var(--bh-red-bright);
        }}

        .brand-sub {{
            font-size: 0.72rem;
            color: var(--text-secondary);
            font-weight: 600;
        }}

        .nav-search {{
            flex: 1;
            min-width: 220px;
            max-width: 390px;
            position: relative;
        }}

        .nav-search input {{
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 9px 40px 9px 14px;
            border-radius: 999px;
            font-size: 0.88rem;
            outline: none;
            transition: var(--transition);
        }}

        .nav-search input:focus {{
            border-color: var(--bh-red-bright);
            box-shadow: 0 0 0 3px var(--bh-red-soft);
        }}

        .nav-search button {{
            position: absolute;
            right: 12px;
            top: 50%;
            transform: translateY(-50%);
            background: none;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 1rem;
        }}

        .nav-links {{
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }}

        .nav-link {{
            padding: 7px 12px;
            border-radius: 10px;
            font-size: 0.88rem;
            font-weight: 700;
            color: var(--text-secondary);
            transition: var(--transition);
            display: inline-flex;
            align-items: center;
            gap: 6px;
            flex-shrink: 0;
        }}

        .nav-link:hover {{
            color: var(--text-primary);
            background: var(--bg-card);
        }}

        .nav-link.active {{
            color: #FFFFFF;
            background: var(--bh-red);
            box-shadow: 0 6px 16px rgba(206, 17, 38, 0.32);
        }}

        .live-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #EF4444;
            display: inline-block;
            box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.25);
        }}

        .nav-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
        }}

        .theme-btn {{
            width: 36px;
            height: 36px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            background: var(--bg-card);
            color: var(--text-primary);
            cursor: pointer;
            font-size: 1rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: var(--transition);
        }}

        .theme-btn:hover {{
            border-color: var(--bh-red-bright);
        }}

        .btn-outline {{
            padding: 7px 12px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            background: var(--bg-card);
            color: var(--text-primary);
            font-weight: 700;
            font-size: 0.84rem;
            transition: var(--transition);
            white-space: nowrap;
        }}

        .btn-outline:hover {{
            border-color: var(--bh-red-bright);
        }}

        .btn-primary {{
            padding: 7px 14px;
            border-radius: 10px;
            border: none;
            background: linear-gradient(135deg, #CE1126, #EF233C);
            color: #FFFFFF;
            font-weight: 800;
            font-size: 0.84rem;
            cursor: pointer;
            box-shadow: 0 6px 18px rgba(206, 17, 38, 0.32);
            transition: var(--transition);
            display: inline-flex;
            align-items: center;
            gap: 6px;
            white-space: nowrap;
        }}

        .btn-primary:hover {{
            transform: translateY(-1px);
            filter: brightness(1.06);
        }}

        /* ===== Main Content ===== */
        main {{
            flex: 1;
            padding: 22px 0 60px;
        }}

        /* ===== Hero Section ===== */
        .hero-box {{
            position: relative;
            overflow: hidden;
            border-radius: 26px;
            background: linear-gradient(135deg, rgba(206, 17, 38, 0.16) 0%, var(--bg-surface) 48%, rgba(206, 17, 38, 0.08) 100%);
            border: 1px solid var(--border-strong);
            padding: 34px 32px;
            margin-bottom: 34px;
            box-shadow: var(--shadow-card);
            display: grid;
            grid-template-columns: 1.45fr 1fr;
            gap: 28px;
            align-items: center;
        }}

        .hero-badge {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: var(--bh-red-soft);
            color: var(--bh-red-bright);
            border: 1px solid rgba(239, 35, 60, 0.35);
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 800;
            margin-bottom: 14px;
        }}

        .hero-title {{
            font-size: 2.15rem;
            font-weight: 900;
            line-height: 1.25;
            margin-bottom: 12px;
        }}

        .hero-title span {{
            color: var(--bh-red-bright);
        }}

        .hero-desc {{
            color: var(--text-secondary);
            font-size: 0.98rem;
            line-height: 1.7;
            margin-bottom: 22px;
            max-width: 620px;
        }}

        .hero-cta-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 24px;
        }}

        .hero-stats {{
            display: flex;
            flex-wrap: wrap;
            gap: 24px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
        }}

        .stat-item strong {{
            display: block;
            font-size: 1.35rem;
            font-weight: 900;
            color: var(--text-primary);
        }}

        .stat-item span {{
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}

        .hero-showcase {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}

        .showcase-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .tag-pill {{
            background: rgba(16, 185, 129, 0.14);
            color: var(--bh-green);
            padding: 4px 11px;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 800;
        }}

        .showcase-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-input);
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid var(--border-color);
        }}

        /* ===== Section Header ===== */
        .sec-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }}

        .sec-title {{
            font-size: 1.35rem;
            font-weight: 900;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .sec-sub {{
            font-size: 0.86rem;
            color: var(--text-secondary);
        }}

        /* ===== Stores & Categories Grid ===== */
        .stores-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(195px, 1fr));
            gap: 14px;
            margin-bottom: 36px;
        }}

        .store-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 18px 14px;
            text-align: center;
            transition: var(--transition);
            position: relative;
        }}

        .store-card:hover {{
            transform: translateY(-3px);
            border-color: var(--bh-red-bright);
            background: var(--bg-card-hover);
        }}

        .store-card.coming-soon {{
            opacity: 0.68;
        }}

        .store-icon {{
            width: 52px;
            height: 52px;
            border-radius: 15px;
            margin: 0 auto 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 900;
            font-size: 1.15rem;
        }}

        .store-name {{
            font-weight: 800;
            font-size: 1rem;
            margin-bottom: 4px;
        }}

        .store-meta {{
            font-size: 0.8rem;
            color: var(--bh-green);
            font-weight: 700;
        }}

        .cats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 14px;
            margin-bottom: 36px;
        }}

        .cat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 16px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            transition: var(--transition);
        }}

        .cat-card:hover {{
            border-color: var(--bh-red-bright);
            transform: translateY(-2px);
        }}

        .cat-badge-icon {{
            width: 44px;
            height: 44px;
            border-radius: 12px;
            background: var(--bh-red-soft);
            color: var(--bh-red-bright);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
            flex-shrink: 0;
        }}

        /* ===== Products Grid ===== */
        .products-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 18px;
        }}

        .deal-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 18px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: var(--transition);
            box-shadow: var(--shadow-card);
        }}

        .deal-card:hover {{
            transform: translateY(-4px);
            border-color: var(--bh-red-bright);
        }}

        .deal-thumb {{
            position: relative;
            height: 205px;
            background: #FFFFFF;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 14px;
        }}

        .deal-thumb img {{
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }}

        .badges-top-right {{
            position: absolute;
            top: 10px;
            right: 10px;
            display: flex;
            flex-direction: column;
            gap: 5px;
        }}

        .badge-disc {{
            background: var(--bh-red);
            color: #FFFFFF;
            font-weight: 900;
            font-size: 0.78rem;
            padding: 3px 9px;
            border-radius: 8px;
        }}

        .badge-real {{
            background: #10B981;
            color: #FFFFFF;
            font-weight: 800;
            font-size: 0.74rem;
            padding: 3px 9px;
            border-radius: 8px;
        }}

        .badge-store {{
            position: absolute;
            bottom: 10px;
            left: 10px;
            background: rgba(15, 23, 42, 0.82);
            color: #FDE047;
            font-size: 0.72rem;
            font-weight: 800;
            padding: 3px 9px;
            border-radius: 8px;
        }}

        .deal-body {{
            padding: 15px;
            display: flex;
            flex-direction: column;
            flex: 1;
            gap: 10px;
        }}

        .deal-cat {{
            font-size: 0.76rem;
            color: var(--text-secondary);
            font-weight: 700;
        }}

        .deal-title {{
            font-size: 0.93rem;
            font-weight: 700;
            line-height: 1.45;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            min-height: 2.7em;
        }}

        .price-row {{
            display: flex;
            align-items: baseline;
            gap: 8px;
            margin-top: auto;
        }}

        .price-now {{
            font-size: 1.25rem;
            font-weight: 900;
            color: var(--bh-red-bright);
        }}

        .price-old {{
            font-size: 0.85rem;
            color: var(--text-muted);
            text-decoration: line-through;
        }}

        .history-mini {{
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            color: var(--text-secondary);
            background: var(--bg-input);
            padding: 7px 10px;
            border-radius: 10px;
        }}

        .card-actions {{
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 8px;
        }}

        .btn-buy {{
            background: var(--bh-red);
            color: #FFFFFF;
            text-align: center;
            padding: 9px 12px;
            border-radius: 10px;
            font-weight: 800;
            font-size: 0.84rem;
            transition: var(--transition);
        }}

        .btn-buy:hover {{
            background: var(--bh-red-bright);
        }}

        .btn-hist {{
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 9px 12px;
            border-radius: 10px;
            font-weight: 700;
            font-size: 0.82rem;
            cursor: pointer;
            transition: var(--transition);
        }}

        .btn-hist:hover {{
            border-color: var(--bh-red-bright);
        }}

        /* ===== Filter Bar ===== */
        .filter-bar {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 16px;
            margin-bottom: 22px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
        }}

        .filter-bar select, .filter-bar input {{
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 9px 14px;
            border-radius: 11px;
            font-size: 0.88rem;
            outline: none;
        }}

        .chip-toggle {{
            padding: 9px 14px;
            border-radius: 11px;
            border: 1px solid var(--border-color);
            background: var(--bg-input);
            color: var(--text-secondary);
            font-weight: 700;
            font-size: 0.86rem;
            cursor: pointer;
        }}

        .chip-toggle.active {{
            background: rgba(16, 185, 129, 0.16);
            border-color: #10B981;
            color: #10B981;
        }}

        /* ===== Coupons ===== */
        .coupon-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-top: 4px solid var(--bh-red);
            border-radius: 18px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}

        .coupon-code-box {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: var(--bh-red-soft);
            border: 1.5px dashed var(--bh-red-bright);
            padding: 11px 16px;
            border-radius: 12px;
            cursor: pointer;
            font-weight: 900;
            font-size: 1.1rem;
            color: var(--bh-red-bright);
        }}

        /* ===== Auth Card ===== */
        .auth-wrap {{
            max-width: 440px;
            margin: 24px auto;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 22px;
            padding: 30px;
            box-shadow: var(--shadow-card);
        }}

        .form-group {{
            margin-bottom: 14px;
        }}

        .form-group label {{
            display: block;
            font-size: 0.85rem;
            font-weight: 700;
            margin-bottom: 6px;
            color: var(--text-secondary);
        }}

        .form-group input {{
            width: 100%;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 11px 14px;
            border-radius: 12px;
            font-size: 0.92rem;
        }}

        /* ===== Modal ===== */
        .modal-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.72);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 999;
            padding: 16px;
        }}

        .modal-backdrop.open {{
            display: flex;
        }}

        .modal-card {{
            background: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 22px;
            width: 100%;
            max-width: 680px;
            padding: 24px;
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5);
        }}

        /* ===== Floating Telegram Button ===== */
        .float-tg {{
            position: fixed;
            bottom: 20px;
            left: 20px;
            z-index: 90;
            background: linear-gradient(135deg, #0088cc, #229ED9);
            color: #FFFFFF;
            padding: 11px 16px;
            border-radius: 999px;
            font-weight: 800;
            font-size: 0.85rem;
            box-shadow: 0 10px 25px rgba(0, 136, 204, 0.38);
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: var(--transition);
        }}

        .float-tg:hover {{
            transform: translateY(-2px);
        }}

        /* ===== Footer ===== */
        footer {{
            background: var(--bg-surface);
            border-top: 1px solid var(--border-color);
            padding: 36px 0 24px;
            margin-top: auto;
        }}

        .footer-grid {{
            display: grid;
            grid-template-columns: 1.4fr 1fr 1fr 1.3fr;
            gap: 24px;
            margin-bottom: 24px;
        }}

        .footer-col h4 {{
            font-size: 1rem;
            font-weight: 800;
            margin-bottom: 12px;
            color: var(--text-primary);
        }}

        .footer-col ul {{
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 8px;
            font-size: 0.86rem;
            color: var(--text-secondary);
        }}

        .footer-col ul a:hover {{
            color: var(--bh-red-bright);
        }}

        .footer-bottom {{
            border-top: 1px solid var(--border-color);
            padding-top: 16px;
            text-align: center;
            font-size: 0.8rem;
            color: var(--text-muted);
        }}

        @media (max-width: 920px) {{
            .hero-box {{
                grid-template-columns: 1fr;
            }}
            .footer-grid {{
                grid-template-columns: 1fr 1fr;
            }}
        }}

        /* Slim, non-sticky mobile navbar with horizontal scrollable nav tabs */
        @media (max-width: 600px) {{
            .container {{
                padding: 0 12px;
            }}
            .navbar-inner {{
                min-height: auto;
                gap: 8px;
                padding: 8px 0;
            }}
            .brand-logo-img {{
                height: 30px;
            }}
            .brand-title {{
                font-size: 0.95rem;
            }}
            .brand-sub {{
                display: none;
            }}
            .nav-actions {{
                gap: 5px;
            }}
            .theme-btn {{
                width: 30px;
                height: 30px;
                font-size: 0.85rem;
                border-radius: 8px;
            }}
            .btn-outline, .btn-primary {{
                padding: 5px 9px;
                font-size: 0.75rem;
                border-radius: 8px;
            }}
            .nav-search {{
                order: 3;
                flex: 0 0 100%;
                max-width: 100%;
                min-width: 0;
            }}
            .nav-search input {{
                padding: 7px 34px 7px 12px;
                font-size: 0.82rem;
            }}
            .nav-links {{
                order: 4;
                flex: 0 0 100%;
                flex-wrap: nowrap;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                scrollbar-width: none;
                gap: 6px;
                padding-bottom: 2px;
            }}
            .nav-links::-webkit-scrollbar {{
                display: none;
            }}
            .nav-link {{
                padding: 5px 10px;
                font-size: 0.8rem;
                white-space: nowrap;
            }}
            .footer-grid {{
                grid-template-columns: 1fr;
            }}
            .hero-title {{
                font-size: 1.65rem;
            }}
        }}
    </style>
</head>
<body>
    <header class="navbar">
        <div class="container navbar-inner">
            <a href="/" class="brand-link" title="العودة للصفحة الرئيسية">
                {logo_visual}
                <div class="brand-text">
                    <div class="brand-title">تخفيضات <span>مشطشطة</span> 🌶️</div>
                    <div class="brand-sub">راصد عروض وتاريخ أسعار البحرين (BHD)</div>
                </div>
            </a>

            <div class="nav-actions">
                <button type="button" class="theme-btn" id="themeToggleBtn" title="تبديل الوضع الليلي / النهاري">☀️</button>
                <a href="/login" class="btn-outline">دخول</a>
                <a href="/register" class="btn-primary">حساب جديد</a>
            </div>

            <form class="nav-search" action="/deals" method="get">
                <input type="text" name="q" id="globalSearchInput" placeholder="ابحث عن منتج في البحرين (آيفون، لابتوب، عطور...)" />
                <button type="submit" aria-label="بحث">🔍</button>
            </form>

            <nav class="nav-links" aria-label="التنقل الرئيسي">
                <a href="/" class="{nav_cls('home')}">الرئيسية</a>
                <a href="/deals" class="{nav_cls('deals')}"><span class="live-dot"></span> دلوقتي</a>
                <a href="/categories" class="{nav_cls('categories')}">الأقسام</a>
                <a href="/coupons" class="{nav_cls('coupons')}">🎟️ كوبونات</a>
                <a href="/stores" class="{nav_cls('stores')}">المتاجر</a>
            </nav>
        </div>
    </header>

    <main class="container">
        {body_html}
    </main>

    <a href="{TELEGRAM_CHANNEL_URL}" target="_blank" rel="noopener" class="float-tg">
        ✈️ قناة اللقطات على تيليجرام
    </a>

    <!-- Price History Modal -->
    <div class="modal-backdrop" id="historyModal" onclick="if(event.target===this) closeHistoryModal()">
        <div class="modal-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                <h3 id="modalTitle" style="font-size:1.05rem; font-weight:800; line-height:1.4;">تاريخ سعر المنتج</h3>
                <button onclick="closeHistoryModal()" style="background:none; border:none; color:var(--text-secondary); font-size:1.4rem; cursor:pointer;">✕</button>
            </div>
            <div id="modalStats" style="display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:16px;"></div>
            <div style="height:260px;">
                <canvas id="priceChartCanvas"></canvas>
            </div>
            <div style="margin-top:16px; display:flex; justify-content:flex-end; gap:10px;">
                <button onclick="closeHistoryModal()" class="btn-outline">إغلاق</button>
                <a id="modalBuyLink" href="#" target="_blank" rel="noopener" class="btn-primary">الذهاب للمتجر ↗</a>
            </div>
        </div>
    </div>

    <footer>
        <div class="container">
            <div class="footer-grid">
                <div class="footer-col">
                    <div class="brand-link" style="margin-bottom:10px;">
                        {logo_visual}
                        <div class="brand-text">
                            <div class="brand-title">تخفيضات <span>مشطشطة</span> 🌶️</div>
                            <div class="brand-sub">عروض البحرين لحظة بلحظة</div>
                        </div>
                    </div>
                    <p style="font-size:0.85rem; color:var(--text-secondary); line-height:1.7;">
                        منصتك البحرينية الذكية لمتابعة الأسعار وكشف اللقطات الحقيقية من متاجر البحرين بالدينار البحريني (BHD / د.ب) مع سجل كامل لتاريخ كل منتج.
                    </p>
                </div>

                <div class="footer-col">
                    <h4>أشهر المتاجر</h4>
                    <ul>
                        <li><a href="/deals?store=noon_bh">🟡 نون البحرين (نشط)</a></li>
                        <li><a href="/stores">🔵 إكسترا البحرين (قريباً)</a></li>
                        <li><a href="/stores">🔴 شرف DG البحرين (قريباً)</a></li>
                        <li><a href="/stores">⚫ أشرف البحرين (قريباً)</a></li>
                        <li><a href="/stores">🟠 طلبات البحرين (قريباً)</a></li>
                    </ul>
                </div>

                <div class="footer-col">
                    <h4>روابط سريعة</h4>
                    <ul>
                        <li><a href="/deals?real_only=1">🔥 اللقطات الحقيقية فقط</a></li>
                        <li><a href="/deals?sort=discount">📉 أعلى نسبة خصم</a></li>
                        <li><a href="/categories">📂 تصفح حسب الأقسام</a></li>
                        <li><a href="/coupons">🎟️ كوبونات الخصم</a></li>
                        <li><a href="{TELEGRAM_CHANNEL_URL}" target="_blank">✈️ قناة التيليجرام</a></li>
                    </ul>
                </div>

                <div class="footer-col">
                    <h4>🛡️ إخلاء المسؤولية</h4>
                    <p style="font-size:0.82rem; color:var(--text-secondary); line-height:1.7;">
                        الأسعار وحالة التوفر تتحدث دورياً، والسعر النهائي هو الظاهر داخل صفحة المتجر وقت الشراء. بعض الروابط قد تكون روابط تسويق بالعمولة دون أي تكلفة إضافية عليك.
                    </p>
                </div>
            </div>
            <div class="footer-bottom">
                © 2026 تخفيضات مشطشطة 🌶️ — جميع الحقوق محفوظة | مملكة البحرين (BHD)
            </div>
        </div>
    </footer>

    <script>
        (function() {{
            const saved = localStorage.getItem('mshatshata_theme') || 'dark';
            document.documentElement.setAttribute('data-theme', saved);
            const btn = document.getElementById('themeToggleBtn');
            if (btn) btn.textContent = saved === 'dark' ? '☀️' : '🌙';
        }})();

        document.getElementById('themeToggleBtn').addEventListener('click', function() {{
            const current = document.documentElement.getAttribute('data-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('mshatshata_theme', next);
            this.textContent = next === 'dark' ? '☀️' : '🌙';
        }});

        function escapeHtml(str) {{
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }}

        function renderProductCard(p) {{
            const img = p.image_url || 'https://via.placeholder.com/300x300?text=No+Image';
            const oldHtml = p.old_price ? `<span class="price-old">${{Number(p.old_price).toFixed(2)}} د.ب</span>` : '';
            const discHtml = p.discount_pct > 0 ? `<span class="badge-disc">-${{Math.round(p.discount_pct)}}%</span>` : '';
            const realHtml = p.is_real_deal ? `<span class="badge-real">🔥 لقطة حقيقية</span>` : (p.is_lowest_now ? `<span class="badge-real">📉 أقل سعر</span>` : '');
            return `
                <article class="deal-card">
                    <div class="deal-thumb">
                        <div class="badges-top-right">${{discHtml}}${{realHtml}}</div>
                        <span class="badge-store">نون البحرين</span>
                        <img src="${{escapeHtml(img)}}" alt="${{escapeHtml(p.name)}}" loading="lazy" />
                    </div>
                    <div class="deal-body">
                        <div class="deal-cat">${{escapeHtml(p.category || 'عام')}} • ${{escapeHtml(p.updated_ago || '')}}</div>
                        <h3 class="deal-title" title="${{escapeHtml(p.name)}}">${{escapeHtml(p.name)}}</h3>
                        <div class="price-row">
                            <span class="price-now">${{Number(p.current_price).toFixed(2)}} BHD</span>
                            ${{oldHtml}}
                        </div>
                        <div class="history-mini">
                            <span>الأقل: <b>${{Number(p.lowest_price).toFixed(2)}} د.ب</b></span>
                            <span>المتوسط: <b>${{Number(p.avg_price).toFixed(2)}} د.ب</b></span>
                        </div>
                        <div class="card-actions">
                            <a href="${{escapeHtml(p.affiliate_url)}}" target="_blank" rel="noopener" class="btn-buy">اذهب للعرض ↗</a>
                            <button type="button" class="btn-hist" onclick="openHistoryModal('${{escapeHtml(p.sku)}}')" title="رسم تاريخ السعر">📊 السجل</button>
                        </div>
                    </div>
                </article>
            `;
        }}

        let chartInstance = null;
        async function openHistoryModal(sku) {{
            const modal = document.getElementById('historyModal');
            modal.classList.add('open');
            document.getElementById('modalTitle').textContent = 'جاري تحميل سجل السعر...';
            try {{
                const res = await fetch('/api/history/' + encodeURIComponent(sku));
                const data = await res.json();
                if (!res.ok) return;
                const p = data.product;
                document.getElementById('modalTitle').textContent = p.name;
                document.getElementById('modalBuyLink').href = p.affiliate_url;
                document.getElementById('modalStats').innerHTML = `
                    <div style="background:var(--bg-input); padding:10px; border-radius:12px; text-align:center;">
                        <div style="font-size:0.75rem; color:var(--text-secondary);">السعر الحالي</div>
                        <strong style="color:var(--bh-red-bright); font-size:1.05rem;">${{Number(p.current_price).toFixed(2)}} BHD</strong>
                    </div>
                    <div style="background:var(--bg-input); padding:10px; border-radius:12px; text-align:center;">
                        <div style="font-size:0.75rem; color:var(--text-secondary);">أقل سعر مسجل</div>
                        <strong style="color:var(--bh-green); font-size:1.05rem;">${{Number(p.lowest_price).toFixed(2)}} BHD</strong>
                    </div>
                    <div style="background:var(--bg-input); padding:10px; border-radius:12px; text-align:center;">
                        <div style="font-size:0.75rem; color:var(--text-secondary);">متوسط السعر</div>
                        <strong style="font-size:1.05rem;">${{Number(p.avg_price).toFixed(2)}} BHD</strong>
                    </div>
                `;
                const labels = data.history.map(h => h.date);
                const prices = data.history.map(h => h.price);
                const ctx = document.getElementById('priceChartCanvas').getContext('2d');
                if (chartInstance) chartInstance.destroy();
                chartInstance = new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels,
                        datasets: [{{
                            label: 'السعر (BHD)',
                            data: prices,
                            borderColor: '#EF233C',
                            backgroundColor: 'rgba(239, 35, 60, 0.16)',
                            fill: true,
                            tension: 0.3,
                            pointRadius: 4
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{ legend: {{ display: false }} }}
                    }}
                }});
            }} catch (e) {{
                console.error(e);
            }}
        }}

        function closeHistoryModal() {{
            document.getElementById('historyModal').classList.remove('open');
        }}
    </script>
    {extra_script}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def page_home():
    body = f"""
    <section class="hero-box">
        <div>
            <div class="hero-badge">🇧🇭 راصد أسعار البحرين الذكي • تحديث مستمر</div>
            <h1 class="hero-title">كل تخفيض مشطشط في البحرين... <span>أول ما يحصل 🌶️</span></h1>
            <p class="hero-desc">
                بنراقب أسعار آلاف المنتجات في متاجر البحرين بالدينار البحريني (BHD / د.ب)، وبنسجل تاريخ سعر كل منتج عشان تعرف الفرق بين الخصم الوهمي واللقطة الحقيقية قبل ما تشتري.
            </p>
            <div class="hero-cta-group">
                <a href="/deals?real_only=1" class="btn-primary">🔥 شوف اللقطات الحقيقية</a>
                <a href="/deals" class="btn-outline">⚡ كل العروض المباشرة</a>
                <a href="/coupons" class="btn-outline">🎟️ كوبونات الخصم</a>
                <a href="{TELEGRAM_CHANNEL_URL}" target="_blank" rel="noopener" class="btn-outline">✈️ انضم لقناة التيليجرام</a>
            </div>
            <div class="hero-stats">
                <div class="stat-item"><strong id="statTotal">--</strong><span>منتج مرصود في البحرين</span></div>
                <div class="stat-item"><strong id="statReal" style="color:var(--bh-green);">--</strong><span>لقطة حقيقية مؤكدة</span></div>
                <div class="stat-item"><strong id="statMaxDisc" style="color:var(--bh-red-bright);">--%</strong><span>أعلى خصم مرصود</span></div>
                <div class="stat-item"><strong id="statUpdated" style="font-size:1rem;">--</strong><span>آخر تحديث للبيانات</span></div>
            </div>
        </div>

        <div class="hero-showcase">
            <div class="showcase-top">
                <span style="font-weight:800; font-size:0.95rem;">📊 كيف نكشف الخصم الحقيقي؟</span>
                <span class="tag-pill">تحليل تلقائي 24/7</span>
            </div>
            <div class="showcase-row">
                <div>
                    <div style="font-weight:800; font-size:0.9rem;">1. مقارنة مع أقل سعر تاريخي</div>
                    <div style="font-size:0.78rem; color:var(--text-secondary);">نراجع سجل السعر عبر الجولات السابقة</div>
                </div>
                <span style="color:var(--bh-green); font-weight:900;">✓ موثق</span>
            </div>
            <div class="showcase-row">
                <div>
                    <div style="font-weight:800; font-size:0.9rem;">2. كشف رفع السعر قبل الخصم</div>
                    <div style="font-size:0.78rem; color:var(--text-secondary);">نقارن السعر الحالي بمتوسط السعر الحقيقي</div>
                </div>
                <span style="color:var(--bh-red-bright); font-weight:900;">🌶️ فلترة ذكية</span>
            </div>
            <div class="showcase-row">
                <div>
                    <div style="font-weight:800; font-size:0.9rem;">3. تنبيهات فورية على تيليجرام</div>
                    <div style="font-size:0.78rem; color:var(--text-secondary);">أول ما السعر يهبط يوصلك الإشعار بالصورة</div>
                </div>
                <a href="{TELEGRAM_CHANNEL_URL}" target="_blank" style="color:#38BDF8; font-weight:800; font-size:0.84rem;">اشترك ↗</a>
            </div>
        </div>
    </section>

    <section>
        <div class="sec-head">
            <div>
                <h2 class="sec-title">🏬 تسوّق حسب المتاجر في البحرين</h2>
                <div class="sec-sub">بدأنا برصد نون البحرين بالكامل، وقريباً باقي متاجر البحرين</div>
            </div>
            <a href="/stores" class="btn-outline">كل المتاجر ←</a>
        </div>
        <div class="stores-grid">
            <a href="/deals?store=noon_bh" class="store-card" style="border-color: rgba(250, 204, 21, 0.45);">
                <div class="store-icon" style="background:#FACC15; color:#111827;">noon</div>
                <div class="store-name">نون البحرين</div>
                <div class="store-meta" id="noonStoreMeta">نشط الآن • BHD</div>
            </a>
            <a href="/stores" class="store-card coming-soon">
                <div class="store-icon" style="background:#1D4ED8; color:#FFFFFF;">eXtra</div>
                <div class="store-name">إكسترا البحرين</div>
                <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
            </a>
            <a href="/stores" class="store-card coming-soon">
                <div class="store-icon" style="background:#DC2626; color:#FFFFFF;">DG</div>
                <div class="store-name">شرف دي جي</div>
                <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
            </a>
            <a href="/stores" class="store-card coming-soon">
                <div class="store-icon" style="background:#111827; color:#FFFFFF;">A</div>
                <div class="store-name">أشرف البحرين</div>
                <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
            </a>
            <a href="/stores" class="store-card coming-soon">
                <div class="store-icon" style="background:#EA580C; color:#FFFFFF;">t</div>
                <div class="store-name">طلبات البحرين</div>
                <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
            </a>
        </div>
    </section>

    <section>
        <div class="sec-head">
            <div>
                <h2 class="sec-title">📂 تسوّق حسب القسم</h2>
                <div class="sec-sub">اختر القسم وشوف أقوى التخفيضات المرصودة فيه</div>
            </div>
            <a href="/categories" class="btn-outline">كل الأقسام ←</a>
        </div>
        <div class="cats-grid" id="homeCatsGrid"></div>
    </section>

    <section style="margin-bottom: 38px;">
        <div class="sec-head">
            <div>
                <h2 class="sec-title"><span class="live-dot"></span> اللي نزل دلوقتي — لقطات البحرين</h2>
                <div class="sec-sub">أحدث المنتجات اللي نزلت أسعارها أو عليها خصومات قوية</div>
            </div>
            <a href="/deals" class="btn-primary">شوف الكل ←</a>
        </div>
        <div class="products-grid" id="homeDealsGrid"></div>
    </section>
    """

    script = """
    <script>
        async function loadHome() {
            try {
                const sumRes = await fetch('/api/summary');
                const sum = await sumRes.json();
                document.getElementById('statTotal').textContent = sum.total_active.toLocaleString('ar-BH');
                document.getElementById('statReal').textContent = sum.real_deals.toLocaleString('ar-BH');
                document.getElementById('statMaxDisc').textContent = sum.max_discount + '%';
                document.getElementById('statUpdated').textContent = sum.last_updated_text;
                document.getElementById('noonStoreMeta').textContent = `${sum.total_active} منتج • خصم حتى ${sum.max_discount}%`;

                const catsGrid = document.getElementById('homeCatsGrid');
                catsGrid.innerHTML = sum.categories.slice(0, 8).map(c => `
                    <a href="/deals?category=${encodeURIComponent(c.name)}" class="cat-card">
                        <div style="display:flex; align-items:center; gap:12px;">
                            <div class="cat-badge-icon">🌶️</div>
                            <div>
                                <div style="font-weight:800; font-size:0.95rem;">${escapeHtml(c.name)}</div>
                                <div style="font-size:0.78rem; color:var(--text-secondary);">${c.count} منتج • خصم لحد ${c.max_discount}%</div>
                            </div>
                        </div>
                        <span style="color:var(--text-muted);">←</span>
                    </a>
                `).join('');

                const prodRes = await fetch('/api/products?sort=real&limit=12');
                const prodData = await prodRes.json();
                const grid = document.getElementById('homeDealsGrid');
                if (!prodData.items.length) {
                    grid.innerHTML = '<p style="color:var(--text-secondary);">لا توجد منتجات مسجلة بعد، شغّل جامع البيانات لبدء الرصد.</p>';
                } else {
                    grid.innerHTML = prodData.items.map(renderProductCard).join('');
                }
            } catch (e) {
                console.error(e);
            }
        }
        loadHome();
    </script>
    """
    return render_layout("الرئيسية", "home", body, script)


@app.get("/deals", response_class=HTMLResponse)
def page_deals():
    body = """
    <div class="sec-head">
        <div>
            <h1 class="sec-title"><span class="live-dot"></span> العروض المباشرة في البحرين</h1>
            <div class="sec-sub">ابحث وفلتر حسب القسم ونسبة الخصم واللقطات الحقيقية</div>
        </div>
        <div id="dealsCountBadge" class="tag-pill">جاري التحميل...</div>
    </div>

    <div class="filter-bar">
        <input type="text" id="filterQ" placeholder="ابحث بالاسم..." style="flex:1; min-width:200px;" />
        <select id="filterCat">
            <option value="">كل الأقسام</option>
        </select>
        <select id="filterSort">
            <option value="recent">الأحدث نزولاً</option>
            <option value="real">اللقطات الحقيقية أولاً</option>
            <option value="discount">أعلى نسبة خصم</option>
            <option value="price_asc">السعر: من الأقل للأعلى</option>
            <option value="price_desc">السعر: من الأعلى للأقل</option>
        </select>
        <button type="button" id="toggleRealBtn" class="chip-toggle">🔥 لقطات حقيقية فقط</button>
        <button type="button" id="applyBtn" class="btn-primary">تطبيق</button>
    </div>

    <div class="products-grid" id="dealsGrid"></div>
    <div id="paginationBox" style="display:flex; justify-content:center; gap:10px; margin-top:28px;"></div>
    """

    script = """
    <script>
        const urlParams = new URLSearchParams(window.location.search);
        let realOnly = urlParams.get('real_only') === '1' ? 1 : 0;
        const activeStore = urlParams.get('store') || '';
        let currentPage = 1;

        document.getElementById('filterQ').value = urlParams.get('q') || '';
        document.getElementById('filterSort').value = urlParams.get('sort') || 'recent';
        if (realOnly) document.getElementById('toggleRealBtn').classList.add('active');

        document.getElementById('toggleRealBtn').addEventListener('click', function() {
            realOnly = realOnly ? 0 : 1;
            this.classList.toggle('active', realOnly === 1);
            applyFilters(1);
        });

        document.getElementById('filterSort').addEventListener('change', function() {
            applyFilters(1);
        });

        document.getElementById('filterCat').addEventListener('change', function() {
            applyFilters(1);
        });

        document.getElementById('applyBtn').addEventListener('click', function() {
            applyFilters(1);
        });

        document.getElementById('filterQ').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                applyFilters(1);
            }
        });

        async function initCategoriesFilter() {
            const res = await fetch('/api/summary');
            const data = await res.json();
            const sel = document.getElementById('filterCat');
            const targetCat = urlParams.get('category') || '';
            data.categories.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c.name;
                opt.textContent = `${c.name} (${c.count})`;
                if (c.name === targetCat) opt.selected = true;
                sel.appendChild(opt);
            });
        }

        async function applyFilters(page = 1) {
            currentPage = page;
            const q = document.getElementById('filterQ').value;
            const cat = document.getElementById('filterCat').value;
            const sort = document.getElementById('filterSort').value;

            const qs = new URLSearchParams({
                q, category: cat, store: activeStore, sort, real_only: realOnly, page, limit: 24
            });
            const res = await fetch('/api/products?' + qs.toString());
            const data = await res.json();

            document.getElementById('dealsCountBadge').textContent = `${data.total} عرض متاح`;
            const grid = document.getElementById('dealsGrid');
            if (!data.items.length) {
                grid.innerHTML = '<p style="color:var(--text-secondary);">لا توجد نتائج مطابقة لبحثك حالياً.</p>';
            } else {
                grid.innerHTML = data.items.map(renderProductCard).join('');
            }

            const pag = document.getElementById('paginationBox');
            pag.innerHTML = '';
            if (data.pages > 1) {
                if (page > 1) {
                    pag.innerHTML += `<button class="btn-outline" onclick="applyFilters(${page - 1})">السابق</button>`;
                }
                pag.innerHTML += `<span style="padding:8px 14px; font-weight:700;">صفحة ${page} من ${data.pages}</span>`;
                if (page < data.pages) {
                    pag.innerHTML += `<button class="btn-outline" onclick="applyFilters(${page + 1})">التالي</button>`;
                }
            }
        }

        initCategoriesFilter().then(() => applyFilters(1));
    </script>
    """
    return render_layout("العروض المباشرة", "deals", body, script)


@app.get("/categories", response_class=HTMLResponse)
def page_categories():
    body = """
    <div class="sec-head">
        <div>
            <h1 class="sec-title">📂 تسوّق حسب القسم</h1>
            <div class="sec-sub">اختر القسم واستعرض كل العروض وتاريخ الأسعار فيه</div>
        </div>
    </div>
    <div class="cats-grid" id="allCatsGrid"></div>
    """
    script = """
    <script>
        fetch('/api/summary').then(r => r.json()).then(data => {
            const grid = document.getElementById('allCatsGrid');
            grid.innerHTML = data.categories.map(c => `
                <a href="/deals?category=${encodeURIComponent(c.name)}" class="cat-card">
                    <div style="display:flex; align-items:center; gap:14px;">
                        <div class="cat-badge-icon">🌶️</div>
                        <div>
                            <div style="font-weight:800; font-size:1rem;">${escapeHtml(c.name)}</div>
                            <div style="font-size:0.8rem; color:var(--text-secondary);">${c.count} منتج مرصود • خصم حتى ${c.max_discount}%</div>
                        </div>
                    </div>
                    <span style="font-weight:900; color:var(--bh-red-bright);">←</span>
                </a>
            `).join('');
        });
    </script>
    """
    return render_layout("الأقسام", "categories", body, script)


@app.get("/stores", response_class=HTMLResponse)
def page_stores():
    body = """
    <div class="sec-head">
        <div>
            <h1 class="sec-title">🏬 المتاجر المرصودة في مملكة البحرين 🇧🇭</h1>
            <div class="sec-sub">المتاجر التي نراقب أسعارها ونكشف تخفيضاتها الحقيقية</div>
        </div>
    </div>
    <div class="stores-grid">
        <a href="/deals?store=noon_bh" class="store-card" style="border-color: rgba(250, 204, 21, 0.55);">
            <div class="store-icon" style="background:#FACC15; color:#111827;">noon</div>
            <div class="store-name">نون البحرين</div>
            <div style="font-size:0.76rem; color:var(--text-secondary); margin-bottom:6px;">BH البحرين • عملة BHD</div>
            <div class="store-meta" id="storeNoonStats">نشط الآن</div>
        </a>
        <div class="store-card coming-soon">
            <div class="store-icon" style="background:#1D4ED8; color:#FFFFFF;">eXtra</div>
            <div class="store-name">إكسترا البحرين</div>
            <div style="font-size:0.76rem; color:var(--text-secondary); margin-bottom:6px;">BH البحرين</div>
            <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
        </div>
        <div class="store-card coming-soon">
            <div class="store-icon" style="background:#DC2626; color:#FFFFFF;">DG</div>
            <div class="store-name">شرف دي جي البحرين</div>
            <div style="font-size:0.76rem; color:var(--text-secondary); margin-bottom:6px;">BH البحرين</div>
            <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
        </div>
        <div class="store-card coming-soon">
            <div class="store-icon" style="background:#111827; color:#FFFFFF;">A</div>
            <div class="store-name">أشرف سوني البحرين</div>
            <div style="font-size:0.76rem; color:var(--text-secondary); margin-bottom:6px;">BH البحرين</div>
            <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
        </div>
        <div class="store-card coming-soon">
            <div class="store-icon" style="background:#EA580C; color:#FFFFFF;">t</div>
            <div class="store-name">طلبات البحرين</div>
            <div style="font-size:0.76rem; color:var(--text-secondary); margin-bottom:6px;">BH البحرين</div>
            <div class="store-meta" style="color:var(--text-muted);">قريباً</div>
        </div>
    </div>
    """
    script = """
    <script>
        fetch('/api/summary').then(r => r.json()).then(d => {
            document.getElementById('storeNoonStats').textContent = `${d.total_active} منتج • خصومات لحد ${d.max_discount}%`;
        });
    </script>
    """
    return render_layout("المتاجر", "stores", body, script)


@app.get("/coupons", response_class=HTMLResponse)
def page_coupons():
    body = f"""
    <section class="hero-box" style="grid-template-columns:1fr; padding:26px 28px;">
        <div>
            <div class="hero-badge">🎟️ كوبونات حصرية للبحرين</div>
            <h1 class="hero-title" style="font-size:1.75rem;">كوبونات الخصم الإضافية</h1>
            <p class="hero-desc" style="margin-bottom:0;">
                دوس على أي كود خصم هيتنسخ على طول، والصقه في خانة الكوبون وقت الدفع في المتجر.
            </p>
        </div>
    </section>

    <div class="products-grid" style="margin-bottom:28px;">
        <div class="coupon-card">
            <div style="display:flex; align-items:center; justify-content:space-between;">
                <div style="display:flex; align-items:center; gap:10px;">
                    <div style="width:42px; height:42px; border-radius:12px; background:#FACC15; color:#111; font-weight:900; display:flex; align-items:center; justify-content:center;">noon</div>
                    <div>
                        <div style="font-weight:800;">كوبون خصم نون البحرين</div>
                        <div style="font-size:0.78rem; color:var(--text-secondary);">كود تجريبي قابل للتحديث</div>
                    </div>
                </div>
                <span class="tag-pill">فعّال</span>
            </div>
            <p style="font-size:0.86rem; color:var(--text-secondary);">
                خصم إضافي على مشترياتك من نون البحرين (مثال تجريبي: M22).
            </p>
            <div class="coupon-code-box" onclick="copyCoupon('M22', this)">
                <span>M22</span>
                <span style="font-size:0.82rem;">📋 اضغط للنسخ</span>
            </div>
            <a href="https://www.noon.com/bahrain-ar/" target="_blank" rel="noopener" class="btn-outline" style="text-align:center;">الذهاب لموقع نون البحرين ↗</a>
        </div>
    </div>

    <div style="background:linear-gradient(135deg, #0088cc, #0284c7); color:#fff; border-radius:20px; padding:24px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px;">
        <div>
            <h3 style="font-size:1.2rem; font-weight:900; margin-bottom:4px;">✈️ انضم لقناة التيليجرام — أهم حاجة!</h3>
            <p style="font-size:0.9rem; opacity:0.92;">أي تخفيض مشطشط أو كوبون جديد بينزل على القناة الأول قبل ما يخلص.</p>
        </div>
        <a href="{TELEGRAM_CHANNEL_URL}" target="_blank" rel="noopener" style="background:#fff; color:#0284c7; padding:10px 20px; border-radius:12px; font-weight:900;">انضم للقناة الآن ↗</a>
    </div>
    """
    script = """
    <script>
        function copyCoupon(code, el) {
            navigator.clipboard.writeText(code);
            const label = el.querySelector('span:last-child');
            const orig = label.textContent;
            label.textContent = '✅ تم النسخ!';
            setTimeout(() => { label.textContent = orig; }, 1800);
        }
    </script>
    """
    return render_layout("كوبونات الخصم", "coupons", body, script)


@app.get("/login", response_class=HTMLResponse)
def page_login():
    body = """
    <div class="auth-wrap">
        <h1 style="font-size:1.5rem; font-weight:900; margin-bottom:6px; text-align:center;">أهلاً بيك تاني 👋</h1>
        <p style="font-size:0.86rem; color:var(--text-secondary); text-align:center; margin-bottom:20px;">
            سجّل دخولك لمتابعة تنبيهاتك وقائمة منتجاتك المفضلة
        </p>
        <form onsubmit="event.preventDefault(); alert('واجهة تسجيل الدخول جاهزة — سيتم تفعيل الربط الكامل بالحسابات في المرحلة القادمة.');">
            <div class="form-group">
                <label>البريد الإلكتروني</label>
                <input type="email" required placeholder="name@example.com" />
            </div>
            <div class="form-group">
                <label>كلمة المرور</label>
                <input type="password" required placeholder="••••••••" />
            </div>
            <button type="submit" class="btn-primary" style="width:100%; justify-content:center; padding:12px; font-size:0.95rem;">دخول</button>
        </form>
        <p style="text-align:center; margin-top:16px; font-size:0.85rem; color:var(--text-secondary);">
            ما عندكش حساب؟ <a href="/register" style="color:var(--bh-red-bright); font-weight:800;">اعمل حساب جديد</a>
        </p>
    </div>
    """
    return render_layout("تسجيل الدخول", "login", body)


@app.get("/register", response_class=HTMLResponse)
def page_register():
    body = """
    <div class="auth-wrap">
        <h1 style="font-size:1.5rem; font-weight:900; margin-bottom:6px; text-align:center;">اعمل حساب مجاني 🌶️</h1>
        <p style="font-size:0.86rem; color:var(--text-secondary); text-align:center; margin-bottom:20px;">
            احفظ المنتجات اللي تهمك وتابع تاريخ أسعارها في البحرين
        </p>
        <form onsubmit="event.preventDefault(); alert('واجهة إنشاء الحساب جاهزة — سيتم تفعيل قاعدة بيانات المستخدمين في المرحلة القادمة.');">
            <div class="form-group">
                <label>الاسم</label>
                <input type="text" required placeholder="مثال: محمد" />
            </div>
            <div class="form-group">
                <label>البريد الإلكتروني</label>
                <input type="email" required placeholder="name@example.com" />
            </div>
            <div class="form-group">
                <label>كلمة المرور</label>
                <input type="password" required placeholder="8 حروف على الأقل" />
            </div>
            <button type="submit" class="btn-primary" style="width:100%; justify-content:center; padding:12px; font-size:0.95rem;">إنشاء الحساب</button>
        </form>
        <p style="text-align:center; margin-top:16px; font-size:0.85rem; color:var(--text-secondary);">
            عندك حساب بالفعل؟ <a href="/login" style="color:var(--bh-red-bright); font-weight:800;">سجّل دخول</a>
        </p>
    </div>
    """
    return render_layout("حساب جديد", "register", body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)