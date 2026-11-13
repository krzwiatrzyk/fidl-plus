"""
Fidl Plus - web UI.

Run:  python app.py
Open: http://localhost:8000
"""
import asyncio
import contextlib
import html
import json
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import uvicorn
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import scheduler
import services
from receipt_parser import enrich_receipts


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # In-process scheduler: the app must run as a single uvicorn worker.
    if scheduler.ENABLED:
        scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Fidl Plus", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Filtry Jinja2
templates.env.filters["urlencode"] = lambda s: quote(str(s), safe="")
templates.env.filters["tojson"]    = lambda v: json.dumps(v, ensure_ascii=False)

# Sync of all accounts started from the UI; per-account progress lives in services.progress
sync_state = {
    "running": False,
    "new": 0,
    "error": None,
    "done": False,
}

# Data directory. Inside Docker this is the mounted /data volume.
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_data_file(name: str, *legacy_names: str) -> str:
    """
    Path to a data file inside DATA_DIR.

    Falls back to older locations and names so that an existing installation
    keeps working after an upgrade: files used to live in the application
    directory, and the receipt dataset used to have a Polish filename.
    """
    current = os.path.join(DATA_DIR, name)
    if os.path.exists(current):
        return current

    for candidate in (
        *(os.path.join(DATA_DIR, legacy) for legacy in legacy_names),
        os.path.join(_APP_DIR, name),
        *(os.path.join(_APP_DIR, legacy) for legacy in legacy_names),
    ):
        if os.path.exists(candidate):
            print(f"Uwaga: używam {candidate}. Zmień nazwę na {current}")
            return candidate

    return current


DB_FILE = os.path.join(DATA_DIR, "fidl.db")
db.init(DB_FILE)
db.migrate_legacy(
    _resolve_data_file("lidl_tokens.json"),
    _resolve_data_file("receipts.json", "wszystkie_paragony_szczegoly.json"),
)

# Browser preview during login inside Docker (noVNC).
# NOVNC_PORT  - port on the same host as the app (6080 in the Docker image)
# NOVNC_URL   - absolute address when the app sits behind a reverse proxy
# Empty means no noVNC: running locally, Chrome opens directly on the desktop.
NOVNC_PORT = os.getenv("NOVNC_PORT", "")
NOVNC_URL = os.getenv("NOVNC_URL", "")

# fastapi-radar: request/SQL/exception dashboard at /__radar, opt-in because it
# stores request and response bodies without any auth in front of it.
RADAR_ENABLED = os.getenv("RADAR_ENABLED", "0") == "1"
if RADAR_ENABLED:
    from fastapi_radar import Radar

    radar = Radar(
        app,
        db_engine=db.engine,
        db_path=DATA_DIR,
        # Bindings would put access/refresh tokens into radar.duckdb.
        capture_sql_bindings=False,
        # /login carries token uploads; the rest is htmx polling noise.
        exclude_paths=["/login", "/sync/status", "/static"],
        include_in_schema=False,
        service_name="fidl-plus",
    )
    radar.create_tables()
