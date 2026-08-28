"""
Lokaler DEGIRO-Proxy fuer das degiro-akad Dashboard.

Laeuft NUR auf deinem eigenen Rechner (127.0.0.1) und leitet Anfragen von der
GitHub-Pages-Seite an DEGIRO (und an eine oeffentliche, kostenlose FX-API fuer
Waehrungsumrechnung) weiter. Es wird NICHTS auf die Festplatte oder nach
GitHub geschrieben - Login-Daten und DEGIRO-Antworten leben ausschliesslich
im Arbeitsspeicher dieses Prozesses, solange er laeuft. Beendest du das
Skript (Strg+C), ist alles weg.

Start:
    pip install -r requirements.txt
    python proxy.py

Optional:
    python proxy.py --port 8765 --origin https://mattieddie.github.io
"""

from __future__ import annotations

import argparse
import logging
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

import requests
from flask import Flask, jsonify, request

from degiro_connector.core.exceptions import DeGiroConnectionError
from degiro_connector.quotecast.models.chart import ChartRequest, Interval
from degiro_connector.quotecast.tools.chart_fetcher import ChartFetcher
from degiro_connector.trading.api import API as TradingAPI
from degiro_connector.trading.models.account import (
    OverviewRequest,
    UpdateOption,
    UpdateRequest,
)
from degiro_connector.trading.models.credentials import Credentials
from degiro_connector.trading.models.transaction import HistoryRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("degiro-proxy")

app = Flask(__name__)

# --- In-Memory-Session & Caches (nichts wird auf Platte persistiert) --------
SESSION: dict = {
    "trading_api": None,
    "int_account": None,
    "user_token": None,
    "base_currency": None,
    "logged_in_at": None,
}

# Nur fuer die Dauer des laufenden Prozesses im RAM - kein Zugriff auf Disk.
FX_SERIES_CACHE: dict[tuple, dict[str, float]] = {}
CHART_CACHE: dict[tuple, dict] = {}

TARGET_CURRENCY = "CHF"

# Wird beim Start einmalig erzeugt und in der Konsole ausgegeben. Die Seite
# muss dieses Token kennen, um den Proxy ansprechen zu duerfen.
PROXY_TOKEN = secrets.token_urlsafe(24)

ALLOWED_ORIGINS: list[str] = []


# --- Hilfsfunktionen: DEGIRO-Rohformat -------------------------------------

def _flatten_rows(raw_rows: list) -> list[dict]:
    """Wandelt DEGIROs name/value-Zeilenformat in flache dicts um."""
    flat_rows = []
    for row in raw_rows or []:
        if isinstance(row.get("value"), list) and row["value"] and isinstance(row["value"][0], dict) and "name" in row["value"][0]:
            flat = {"id": row.get("id")}
            for item in row["value"]:
                flat[item.get("name")] = item.get("value")
            flat_rows.append(flat)
        else:
            flat_rows.append(row)
    return flat_rows


def _flatten_dict(raw: dict) -> dict:
    """Wandelt eine einzelne name/value-Liste (z.B. totalPortfolio) in ein flaches dict um."""
    value = raw.get("value") if isinstance(raw, dict) else None
    if isinstance(value, list) and value and isinstance(value[0], dict) and "name" in value[0]:
        return {item.get("name"): item.get("value") for item in value}
    return raw