templates.env.globals["radar_enabled"] = RADAR_ENABLED

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ago(value: str | None) -> str:
    if not value:
        return "nigdy"
    seconds = (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "chwilę"
    elif seconds < 3600:
        text = f"{int(seconds // 60)} min"
    elif seconds < 86400:
        text = f"{int(seconds // 3600)} godz."
    else:
        text = f"{int(seconds // 86400)} dni"
    if text == "chwilę":
        return "za chwilę" if future else "przed chwilą"
    return f"za {text}" if future else f"{text} temu"


def _localtime(value: str | None) -> str:
    if not value:
        return "-"
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["ago"] = _ago
templates.env.filters["localtime"] = _localtime


def _duration(run: dict) -> str:
    if not run.get("finished_at"):
        return "-"
    seconds = (datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).seconds
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


templates.env.filters["duration"] = _duration


def accounts_with_status() -> list[dict]:
    return [{**a, "status": services.account_status(a)} for a in db.list_accounts()]


def selected_account(request: Request) -> int | None:
    """Account chosen in the nav filter: ?account= wins, then the cookie."""
    value = request.query_params.get("account", request.cookies.get("account", ""))
    return int(value) if value.isdigit() else None


def url_with(request: Request, **params) -> str:
    """The current page with some query params replaced; None drops one. Always back to page 1."""
    query = {k: v for k, v in request.query_params.items() if k != "page"}
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return f"{request.url.path}?{urlencode(query)}" if query else request.url.path


templates.env.globals["nav_accounts"] = accounts_with_status
templates.env.globals["selected_account"] = selected_account
templates.env.globals["url_with"] = url_with


DATE_KEYS = ("date_from", "date_to")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def date_range(request: Request) -> tuple[str, str]:
    """(date_from, date_to) as YYYY-MM-DD, "" when unset: the query wins, then the session cookie."""
    values = []
    for key in DATE_KEYS:
        value = request.query_params.get(key, request.cookies.get(key, ""))
        values.append(value if _DATE_RE.fullmatch(value) else "")
    return values[0], values[1]


templates.env.globals["date_range"] = date_range
templates.env.globals["date_filter_paths"] = {"/", "/receipts", "/products", "/product",
                                              "/insights", "/top-products"}


@app.middleware("http")
async def remember_filters(request: Request, call_next):
    response = await call_next(request)
    if "account" in request.query_params:
        response.set_cookie("account", request.query_params["account"], max_age=365 * 86400,
                            samesite="lax")
    # A session cookie: the range follows you between pages but is gone after
    # the browser closes, so an old range never silently hides data.
    for key in DATE_KEYS:
        if key not in request.query_params:
            continue
        value = request.query_params[key]
        if _DATE_RE.fullmatch(value):
            response.set_cookie(key, value, samesite="lax")
        else:
            response.delete_cookie(key)
    return response


_receipts_cache: dict = {"version": None, "receipts": []}


def load_receipts() -> list:
    """All receipts with HTML printouts parsed and product names normalized."""
    if _receipts_cache["version"] != db.receipts_version:
        receipts, _ = enrich_receipts(db.all_receipts())
        _receipts_cache.update(version=db.receipts_version, receipts=receipts)
    return _receipts_cache["receipts"]


def scoped_receipts(request: Request) -> list:
    """Receipts of the account and date range picked in the filters; everything when unset."""
    account_id = selected_account(request)
    date_from, date_to = date_range(request)
    receipts = load_receipts()
    if account_id is not None:
        receipts = [r for r in receipts if r.get("_account_id") == account_id]
    if date_from:
        receipts = [r for r in receipts if r.get("date", "")[:10] >= date_from]
    if date_to:
        receipts = [r for r in receipts if r.get("date", "")[:10] <= date_to]
    return receipts


def parse_price(val) -> float:
    """Safely convert a price, which may arrive as '4,39' or as 4.39."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


# Money as "12.34" - tolerates the API returning either a float or "12,34".
templates.env.filters["money"] = lambda v: f"{parse_price(v):.2f}"


def item_discount(item: dict) -> float:
    """
    Total discount applied to a single receipt line.

    W API Lidl `amount` bywa zapisany raz jako "1,50", raz jako "-1,50",
    so normalize it to a positive number (the amount taken off).
    """
    total = 0.0
    for d in item.get("discounts", []) or []:
        total += abs(parse_price(d.get("amount", 0)))
    return total


def item_original_amount(item: dict) -> float:
    """
    Kwota pozycji przed rabatem. Preferujemy `originalAmount` z API,
    because for goods sold by weight unit price times quantity does not add up.
    """
    original = parse_price(item.get("originalAmount", 0))
    if original > 0:
        return original
    qty = parse_price(item.get("quantity", 1)) or 1.0
    return parse_price(item.get("currentUnitPrice", 0)) * qty


def item_prices(item: dict) -> tuple[float, float]:
    """
    Return (base_price, promo_price) per unit.

    Discounts are given as a total for the whole line, so divide by the
    quantity to get a per-unit price.
    """
    qty = parse_price(item.get("quantity", 1)) or 1.0
    if qty <= 0:
        qty = 1.0
    base = parse_price(item.get("currentUnitPrice", 0))
    promo = max((item_original_amount(item) - item_discount(item)) / qty, 0.0)
    return base, promo


def receipt_discount(receipt: dict) -> float:
    """
    Discount for the whole receipt. Receipts parsed from HTML carry no
    `totalDiscount`, so in that case sum the per-line discounts instead.
    """
    discount = parse_price(receipt.get("totalDiscount", 0))
    if discount:
        return abs(discount)
    return sum(item_discount(i) for i in receipt.get("itemsLine", []) or [])


def receipt_total(receipt: dict) -> float:
    """
    Amount actually paid for the receipt.

    Note: `totalAmount` from the API is already NET of discounts - verified
    across the full dataset, where sum(line amounts) - discounts == totalAmount.
    Subtracting `totalDiscount` again would under-report spending.
    """
    return parse_price(receipt.get("totalAmount", 0))


def get_stats(receipts: list) -> dict:
    if not receipts:
        return {}
    total_spent = sum(receipt_total(r) for r in receipts)
    dates = [r["date"][:10] for r in receipts if r.get("date")]
    stores = [r.get("store", {}).get("name", "") for r in receipts if r.get("store")]
    store_counts = defaultdict(int)
    for s in stores:
        store_counts[s] += 1
    fav_store = max(store_counts, key=store_counts.get) if store_counts else "-"

    # Coupons and savings
    total_coupons = sum(len(r.get("couponsUsed", [])) for r in receipts)
    total_discount = sum(receipt_discount(r) for r in receipts)

    # Average gap between shopping trips
    from datetime import date as date_type
    unique_dates = sorted(set(dates))
    if len(unique_dates) > 1:
        date_objs = [date_type.fromisoformat(d) for d in unique_dates]
        gaps = [(date_objs[i+1]-date_objs[i]).days for i in range(len(date_objs)-1)]
        avg_gap = sum(gaps) / len(gaps)
    else:
        avg_gap = 0

    return {
        "total_receipts": len(receipts),
        "total_spent": total_spent,
        "fav_store": fav_store,
        "date_from": min(dates) if dates else "-",
        "date_to": max(dates) if dates else "-",
        "total_coupons": total_coupons,
        "total_discount": total_discount,
        "avg_gap": avg_gap,
    }


def get_insights(receipts: list) -> dict:
    """Dane do strony /insights."""
    from datetime import datetime as dt

    # Heatmap: hour x weekday -> number of visits
    heatmap = defaultdict(int)
    day_names = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]
    for r in receipts:
        try:
            d = dt.fromisoformat(r["date"])
            heatmap[f"{d.weekday()}_{d.hour}"] += 1
        except Exception:
            pass

    # Monthly spending (discounts already deducted)
    monthly = defaultdict(float)
    monthly_visits = defaultdict(int)
    for r in receipts:
        month = r.get("date", "")[:7]
        if month:
            monthly[month] += receipt_total(r)
            monthly_visits[month] += 1
    months_sorted = sorted(monthly.keys())

    # Pie VAT
    vat_labels = {"A": "23% VAT", "B": "8% VAT", "C": "5% VAT", "D": "0% VAT"}
    vat_spend = defaultdict(float)
    for r in receipts:
        for t in r.get("taxes", []) or []:
            key = t.get("taxGroupName", "?")
            if key in vat_labels:
                vat_spend[key] += parse_price(t.get("taxableAmount", 0))

    # Top 10 most expensive receipts (discounts already deducted)
    top_receipts = sorted(
        [r for r in receipts if r.get("id")],
        key=receipt_total,
        reverse=True
    )[:10]

    return {
        "heatmap": dict(heatmap),
        "day_names": day_names,
        "months": months_sorted,
        "monthly_amounts": [round(monthly[m], 2) for m in months_sorted],
        "monthly_visits": [monthly_visits[m] for m in months_sorted],
        "vat_labels": [vat_labels.get(k, k) for k in sorted(vat_spend)],
        "vat_amounts": [round(vat_spend[k], 2) for k in sorted(vat_spend)],
        "top_receipts": top_receipts,
    }


def get_top_products(receipts: list) -> dict:
    """Dane do strony /top-products."""
    product_count = defaultdict(float)
    product_spend = defaultdict(float)
    product_last_price = {}
    price_history = defaultdict(list)

    for r in receipts:
        date = r.get("date", "")[:10]
        for item in r.get("itemsLine", []):
            name = item.get("name", "")
            bc = item.get("codeInput", "")
            if not name:
                continue
            qty = parse_price(item.get("quantity", 1)) or 1
            base_price, promo_price = item_prices(item)
            product_count[name] += qty
            product_spend[name] += promo_price * qty
            product_last_price[name] = promo_price
            if base_price > 0 and bc and len(bc) >= 8:
                price_history[name].append((date, promo_price))

    # Ranking by quantity bought
    top_by_count = sorted(
        [{"name": k, "count": int(v), "spend": round(product_spend[k], 2),
          "last_price": round(product_last_price.get(k, 0), 2)}
         for k, v in product_count.items() if v >= 2],
        key=lambda x: -x["count"]
    )[:20]

    # Largest price swings
    volatility = []
    for name, history in price_history.items():
        if len(history) < 3:
            continue
        prices = [p for _, p in history]
        mn, mx = min(prices), max(prices)
        if mn == 0:
            continue
        pct = (mx - mn) / mn * 100
        if pct < 5:
            continue
        # Pierwsza i ostatnia cena
        history_sorted = sorted(history)
        first_p = history_sorted[0][1]
        last_p = history_sorted[-1][1]
        volatility.append({
            "name": name,
            "min_price": round(mn, 2),
            "max_price": round(mx, 2),
            "first_price": round(first_p, 2),
            "last_price": round(last_p, 2),
            "change_pct": round(pct, 1),
            "count": len(history),
        })
    volatility.sort(key=lambda x: -x["change_pct"])

    return {
        "top_by_count": top_by_count,
        "volatility": volatility[:20],
    }


def get_coupon_stats(receipts: list) -> dict:
    """Coupon usage statistics."""
    from collections import Counter
    coupon_titles = Counter()
    total_discount = 0.0
    receipts_with_coupon = 0

    for r in receipts:
        coupons = r.get("couponsUsed", [])
        if coupons:
            receipts_with_coupon += 1
        for c in coupons:
            title = c.get("couponTitle") or c.get("title") or "?"
            coupon_titles[title] += 1
        total_discount += receipt_discount(r)

    return {
        "total_coupons": sum(coupon_titles.values()),
        "unique_coupons": len(coupon_titles),
        "receipts_with_coupon": receipts_with_coupon,
        "total_discount": round(total_discount, 2),
        "top_coupons": coupon_titles.most_common(10),
    }


def get_product_history(receipts: list, name_query: str) -> list:
    """Price history for every product matching the query."""
    name_query_lower = name_query.lower()
    history = []
    for receipt in receipts:
        date = receipt.get("date", "")[:10]
        store = receipt.get("store", {}).get("name", "")
        for item in receipt.get("itemsLine", []):
            item_name = item.get("name", "")
            if name_query_lower in item_name.lower():
                base_price, promo_price = item_prices(item)
                history.append({
                    "date": date,
                    "name": item_name,
                    "base_price": base_price,
                    "promo_price": promo_price,
                    "store": store,
                    "receipt_id": receipt.get("id", ""),
                })
    history.sort(key=lambda x: x["date"])
    return history


def all_products(receipts: list, query: str = "") -> list:
    """
    Every product ever bought, optionally filtered by name.

    Names are already unified by barcode in enrich_receipts, so grouping by
    name merges the same product sold under different labels.
    """
    query_lower = query.lower()
    products = {}
    for receipt in sorted(receipts, key=lambda r: r.get("date", "")):
        date = receipt.get("date", "")[:10]
        account = receipt.get("_account_label")
        for item in receipt.get("itemsLine", []):
            name = item.get("name", "")
            if not name or query_lower not in name.lower():
                continue
            base_price, promo_price = item_prices(item)
            qty = parse_price(item.get("quantity", 1)) or 1.0
            p = products.setdefault(name.lower(), {
                "name": name, "count": 0, "spend": 0.0, "min_price": promo_price,
                "max_price": promo_price, "accounts": [],
            })
            p["count"] += 1
            p["spend"] += promo_price * qty
            p["base_price"] = base_price
            p["last_price"] = promo_price
            p["last_date"] = date
            p["min_price"] = min(p["min_price"], promo_price)
            p["max_price"] = max(p["max_price"], promo_price)
            if account and account not in p["accounts"]:
                p["accounts"].append(account)
    return sorted(products.values(), key=lambda x: x["count"], reverse=True)


PRODUCT_SORTS = {
    "count": (lambda p: p["count"], True),
    "name": (lambda p: p["name"].lower(), False),
    "spend": (lambda p: p["spend"], True),
    "last_price": (lambda p: p["last_price"], True),
    "last_date": (lambda p: p["last_date"], True),
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    receipts = scoped_receipts(request)
    stats = get_stats(receipts)
    coupon_stats = get_coupon_stats(receipts)
    recent = sorted(receipts, key=lambda r: r.get("date", ""), reverse=True)[:20]
    accounts = accounts_with_status()

    per_account = []
    if len(accounts) > 1 and selected_account(request) is None:
        for a in accounts:
            own = [r for r in receipts if r.get("_account_id") == a["id"]]
            per_account.append({**a, "receipts": len(own), "spent": sum(receipt_total(r) for r in own)})

    return templates.TemplateResponse(request=request, name="index.html", context={
        "request": request,
        "stats": stats,
        "coupon_stats": coupon_stats,
        "recent": recent,
        "accounts": accounts,
        "per_account": per_account,
    })


@app.get("/receipts", response_class=HTMLResponse)
async def receipts_list(
    request: Request,
    page: int = Query(1, ge=1),
    store: str = Query(""),
):
    all_receipts = scoped_receipts(request)
    receipts = all_receipts

    if store:
        receipts = [r for r in receipts if store.lower() in r.get("store", {}).get("name", "").lower()]

    receipts = sorted(receipts, key=lambda r: r.get("date", ""), reverse=True)

    per_page = 25
    total = len(receipts)
    pages = max(1, (total + per_page - 1) // per_page)
    receipts_page = receipts[(page - 1) * per_page : page * per_page]

    # Store list for the filter dropdown
    all_stores = sorted({r.get("store", {}).get("name", "") for r in all_receipts if r.get("store")})

    return templates.TemplateResponse(request=request, name="receipts.html", context={
        "request": request,
        "receipts": receipts_page,
        "page": page,
        "pages": pages,
        "total": total,
        "store": store,
        "all_stores": all_stores,
        "show_account": selected_account(request) is None and len(db.list_accounts()) > 1,
        "parse_price": parse_price,
    })


@app.get("/receipt/{receipt_id}", response_class=HTMLResponse)
async def receipt_detail(request: Request, receipt_id: str):
    receipts = load_receipts()
    receipt = next((r for r in receipts if r.get("id") == receipt_id), None)
    if not receipt:
        return HTMLResponse("Paragon nie znaleziony", status_code=404)
    items = receipt.get("itemsLine", [])
    for item in items:
        item["_price"], item["_promo_price"] = item_prices(item)
        item["_qty"] = parse_price(item.get("quantity", 1))
        item["_discount"] = item_discount(item)
        item["_total"] = max(item_original_amount(item) - item["_discount"], 0.0)

    # The original printout stays available even when itemsLine exists,
    # because for HTML receipts itemsLine is derived from that very printout.
    html_receipt = receipt.get("htmlPrintedReceipt")

    return templates.TemplateResponse(request=request, name="receipt_detail.html", context={
        "request": request,
        "receipt": receipt,
        "items": items,
        "html_receipt": html_receipt,
        "coupons": receipt.get("couponsUsed", []),
        "parse_price": parse_price,
    })


@app.get("/receipt/{receipt_id}/print", response_class=HTMLResponse)
async def receipt_print(request: Request, receipt_id: str):
    """Oryginalny wydruk paragonu na osobnej stronie, gotowy do druku."""
    receipts = load_receipts()
    receipt = next((r for r in receipts if r.get("id") == receipt_id), None)
    if not receipt:
        return HTMLResponse("Paragon nie znaleziony", status_code=404)

    html_receipt = receipt.get("htmlPrintedReceipt")
    if not html_receipt:
        return HTMLResponse("Ten paragon nie ma wersji do wydruku", status_code=404)

    # Take just the <body> content so it can be embedded in our own page
    # instead of serving a foreign document along with its <head> and styles.
    match = re.search(r"<body[^>]*>(.*?)</body>", html_receipt, re.IGNORECASE | re.DOTALL)
    body = match.group(1) if match else html_receipt

    return templates.TemplateResponse(request=request, name="receipt_print.html", context={
        "request": request,
        "receipt": receipt,
        "receipt_body": body,
    })


@app.get("/insights", response_class=HTMLResponse)
async def insights(request: Request):
    receipts = scoped_receipts(request)
    data = get_insights(receipts)
    return templates.TemplateResponse(request=request, name="insights.html", context={
        "request": request,
        **data,
        "parse_price": parse_price,
    })


@app.get("/top-products", response_class=HTMLResponse)
async def top_products(request: Request):
    receipts = scoped_receipts(request)
    data = get_top_products(receipts)
    return templates.TemplateResponse(request=request, name="top_products.html", context={
        "request": request,
        **data,
    })


@app.get("/search")
async def search(q: str = Query("")):
    return RedirectResponse(f"/products?q={quote(q)}", status_code=301)


@app.get("/products", response_class=HTMLResponse)
async def products(
    request: Request,
    q: str = Query(""),
    sort: str = Query("count"),
    page: int = Query(1, ge=1),
):
    if sort not in PRODUCT_SORTS:
        sort = "count"
    key, reverse = PRODUCT_SORTS[sort]
    found = sorted(all_products(scoped_receipts(request), q), key=key, reverse=reverse)

    per_page = 50
    pages = max(1, (len(found) + per_page - 1) // per_page)
    show_account = selected_account(request) is None and len(db.list_accounts()) > 1
    columns = [("name", "Produkt"), ("count", "Zakupów"), ("spend", "Wydano"),
               ("last_price", "Ostatnia cena"), (None, "Zakres cen"), ("last_date", "Ostatnio")]
    if show_account:
        columns.append((None, "Konta"))

    return templates.TemplateResponse(request=request, name="products.html", context={
        "request": request,
        "q": q,
        "sort": sort,
        "page": page,
        "pages": pages,
        "total": len(found),
        "products": found[(page - 1) * per_page : page * per_page],
        "columns": columns,
        "show_account": show_account,
    })


@app.get("/product", response_class=HTMLResponse)
async def product(request: Request, name: str = Query("")):
    receipts = scoped_receipts(request)
    history = get_product_history(receipts, name) if name else []

    # Chart data uses the promotional price, i.e. what was actually paid
    chart_labels = [h["date"] for h in history]
    chart_prices = [h["promo_price"] for h in history]
    chart_stores = [h["store"] for h in history]

    avg = sum(chart_prices) / len(chart_prices) if chart_prices else 0
    min_price = min(chart_prices) if chart_prices else 0
    max_price = max(chart_prices) if chart_prices else 0

    return templates.TemplateResponse(request=request, name="product.html", context={
        "request": request,
        "name": name,
        "history": history,
        "chart_labels": json.dumps(chart_labels),
        "chart_prices": json.dumps(chart_prices),
        "chart_stores": json.dumps(chart_stores),
        "avg": avg,
        "min_price": min_price,
        "max_price": max_price,
    })


# One login at a time: there is a single Xvfb display and one callback file.
_login_lock = threading.Lock()
login_state: dict = {"started_at": None, "account_id": None, "running": False}


def _parse_account_id(value) -> int | None:
    return int(value) if str(value or "").isdigit() else None


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, account_id: str = Query("")):
    account = db.get_account(_parse_account_id(account_id) or 0)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "request": request,
        "account": account,
        "accounts": db.list_accounts(),
        "novnc_port": NOVNC_PORT,
        "novnc_url": NOVNC_URL,
    })


@app.post("/login/token", response_class=HTMLResponse)
async def login_upload_token(request: Request):
    """
    Upload a lidl_tokens.json produced elsewhere.

    Fallback for when the in-browser login cannot be completed on the server,
    for example because Lidl blocks the login from that IP address.
    """
    form = await request.form()
    token_file = form.get("token_file")
    if token_file and hasattr(token_file, "read"):
        content = await token_file.read()
        try:
            tokens = json.loads(content)
            if not isinstance(tokens, dict) or "access_token" not in tokens:
                raise ValueError("brak pola access_token")
            db.store_login(tokens, _parse_account_id(form.get("account_id")))
            return HTMLResponse("""
                <div id="token-upload-status" class="status-ok">
                    Tokeny wgrane pomyślnie! <a href="/accounts">Przejdź do kont</a>
                </div>
            """)
        except Exception as e:
            return HTMLResponse(f"""
                <div id="token-upload-status" class="alert alert-warn">
                    Błąd: nieprawidłowy plik tokenów ({html.escape(str(e))})
                </div>
            """)
    return HTMLResponse("""
        <div id="token-upload-status" class="alert alert-warn">
            Wybierz plik lidl_tokens.json
        </div>
    """)


@app.post("/login/start", response_class=HTMLResponse)
async def login_start(account_id: str = Form("")):
    """Run browser_login.py in the background and return a status snippet."""
    if not _login_lock.acquire(blocking=False):
        return HTMLResponse("""
            <div id="login-status" class="alert alert-warn">
                Inne logowanie już trwa - dokończ je albo poczekaj aż wygaśnie.
            </div>
        """)

    target = _parse_account_id(account_id)
    login_state.update(started_at=db.now(), account_id=target, running=True)
    command = [sys.executable, "browser_login.py", "--db"]
    if target:
        command += ["--account-id", str(target)]

    def run_login():
        try:
            subprocess.run(command, cwd=os.path.dirname(os.path.abspath(__file__)))
        finally:
            login_state["running"] = False
            _login_lock.release()

    threading.Thread(target=run_login, daemon=True).start()
    return HTMLResponse("""
        <div id="login-status" class="status-running"
             hx-get="/login/status" hx-trigger="every 2s" hx-swap="outerHTML">
            Otwieranie przeglądarki... zaloguj się w jej oknie.
        </div>
    """)


@app.get("/login/status", response_class=HTMLResponse)
async def login_status():
    started = login_state["started_at"]
    if started:
        for account in db.list_accounts():
            if login_state["account_id"] and account["id"] != login_state["account_id"]:
                continue
            if (account.get("refresh_obtained_at") or "") >= started:
                return HTMLResponse(f"""
                    <div id="login-status" class="status-ok">
                        Zalogowano jako {html.escape(account["label"])}!
                        <a href="/accounts">Przejdź do kont</a>
                    </div>
                """)
    if not login_state["running"]:
        return HTMLResponse("""
            <div id="login-status" class="alert alert-warn">
                Logowanie nie powiodło się albo przekroczono czas. Spróbuj ponownie.
            </div>
        """)
    return HTMLResponse("""
        <div id="login-status" class="status-running"
             hx-get="/login/status" hx-trigger="every 2s" hx-swap="outerHTML">
            Czekam na zalogowanie...
        </div>
    """)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    return templates.TemplateResponse(request=request, name="accounts.html", context={
        "request": request,
        "accounts": accounts_with_status(),
        "progress": services.progress,
    })


@app.post("/accounts/{account_id}/refresh")
async def account_refresh(account_id: int):
    with contextlib.suppress(Exception):  # the error is recorded on the account
        await asyncio.to_thread(services.refresh_account, account_id)
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/sync")
async def account_sync(account_id: int):
    def run():
        with contextlib.suppress(Exception):  # the error lands in services.progress
            services.sync_account(account_id)

    threading.Thread(target=run, daemon=True).start()
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/rename")
async def account_rename(account_id: int, label: str = Form(...)):
    if label.strip():
        db.update_account(account_id, label=label.strip())
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/delete")
async def account_delete(account_id: int):
    db.delete_account(account_id)
    return RedirectResponse("/accounts", status_code=303)


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    return templates.TemplateResponse(request=request, name="sync.html", context={"request": request})


def run_sync():
    sync_state.update(running=True, new=0, error=None, done=False)
    try:
        accounts = db.list_accounts()
        if not accounts:
            sync_state["error"] = "Brak kont - zaloguj się najpierw."
            return
        errors = []
        for account in accounts:
            try:
                sync_state["new"] += services.sync_account(account["id"])
            except Exception as e:
                errors.append(f"{account['label']}: {e}")
        if errors:
            sync_state["error"] = "; ".join(errors)
        else:
            sync_state["done"] = True
    finally:
        sync_state["running"] = False


@app.post("/sync/start", response_class=HTMLResponse)
async def sync_start():
    """Pobierz paragony z API w tle."""
    if sync_state["running"]:
        return HTMLResponse("""
            <div id="sync-status" class="status-running"
                 hx-get="/sync/status" hx-trigger="every 2s" hx-swap="outerHTML">
                Synchronizacja już trwa...
            </div>
        """)

    threading.Thread(target=run_sync, daemon=True).start()
    return HTMLResponse("""
        <div id="sync-status" class="status-running"
             hx-get="/sync/status" hx-trigger="every 2s" hx-swap="outerHTML">
            Pobieranie paragonów...
        </div>
    """)


@app.get("/sync/status", response_class=HTMLResponse)
async def sync_status():
    if sync_state.get("error"):
        return HTMLResponse(f"""
            <div id="sync-status" class="alert alert-warn">
                Błąd: {html.escape(sync_state["error"])}
            </div>
        """)

    if sync_state.get("done"):
        count = sync_state["new"]
        return HTMLResponse(f"""
            <div id="sync-status" class="status-ok">
                Pobrano {count} nowych paragonów.
                <a href="/">Przejdź do dashboardu</a>
            </div>
        """)

    if sync_state.get("running"):
        running = [p for p in services.progress.values() if p["running"]]
        count = sum(p["count"] for p in running)
        total = sum(p["total"] for p in running)
        pct = int(count / total * 100) if total else 0
        return HTMLResponse(f"""
            <div id="sync-status" class="status-running"
                 hx-get="/sync/status" hx-trigger="every 2s" hx-swap="outerHTML">
                <div style="margin-bottom:.5rem">
                    Pobieranie: {count} / {total} paragonów ({pct}%)
                </div>
                <div style="background:#fde68a;border-radius:4px;height:8px;overflow:hidden;">
                    <div style="background:#92400e;height:100%;width:{pct}%;transition:width .5s;"></div>
                </div>
            </div>
        """)

    # Not running - show the last result
    count = len(load_receipts())
    if count:
        return HTMLResponse(f"""
            <div id="sync-status" class="status-ok">
                Dane aktualne: {count} paragonów w bazie.
                <a href="/">Dashboard</a>
            </div>
        """)
    return HTMLResponse("""
        <div id="sync-status" style="color:var(--muted);font-size:.9rem">
            Kliknij "Rozpocznij pobieranie" aby pobrać paragony.
        </div>
    """)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    return templates.TemplateResponse(request=request, name="jobs.html", context={
        "request": request,
        "jobs": scheduler.jobs_overview(),
        "runs": db.recent_runs(),
        "enabled": scheduler.scheduler.running,
    })


@app.post("/jobs/{job_id}/run")
async def job_run_now(job_id: str):
    if job_id in scheduler.JOBS:
        scheduler.run_now(job_id)
    return RedirectResponse("/jobs", status_code=303)


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webbrowser
    docker_mode = "--docker" in sys.argv
    host = os.environ.get("FIDL_HOST", "0.0.0.0")
    port = int(os.environ.get("FIDL_PORT", "9000"))
    print("\n" + "="*50)
    print("  Fidl Plus - Web UI")
    if docker_mode:
        print(f"  http://localhost:{port}")
        print("  Tryb Docker - logowanie przez /login/token")
    else:
        print(f"  http://localhost:{port}")
        webbrowser.open(f"http://localhost:{port}")
    print("="*50 + "\n")
    uvicorn.run(app, host=host, port=port, reload=False)