def _json_safe(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return _json_safe(obj.model_dump(mode="json", by_alias=True))
    return obj


def _num(value, default=0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


# --- Hilfsfunktionen: Waehrungsumrechnung (frankfurter.app, ECB-Kurse) ------

def get_fx_series(start_d: date, end_d: date, from_ccy: str, to_ccy: str) -> dict[str, float]:
    """Liefert {ISO-Datum: Kurs} fuer from_ccy->to_ccy. Leeres dict bei gleicher Waehrung."""
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return {}
    key = (start_d.isoformat(), end_d.isoformat(), from_ccy, to_ccy)
    if key in FX_SERIES_CACHE:
        return FX_SERIES_CACHE[key]
    try:
        resp = requests.get(
            f"https://api.frankfurter.app/{start_d.isoformat()}..{end_d.isoformat()}",
            params={"from": from_ccy, "to": to_ccy},
            timeout=15,
        )
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        series = {d: v[to_ccy] for d, v in rates.items() if to_ccy in v}
    except Exception:  # noqa: BLE001
        logger.warning("FX-Abruf %s->%s fehlgeschlagen, verwende 1:1", from_ccy, to_ccy)
        series = {}
    FX_SERIES_CACHE[key] = series
    return series


def fx_rate_on(fx_series: dict[str, float], day: date, fallback: float = 1.0) -> float:
    """Letzter bekannter Kurs an/vor `day` (Wochenenden/Feiertage vorwaertsgefuellt)."""
    if not fx_series:
        return fallback
    d = day
    for _ in range(10):
        rate = fx_series.get(d.isoformat())
        if rate is not None:
            return rate
        d -= timedelta(days=1)
    return fallback


def get_latest_fx_rate(from_ccy: str, to_ccy: str) -> float:
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return 1.0
    series = get_fx_series(date.today() - timedelta(days=7), date.today(), from_ccy, to_ccy)
    return fx_rate_on(series, date.today(), fallback=1.0)


def detect_base_currency(client_details: dict) -> str:
    data = (client_details or {}).get("data", {})
    return data.get("currency") or data.get("baseCurrency") or "EUR"


# --- Hilfsfunktionen: historische Kurse (DEGIRO vwd-Chart-API) --------------

def fetch_daily_close_series(user_token: int, vwd_id: str, since_d: date) -> dict[str, float]:
    """
    Liefert eine Serie {ISO-Datum: Schlusskurs} fuer ein Produkt seit `since_d`.
    Das Ergebnis sollte vom Aufrufer immer mit verify_series() gegen den
    separat von DEGIRO gemeldeten Schlusskurs geprueft werden, da das
    Chart-Rohformat nicht offiziell dokumentiert ist.
    """
    cache_key = (vwd_id, since_d.isoformat())
    if cache_key in CHART_CACHE:
        return CHART_CACHE[cache_key]

    years_back = max(1, (date.today() - since_d).days // 365 + 1)
    period = {
        1: Interval.P1Y,
        2: Interval.P3Y,
        3: Interval.P3Y,
        4: Interval.P5Y,
        5: Interval.P5Y,
    }.get(min(years_back, 5), Interval.P10Y)
    if years_back > 5:
        period = Interval.P10Y

    series_map: dict[str, float] = {}
    try:
        fetcher = ChartFetcher(user_token=user_token)
        chart_request = ChartRequest(
            culture="de-CH",
            period=period,
            requestid="1",
            resolution=Interval.P1D,
            series=[f"ohlc:issueid:{vwd_id}"],
            tz="Europe/Zurich",
        )
        chart = fetcher.get_chart(chart_request=chart_request, raw=True)
        raw_series = (chart or {}).get("series") or []
        ohlc_series = next((s for s in raw_series if str(s.get("id", "")).startswith("ohlc:")), None)
        start_raw = (chart or {}).get("start")
        resolution = (chart or {}).get("resolution") or "P1D"
        step = timedelta(days=1) if "D" in resolution else timedelta(days=1)

        if ohlc_series and start_raw:
            start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            for point in ohlc_series.get("data") or []:
                if not isinstance(point, list) or len(point) < 5:
                    continue
                x, _o, _h, _l, c = point[:5]
                point_date = (start_dt + step * x).date()
                if point_date >= since_d and c is not None:
                    series_map[point_date.isoformat()] = float(c)
    except Exception:  # noqa: BLE001
        logger.warning("Kurs-Chart fuer vwdId=%s nicht abrufbar", vwd_id, exc_info=True)
        series_map = {}

    CHART_CACHE[cache_key] = series_map
    return series_map


def verify_series(
    series_map: dict[str, float],
    reference_price: float | None,
    reference_date: str | None,
    tolerance_pct: float = 0.01,
) -> bool:
    if not series_map or reference_price is None or not reference_date:
        return False
    got = series_map.get(reference_date)
    if got is None:
        return False
    return abs(got - reference_price) <= max(0.01, reference_price * tolerance_pct)


# --- Hilfsfunktionen: historische Kurse, Fallback (Yahoo Finance) -----------
# Wird nur verwendet, wenn DEGIROs eigenes Kurschart nicht abrufbar ist oder
# sich nicht gegen den von DEGIRO gemeldeten Schlusskurs verifizieren laesst.
# Aufloesung ueber die ISIN (eindeutig, kein Rateversuch bei Boersenkuerzeln
# noetig) via Yahoo's oeffentliche, unauthentifizierte Such- und Chart-Endpunkte.

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; degiro-akad-dashboard)"}


def yahoo_resolve_symbol(isin: str | None, fallback_symbol: str | None) -> str | None:
    if isin:
        try:
            resp = requests.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": isin, "quotesCount": 5, "newsCount": 0},
                headers=YAHOO_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            quotes = resp.json().get("quotes") or []
            if quotes and quotes[0].get("symbol"):
                return quotes[0]["symbol"]
        except Exception:  # noqa: BLE001
            logger.warning("Yahoo-ISIN-Suche fuer %s fehlgeschlagen", isin, exc_info=True)
    return fallback_symbol


def fetch_yahoo_daily_close_series(isin: str | None, fallback_symbol: str | None, since_d: date) -> tuple[dict[str, float], str | None]:
    """Liefert (Serie {ISO-Datum: Schlusskurs}, Waehrung laut Yahoo) oder ({}, None)."""
    symbol = yahoo_resolve_symbol(isin, fallback_symbol)
    if not symbol:
        return {}, None

    cache_key = ("yahoo", symbol, since_d.isoformat())
    if cache_key in CHART_CACHE:
        return CHART_CACHE[cache_key]

    years_back = max(1, (date.today() - since_d).days // 365 + 1)
    range_param = "10y" if years_back > 5 else ("5y" if years_back > 2 else "2y")

    series_map: dict[str, float] = {}
    currency = None
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": range_param, "interval": "1d"},
            headers=YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        result = ((resp.json() or {}).get("chart") or {}).get("result") or []
        if result:
            r0 = result[0]
            timestamps = r0.get("timestamp") or []
            quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            currency = (r0.get("meta") or {}).get("currency")
            for ts, c in zip(timestamps, closes):
                if c is None:
                    continue
                d = datetime.utcfromtimestamp(ts).date()
                if d >= since_d:
                    series_map[d.isoformat()] = float(c)
    except Exception:  # noqa: BLE001
        logger.warning("Yahoo-Chart fuer Symbol=%s nicht abrufbar", symbol, exc_info=True)
        series_map = {}

    result_tuple = (series_map, currency)
    CHART_CACHE[cache_key] = result_tuple
    return result_tuple


def resolve_price_series(
    user_token: int,
    vwd_id: str | None,
    isin: str | None,
    symbol: str | None,
    currency: str,
    close_price: float | None,
    close_price_date: str | None,
    since_d: date,
) -> tuple[dict[str, float], str, bool, str]:
    """
    Liefert (Serie, Quelle, verifiziert, Waehrung_der_Serie). Reihenfolge:
    1. DEGIRO-Kurschart, falls gegen DEGIROs eigenen Schlusskurs verifizierbar.
    2. Yahoo Finance (oeffentlich, ueber ISIN aufgeloest), falls DEGIRO nicht
       verifizierbar ist - mit etwas grosszuegigerer Toleranz, da
       unterschiedliche Datenanbieter leicht abweichende Schlusskurse melden
       koennen.
    3. Letzter unverifizierter DEGIRO-Versuch, falls vorhanden.
    4. Nichts gefunden.
    """
    degiro_series: dict[str, float] = {}
    if vwd_id:
        degiro_series = fetch_daily_close_series(user_token, vwd_id, since_d)
        if verify_series(degiro_series, close_price, close_price_date):
            return degiro_series, "degiro", True, currency

    yahoo_series, yahoo_ccy = fetch_yahoo_daily_close_series(isin, symbol, since_d)
    if yahoo_series:
        yahoo_verified = verify_series(yahoo_series, close_price, close_price_date, tolerance_pct=0.03)
        return yahoo_series, "yahoo", yahoo_verified, (yahoo_ccy or currency)

    if degiro_series:
        return degiro_series, "degiro", False, currency

    return {}, "none", False, currency


# --- Auth-Decorators ---------------------------------------------------------

def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, PROXY_TOKEN):
            return jsonify({"error": "Ungueltiges oder fehlendes Proxy-Token"}), 401
        return fn(*args, **kwargs)

    return wrapper


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if SESSION["trading_api"] is None:
            return jsonify({"error": "Nicht bei DEGIRO angemeldet"}), 409
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def options_handler(_any):
    return ("", 204)


# --- Endpunkte: Verbindung ---------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "logged_in": SESSION["trading_api"] is not None,
        "logged_in_at": SESSION["logged_in_at"],
        "baseCurrency": SESSION["base_currency"],
        "targetCurrency": TARGET_CURRENCY,
    })


@app.route("/api/login", methods=["POST"])
def login():
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, PROXY_TOKEN):
        return jsonify({"error": "Ungueltiges oder fehlendes Proxy-Token"}), 401

    body = request.get_json(silent=True) or {}
    username = body.get("username")
    password = body.get("password")
    totp_secret_key = body.get("totp_secret_key") or None
    one_time_password = body.get("one_time_password") or None

    if not username or not password:
        return jsonify({"error": "username und password sind erforderlich"}), 400

    try:
        credentials = Credentials(
            username=username,
            password=password,
            totp_secret_key=totp_secret_key,
            one_time_password=int(one_time_password) if one_time_password else None,
        )
        trading_api = TradingAPI(credentials=credentials)
        _connect_with_in_app_wait(trading_api)

        client_details = trading_api.get_client_details()
        int_account = client_details["data"]["intAccount"]
        user_token = client_details["data"]["id"]
        trading_api.credentials.int_account = int_account

        SESSION["trading_api"] = trading_api
        SESSION["int_account"] = int_account
        SESSION["user_token"] = user_token
        SESSION["base_currency"] = detect_base_currency(client_details)
        SESSION["logged_in_at"] = datetime.now().isoformat()

        logger.info("Login erfolgreich (int_account=%s, base_currency=%s)", int_account, SESSION["base_currency"])
        return jsonify({"success": True, "baseCurrency": SESSION["base_currency"]})
    except DeGiroConnectionError as e:
        logger.warning("DEGIRO-Loginfehler: %s", e)
        return jsonify({"error": f"DEGIRO-Loginfehler: {e}"}), 401
    except Exception as e:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler beim Login")
        return jsonify({"error": f"Unerwarteter Fehler: {e}"}), 500


def _connect_with_in_app_wait(trading_api: TradingAPI, max_wait_seconds: int = 90) -> None:
    """Verbindet sich; falls DEGIRO eine Bestaetigung in der App verlangt,
    wird bis zu max_wait_seconds gewartet (du musst dann in der DEGIRO-App bestaetigen)."""
    try:
        trading_api.connect()
        return
    except DeGiroConnectionError as e:
        if e.error_details is None or e.error_details.status != 12:
            raise
        logger.info("DEGIRO verlangt Bestaetigung in der App. Bitte in der DEGIRO-App bestaetigen...")
        trading_api.credentials.in_app_token = e.error_details.in_app_token
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            time.sleep(3)
            try:
                trading_api.connect()
                return
            except DeGiroConnectionError as retry_error:
                if retry_error.error_details and retry_error.error_details.status == 3:
                    continue
                raise
        raise TimeoutError("Zeitlimit fuer die App-Bestaetigung erreicht")


@app.route("/api/logout", methods=["POST"])
@require_auth
def logout():
    trading_api = SESSION.get("trading_api")
    if trading_api is not None:
        try:
            trading_api.logout()
        except Exception:  # noqa: BLE001
            pass
    SESSION.update({
        "trading_api": None, "int_account": None, "user_token": None,
        "base_currency": None, "logged_in_at": None,
    })
    return jsonify({"success": True})


# --- Endpunkte: Portfolio -----------------------------------------------------

def _get_cash_available_to_trade(account_update: dict) -> tuple[float, str]:
    """
    'Available to trade'-Guthaben aus dem CASH_FUNDS-Block (nicht aus den
    mehrdeutigen totalPortfolio-Reportfeldern). Liefert (Betrag, Waehrung)
    fuer die groesste Position; bei mehreren Waehrungen werden alle
    zurueckgegeben (siehe cashFunds im Response).
    """
    raw_cash = (account_update.get("cashFunds") or {}).get("value") or []
    rows = _flatten_rows(raw_cash)
    best = None
    for row in rows:
        amount = _num(row.get("value"))
        currency = row.get("currencyCode") or row.get("currency")
        if currency and (best is None or amount > best[0]):
            best = (amount, currency)
    return best or (0.0, "EUR")


@app.route("/api/portfolio", methods=["GET"])
@require_auth
@require_login
def portfolio():
    trading_api = SESSION["trading_api"]
    base_currency = SESSION["base_currency"] or "EUR"
    try:
        account_update = trading_api.get_update(
            request_list=[
                UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0),
                UpdateRequest(option=UpdateOption.TOTAL_PORTFOLIO, last_updated=0),
                UpdateRequest(option=UpdateOption.CASH_FUNDS, last_updated=0),
            ],
            raw=True,
        )

        raw_positions = (account_update.get("portfolio") or {}).get("value") or []
        positions = _flatten_rows(raw_positions)
        positions = [p for p in positions if p.get("positionType") in (None, "PRODUCT") and _num(p.get("size")) != 0]

        product_ids = [int(p["id"]) for p in positions if p.get("id") is not None]
        product_info = {}
        if product_ids:
            info_raw = trading_api.get_products_info(product_list=product_ids, raw=True)
            product_info = (info_raw or {}).get("data", {})

        cash_amount, cash_currency = _get_cash_available_to_trade(account_update)
        cash_chf = cash_amount * get_latest_fx_rate(cash_currency, TARGET_CURRENCY)

        enriched = []
        total_value_chf = 0.0
        for p in positions:
            pid = str(p.get("id"))
            info = product_info.get(pid, {})
            currency = p.get("currency") or info.get("currency") or base_currency

            size = _num(p.get("size"))
            price = _num(p.get("price"))
            avg_price = _num(p.get("breakEvenPrice")) or _num(p.get("averagePrice"))
            value_native = p.get("value")
            value_native = _num(value_native) if value_native is not None else size * price

            fx = get_latest_fx_rate(currency, TARGET_CURRENCY)
            value_chf = value_native * fx
            avg_cost_chf = avg_price * fx
            # Unrealisierter G/V = Stueck * (aktueller Kurs - Einstandskurs), in Originalwaehrung,
            # dann nach CHF umgerechnet. (Vorher faelschlich das mehrdeutige "plBase"-Feld verwendet.)
            pl_unrealized_native = size * (price - avg_price) if avg_price else None
            pl_unrealized_chf = pl_unrealized_native * fx if pl_unrealized_native is not None else None
            pl_pct = ((price - avg_price) / avg_price * 100) if avg_price else None

            total_value_chf += value_chf

            enriched.append({
                "productId": pid,
                "vwdId": info.get("vwdId"),
                "name": info.get("name") or p.get("name"),
                "symbol": info.get("symbol"),
                "isin": info.get("isin"),
                "exchangeId": info.get("exchangeId"),
                "productType": info.get("productType"),
                "currency": currency,
                "fxRateToChf": fx,
                "size": size,
                "price": price,
                "priceChf": price * fx,
                "averagePrice": avg_price,
                "averagePriceChf": avg_cost_chf,
                "value": value_native,
                "valueChf": value_chf,
                "plUnrealized": pl_unrealized_native,
                "plUnrealizedChf": pl_unrealized_chf,
                "plUnrealizedPct": pl_pct,
            })

        equity_chf = total_value_chf + cash_chf

        return jsonify({
            "fetchedAt": datetime.now().isoformat(),
            "currency": TARGET_CURRENCY,
            "baseCurrency": base_currency,
            "positions": enriched,
            "totals": {
                "equityChf": equity_chf,
                "cashChf": cash_chf,
                "cashNative": cash_amount,
                "cashCurrency": cash_currency,
                "positionsValueChf": total_value_chf,
            },
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen des Portfolios")
        return jsonify({"error": f"Fehler beim Abrufen des Portfolios: {e}"}), 502


@app.route("/api/transactions", methods=["GET"])
@require_auth
@require_login
def transactions():
    trading_api = SESSION["trading_api"]
    days = int(request.args.get("days", 3650))
    try:
        history = trading_api.get_transactions_history(
            transaction_request=HistoryRequest(
                from_date=date.today() - timedelta(days=days),
                to_date=date.today(),
            ),
            raw=False,
        )
        items = _json_safe(history.data if hasattr(history, "data") else history)
        return jsonify({"fetchedAt": datetime.now().isoformat(), "transactions": items})
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen der Transaktionen")
        return jsonify({"error": f"Fehler beim Abrufen der Transaktionen: {e}"}), 502


# --- Endpunkte: Historie (seit Kauf, in CHF) ---------------------------------

def _get_cash_history_chf(trading_api, base_currency: str, since_d: date) -> dict[str, float]:
    """
    Exakte taegliche Cash-Historie aus DEGIROs eigenen 'cashMovements' (inkl.
    laufendem Saldo je Buchung), nach CHF umgerechnet und vorwaertsgefuellt.
    """
    try:
        overview = trading_api.get_account_overview(
            overview_request=OverviewRequest(from_date=since_d, to_date=date.today()),
            raw=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Kontoauszug (cashMovements) nicht abrufbar", exc_info=True)
        return {}

    movements = ((overview or {}).get("data") or {}).get("cashMovements") or []
    if not movements:
        return {}

    by_day_last: dict[str, dict] = {}
    for m in movements:
        d = str(m.get("date") or "")[:10]
        if not d:
            continue
        by_day_last[d] = m  # Liste ist idR chronologisch; letzter Eintrag pro Tag gewinnt

    fx_cache_series: dict[str, dict[str, float]] = {}
    result: dict[str, float] = {}
    for d, movement in sorted(by_day_last.items()):
        balance = movement.get("balance") or {}
        day = date.fromisoformat(d)
        total_chf = 0.0
        for ccy, amount in balance.items():
            if ccy not in fx_cache_series:
                fx_cache_series[ccy] = get_fx_series(since_d, date.today(), ccy, TARGET_CURRENCY)
            total_chf += _num(amount) * fx_rate_on(fx_cache_series[ccy], day, fallback=1.0)
        result[d] = total_chf
    return result


def _forward_fill(series_by_day: dict[str, float], start_d: date, end_d: date) -> list[dict]:
    out = []
    last_value = None
    d = start_d
    while d <= end_d:
        v = series_by_day.get(d.isoformat())
        if v is not None:
            last_value = v
        out.append({"date": d.isoformat(), "value": last_value})
        d += timedelta(days=1)
    return out


@app.route("/api/history/backfill", methods=["GET"])
@require_auth
@require_login
def history_backfill():
    """
    Best-effort Historie seit dem ersten Kauf, berechnet aus Transaktionen +
    DEGIRO-Kurscharts + Kontoauszug. Da das Kurschart-Rohformat nicht
    offiziell dokumentiert ist, wird jede Kursserie gegen den von DEGIRO
    separat gemeldeten Schlusskurs validiert ("verified"). Nicht verifizierte
    Positionen fliessen mit ihrem letzten bekannten Kurs (flach) ein, statt
    falsche Kurse zu zeigen - das Frontend markiert das entsprechend.
    """
    trading_api = SESSION["trading_api"]
    base_currency = SESSION["base_currency"] or "EUR"
    try:
        tx_history = trading_api.get_transactions_history(
            transaction_request=HistoryRequest(from_date=date(2005, 1, 1), to_date=date.today()),
            raw=True,
        )
        tx_items = (tx_history or {}).get("data") or []
        if not tx_items:
            return jsonify({"series": [], "positions": [], "note": "Keine Transaktionen gefunden"})

        by_product: dict[str, list[dict]] = {}
        for t in tx_items:
            pid = str(t.get("productId"))
            by_product.setdefault(pid, []).append(t)

        earliest = min(date.fromisoformat(str(t.get("date"))[:10]) for t in tx_items)

        account_update = trading_api.get_update(
            request_list=[UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0)],
            raw=True,
        )
        current_positions = _flatten_rows((account_update.get("portfolio") or {}).get("value") or [])
        current_ids = {str(p.get("id")) for p in current_positions if _num(p.get("size")) != 0}

        product_ids = [int(pid) for pid in by_product if pid.isdigit()]
        info_raw = trading_api.get_products_info(product_list=product_ids, raw=True) if product_ids else {}
        product_info = (info_raw or {}).get("data", {})

        cash_series = _get_cash_history_chf(trading_api, base_currency, earliest)

        per_product_meta = []
        daily_value_chf: dict[str, float] = {}

        for pid, txs in by_product.items():
            if pid not in current_ids:
                continue  # nur aktuell gehaltene Positionen fliessen in "seit Kauf" ein
            info = product_info.get(pid, {})
            vwd_id = info.get("vwdId")
            currency = info.get("currency") or base_currency
            first_date = min(date.fromisoformat(str(t.get("date"))[:10]) for t in txs)

            qty_by_day: dict[str, float] = {}
            running = 0.0
            for t in sorted(txs, key=lambda x: str(x.get("date"))):
                d = str(t.get("date"))[:10]
                signed_qty = _num(t.get("quantity")) * (1 if t.get("buysell") == "B" else -1)
                running += signed_qty
                qty_by_day[d] = running

            price_series, source, verified, series_currency = resolve_price_series(
                SESSION["user_token"], vwd_id, info.get("isin"), info.get("symbol"), currency,
                info.get("closePrice"), str(info.get("closePriceDate") or "")[:10], first_date,
            )
            usable = source == "yahoo" or (source == "degiro" and verified)

            fx_series = get_fx_series(first_date, date.today(), series_currency, TARGET_CURRENCY)
            fallback_price = _num(info.get("closePrice")) or 0.0

            qty_running = 0.0
            price_running = fallback_price
            d = first_date
            while d <= date.today():
                iso = d.isoformat()
                if iso in qty_by_day:
                    qty_running = qty_by_day[iso]
                if usable and iso in price_series:
                    price_running = price_series[iso]
                fx = fx_rate_on(fx_series, d, fallback=1.0)
                value_chf = qty_running * price_running * fx
                daily_value_chf[iso] = daily_value_chf.get(iso, 0.0) + value_chf
                d += timedelta(days=1)

            per_product_meta.append({
                "productId": pid,
                "name": info.get("name"),
                "source": source,
                "verified": verified,
                "since": first_date.isoformat(),
            })

        filled_cash = _forward_fill(cash_series, earliest, date.today()) if cash_series else []
        cash_by_day = {c["date"]: c["value"] for c in filled_cash}

        series = []
        d = earliest
        while d <= date.today():
            iso = d.isoformat()
            positions_value = round(daily_value_chf.get(iso, 0.0), 2)
            cash_v = cash_by_day.get(iso)
            series.append({
                "date": iso,
                "positionsValueChf": positions_value,
                "cashChf": round(cash_v, 2) if cash_v is not None else None,
                "equityChf": round(positions_value + (cash_v or 0.0), 2),
            })
            d += timedelta(days=1)

        return jsonify({
            "since": earliest.isoformat(),
            "series": series,
            "positions": per_product_meta,
            "note": "source=degiro: DEGIRO-Kurschart (verifiziert). source=yahoo: Kursdaten von Yahoo Finance bezogen, da DEGIROs Chart nicht abrufbar/verifizierbar war. source=none: kein Kursverlauf gefunden, es wird ein Naeherungswert (letzter bekannter Kurs) verwendet.",
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler bei der Historien-Rekonstruktion")
        return jsonify({"error": f"Fehler bei der Historien-Rekonstruktion: {e}"}), 502


@app.route("/api/position-history", methods=["GET"])
@require_auth
@require_login
def position_history():
    trading_api = SESSION["trading_api"]
    product_id = request.args.get("productId")
    if not product_id:
        return jsonify({"error": "productId erforderlich"}), 400

    try:
        info_raw = trading_api.get_products_info(product_list=[int(product_id)], raw=True)
        info = ((info_raw or {}).get("data") or {}).get(str(product_id), {})
        vwd_id = info.get("vwdId")
        currency = info.get("currency") or SESSION["base_currency"] or "EUR"

        tx_history = trading_api.get_transactions_history(
            transaction_request=HistoryRequest(from_date=date(2005, 1, 1), to_date=date.today()),
            raw=True,
        )
        tx_items = [t for t in ((tx_history or {}).get("data") or []) if str(t.get("productId")) == str(product_id)]
        first_date = min((date.fromisoformat(str(t.get("date"))[:10]) for t in tx_items), default=date.today() - timedelta(days=365))

        price_series, source, verified, series_currency = resolve_price_series(
            SESSION["user_token"], vwd_id, info.get("isin"), info.get("symbol"), currency,
            info.get("closePrice"), str(info.get("closePriceDate") or "")[:10], first_date,
        )
        usable = source == "yahoo" or (source == "degiro" and verified)
        if not usable:
            price_series = {}

        fx_series = get_fx_series(first_date, date.today(), series_currency, TARGET_CURRENCY)
        series = []
        for d_str, price in sorted(price_series.items()):
            d = date.fromisoformat(d_str)
            fx = fx_rate_on(fx_series, d, fallback=1.0)
            series.append({"date": d_str, "priceNative": price, "priceChf": round(price * fx, 4)})

        notes = {
            "degiro": "Kursverlauf von DEGIRO, gegen den gemeldeten Schlusskurs validiert.",
            "yahoo": "DEGIROs Kurschart war nicht verfuegbar/verifizierbar - Kursdaten stattdessen von Yahoo Finance bezogen"
                     + ("." if verified else " (auch dort nicht exakt gegen DEGIRO validierbar, kleine Abweichungen moeglich)."),
            "none": "Kein historischer Kursverlauf gefunden (weder DEGIRO noch Yahoo Finance). Ab heute wird lokal exakt weitergefuehrt.",
        }

        return jsonify({
            "productId": product_id,
            "name": info.get("name"),
            "symbol": info.get("symbol"),
            "isin": info.get("isin"),
            "exchangeId": info.get("exchangeId"),
            "productType": info.get("productType"),
            "currency": series_currency,
            "source": source,
            "verified": verified,
            "since": first_date.isoformat(),
            "series": series,
            "note": notes[source],
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen der Positions-Historie")
        return jsonify({"error": f"Fehler beim Abrufen der Positions-Historie: {e}"}), 502


def main():
    parser = argparse.ArgumentParser(description="Lokaler DEGIRO-Proxy fuer das degiro-akad Dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--origin",
        action="append",
        default=None,
        help="Erlaubter Origin der Webseite (mehrfach angebbar). Standard: https://mattieddie.github.io",
    )
    args = parser.parse_args()

    global ALLOWED_ORIGINS
    ALLOWED_ORIGINS = args.origin or [
        "https://mattieddie.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    print("=" * 70)
    print(" DEGIRO-Proxy laeuft lokal. Nichts wird gespeichert oder an GitHub gesendet.")
    print(" Waehrungsumrechnung nach CHF via api.frankfurter.app (oeffentliche EZB-Kurse).")
    print(f" Erlaubte Herkunft(en): {', '.join(ALLOWED_ORIGINS)}")
    print(f" Proxy-Token (in der Webseite eintragen): {PROXY_TOKEN}")
    print(f" Adresse: http://127.0.0.1:{args.port}")
    print(" Beenden mit Strg+C")
    print("=" * 70)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
