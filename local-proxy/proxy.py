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
import os
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
    "fmp_api_key": None,
}

# Nur fuer die Dauer des laufenden Prozesses im RAM - kein Zugriff auf Disk.
FX_SERIES_CACHE: dict[tuple, dict[str, float]] = {}
CHART_CACHE: dict[tuple, dict] = {}

TARGET_CURRENCY = "CHF"

# Optionale dritte Kursquelle (nach DEGIRO und Yahoo). Wird NUR aus der
# Umgebungsvariable gelesen - der Key wird hier absichtlich nie hartcodiert
# oder sonst irgendwo abgelegt. Setze ihn vor dem Start selbst, z.B.:
#   PowerShell:  $env:FMP_API_KEY = "dein-key"; python proxy.py
#   bash:        FMP_API_KEY=dein-key python proxy.py
FMP_API_KEY = os.environ.get("FMP_API_KEY")

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
    date_window_days: int = 2,
) -> bool:
    """
    Prueft, ob series_map am reference_date (oder bis zu date_window_days
    davor/danach - Anbieter koennen bei Wochenenden/Feiertagen leicht
    unterschiedliche letzte Handelstage melden) einen Preis nahe
    reference_price hat.
    """
    if not series_map or reference_price is None or not reference_date:
        return False
    try:
        ref_day = date.fromisoformat(reference_date)
    except ValueError:
        return False
    tol = max(0.01, reference_price * tolerance_pct)
    for offset in range(0, date_window_days + 1):
        candidates = {ref_day} if offset == 0 else {ref_day - timedelta(days=offset), ref_day + timedelta(days=offset)}
        for d in candidates:
            got = series_map.get(d.isoformat())
            if got is not None and abs(got - reference_price) <= tol:
                return True
    return False


def price_sanity_ok(
    series_map: dict[str, float],
    reference_price: float | None,
    reference_date: str | None,
    max_ratio: float = 3.0,
    date_window_days: int = 2,
) -> bool:
    """
    Lockere Plausibilitaetspruefung (Verhaeltnis statt exakter Toleranz):
    verwirft nur grobe Fehler - falsches Instrument, falsche Waehrungseinheit
    (z.B. GBX statt GBP), Chart-Decoding-Fehler - nicht kleine, normale
    Abweichungen zwischen Datenanbietern (Dividenden-Anpassung, Handelsschluss-
    Zeitpunkt). Der Kurs am (nahen) Referenzdatum darf hoechstens um den
    Faktor max_ratio ueber oder unter DEGIROs gemeldetem Schlusskurs liegen.
    """
    if not series_map or not reference_price or reference_price <= 0 or not reference_date:
        return False
    try:
        ref_day = date.fromisoformat(reference_date)
    except ValueError:
        return False
    for offset in range(0, date_window_days + 1):
        candidates = {ref_day} if offset == 0 else {ref_day - timedelta(days=offset), ref_day + timedelta(days=offset)}
        for d in candidates:
            got = series_map.get(d.isoformat())
            if got is not None and got > 0:
                ratio = got / reference_price
                if (1 / max_ratio) <= ratio <= max_ratio:
                    return True
    return False


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


def get_fmp_api_key() -> str | None:
    """
    Beim Verbinden ueber die Webseite eingegebener Key (nur im RAM, pro
    Sitzung) hat Vorrang; sonst Fallback auf die Umgebungsvariable
    FMP_API_KEY, falls beim Start von proxy.py gesetzt.
    """
    return SESSION.get("fmp_api_key") or FMP_API_KEY


def fmp_resolve_symbol(isin: str | None) -> str | None:
    api_key = get_fmp_api_key()
    if not api_key or not isin:
        return None
    try:
        resp = requests.get(
            "https://financialmodelingprep.com/stable/search-isin",
            params={"isin": isin, "apikey": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0].get("symbol") or data[0].get("ticker")
    except Exception:  # noqa: BLE001
        logger.warning("FMP-ISIN-Suche fuer %s fehlgeschlagen", isin, exc_info=True)
    return None


def fetch_fmp_daily_close_series(isin: str | None, since_d: date) -> tuple[dict[str, float], str | None]:
    """Liefert (Serie {ISO-Datum: Schlusskurs}, Waehrung falls von FMP gemeldet)."""
    api_key = get_fmp_api_key()
    if not api_key:
        return {}, None
    symbol = fmp_resolve_symbol(isin)
    if not symbol:
        return {}, None

    cache_key = ("fmp", symbol, since_d.isoformat())
    if cache_key in CHART_CACHE:
        return CHART_CACHE[cache_key]

    series_map: dict[str, float] = {}
    currency = None
    try:
        resp = requests.get(
            "https://financialmodelingprep.com/stable/historical-price-eod/full",
            params={"symbol": symbol, "from": since_d.isoformat(), "to": date.today().isoformat(), "apikey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("historical") if isinstance(data, dict) else data
        for row in rows or []:
            d = row.get("date")
            c = row.get("close")
            if d and c is not None:
                series_map[str(d)[:10]] = float(c)
    except Exception:  # noqa: BLE001
        logger.warning("FMP-Kursdaten fuer Symbol=%s nicht abrufbar", symbol, exc_info=True)
        series_map = {}

    result = (series_map, currency)
    CHART_CACHE[cache_key] = result
    return result


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
    1. DEGIRO-Kurschart, falls Daten vorhanden.
    2. Yahoo Finance (oeffentlich, ueber ISIN aufgeloest), falls DEGIRO nichts liefert.
    3. Financial Modeling Prep (nur falls FMP_API_KEY gesetzt), falls auch Yahoo nichts liefert.
    4. Nichts gefunden.

    "verified" ist nur noch eine Info-Kennzeichnung (exakter Abgleich gegen
    DEGIROs separat gemeldeten Schlusskurs) und entscheidet NICHT mehr allein,
    ob die Serie verwendet wird - kleine, normale Abweichungen zwischen
    Anbietern (Datum/Uhrzeit-Cutoff, Dividenden-Anpassung) sollen nicht mehr
    zur Ablehnung fuehren. Als Gate dient stattdessen price_sanity_ok() mit
    einem groben Faktor-3-Rahmen: das faengt weiterhin ein komplett falsches
    Instrument oder eine falsche Waehrungseinheit ab (z.B. der Fall, der zu
    einer ~6.5x ueberhoehten Portfolio-Rekonstruktion gefuehrt hat, als die
    Verifizierung testweise ganz entfernt war), lehnt aber keine Serie mehr
    wegen einer harmlosen kleinen Kursabweichung ab.
    """
    if vwd_id:
        degiro_series = fetch_daily_close_series(user_token, vwd_id, since_d)
        if degiro_series and price_sanity_ok(degiro_series, close_price, close_price_date):
            verified = verify_series(degiro_series, close_price, close_price_date)
            return degiro_series, "degiro", verified, currency

    yahoo_series, yahoo_ccy = fetch_yahoo_daily_close_series(isin, symbol, since_d)
    if yahoo_series and price_sanity_ok(yahoo_series, close_price, close_price_date):
        verified = verify_series(yahoo_series, close_price, close_price_date, tolerance_pct=0.03)
        return yahoo_series, "yahoo", verified, (yahoo_ccy or currency)

    if get_fmp_api_key():
        fmp_series, fmp_ccy = fetch_fmp_daily_close_series(isin, since_d)
        if fmp_series and price_sanity_ok(fmp_series, close_price, close_price_date):
            verified = verify_series(fmp_series, close_price, close_price_date, tolerance_pct=0.03)
            return fmp_series, "fmp", verified, (fmp_ccy or currency)

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
        "fmpConfigured": bool(get_fmp_api_key()),
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
    fmp_api_key = body.get("fmp_api_key") or None

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
        # Nur im RAM fuer die Dauer dieser Sitzung - nie auf Platte geschrieben.
        # Ueberschreibt bewusst nicht mit None, falls dieses Login-Formular
        # ohne Key abgeschickt wurde, aber vorher schon einer per Umgebungs-
        # variable galt (siehe get_fmp_api_key()).
        if fmp_api_key:
            SESSION["fmp_api_key"] = fmp_api_key

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
        "base_currency": None, "logged_in_at": None, "fmp_api_key": None,
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

def _get_cash_and_deposits_history_chf(
    trading_api, since_d: date, live_cash_chf: float | None, tx_items: list[dict], base_currency: str
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Liefert (Cash-Historie, kumulierte Netto-Einzahlungs-Historie), beide
    taeglich in CHF.

    Cash-Historie: letzter gemeldeter Saldo je Tag aus DEGIROs eigenen
    'cashMovements'. Diese Rohsumme misst etwas anderes als das separat live
    abgefragte "available to trade"-Guthaben (cashFunds) - z.B. reservierte /
    noch nicht abgewickelte Betraege, Sollsalden in einer Fremdwaehrung o.ae.
    Ein direkter Wertabgleich beider Groessen schlaegt deshalb strukturell
    fast immer fehl, selbst wenn die Rekonstruktion an sich korrekt ist (das
    war der Grund, warum diese Funktion vorher fuer dieses Konto dauerhaft
    leer zurueckgab und die einzahlungsbereinigte %-Performance nie etwas
    anzeigte). Statt bei Abweichung zu verwerfen, wird die rekonstruierte
    Tag-zu-Tag-FORM beibehalten und die gesamte Serie um eine Konstante
    verschoben, sodass sie am letzten bekannten Tag exakt dem live
    abgefragten Wert entspricht - das verankert die Kurve korrekt, ohne die
    relative Bewegung (aus der Ein-/Auszahlungen erkannt werden) zu verlieren.

    Netto-Einzahlungen: NICHT durch Klassifizieren einzelner Kontobuchungen
    bestimmt (zwei Anlaeufe damit - ueber 'type' geraten, dann ueber
    Anwesenheit von 'productId' - waren beide nachweislich falsch: manche
    Einzahlungen/Sweeps in den Cash-Fund tragen offenbar auch eine productId
    und wurden faelschlich als Handel statt als Einzahlung gezaehlt, was die
    Kennzahl bei jeder so verpassten Einzahlung sprunghaft nach oben trieb).
    Stattdessen rein rechnerisch aus zwei bereits verlaesslichen Groessen
    hergeleitet:
        NettoEinzahlungen(t) = Cash-Saldo(t) - kumulierter Handels-Cashflow(t)
    Der Handels-Cashflow kommt direkt aus der Transaktionshistorie (bereits
    an anderer Stelle erfolgreich genutzt), nicht aus den mehrdeutigen
    Kontobuchungen. Diese Herleitung setzt voraus, dass der Cash-Saldo am
    allerersten Tag (since_d) noch keine Aktivität von vor since_d enthält -
    zutreffend, da since_d der Tag der ersten je getaetigten Transaktion ist.
    """
    fx_cache_series: dict[str, dict[str, float]] = {}

    def _to_chf(ccy, amount, day: date) -> float:
        if not isinstance(ccy, str) or len(ccy) != 3 or not ccy.isalpha():
            return 0.0
        ccy = ccy.upper()
        if ccy not in fx_cache_series:
            fx_cache_series[ccy] = get_fx_series(since_d, date.today(), ccy, TARGET_CURRENCY)
        return _num(amount) * fx_rate_on(fx_cache_series[ccy], day, fallback=1.0)

    try:
        overview = trading_api.get_account_overview(
            overview_request=OverviewRequest(from_date=since_d, to_date=date.today()),
            raw=True,
        )
        movements = ((overview or {}).get("data") or {}).get("cashMovements") or []
    except Exception:  # noqa: BLE001
        logger.warning("Kontoauszug (cashMovements) nicht abrufbar", exc_info=True)
        movements = []

    logger.info("cashMovements: %d Eintraege gefunden (seit %s)", len(movements), since_d.isoformat())

    # Pro Tag koennen mehrere Buchungen vorliegen (Zinsen, Gebuehren, Trades,
    # Ein-/Auszahlungen...); nicht jede davon traegt zwangslaeufig einen
    # vollstaendigen 'balance'-Schnappschuss (manche Buchungstypen liefern
    # dort vermutlich ein leeres/fehlendes Feld). Wird chronologisch einfach
    # die LETZTE Buchung des Tages genommen, kann das zufaellig eine ohne
    # Saldo sein, obwohl eine fruehere Buchung desselben Tages einen echten
    # Saldo hatte - das ergab beobachtet einen rekonstruierten Saldo von
    # exakt 0.00 an Tagen mit echter Kontoaktivitaet. Deshalb: je Tag die
    # letzte Buchung MIT einem nicht-leeren balance-Feld bevorzugen, nur bei
    # komplettem Fehlen eine balance-lose Buchung als Fallback nehmen.
    by_day_last: dict[str, dict] = {}
    for m in sorted(movements, key=lambda m: str(m.get("date") or "")):
        d = str(m.get("date") or "")[:10]
        if not d:
            continue
        balance = m.get("balance")
        if isinstance(balance, dict) and balance:
            by_day_last[d] = m
        elif d not in by_day_last:
            by_day_last[d] = m

    cash_result: dict[str, float] = {}
    empty_balance_days = 0
    for d, movement in sorted(by_day_last.items()):
        balance = movement.get("balance") or {}
        if not balance:
            empty_balance_days += 1
        day = date.fromisoformat(d)
        cash_result[d] = sum(_to_chf(ccy, amount, day) for ccy, amount in balance.items())
    if empty_balance_days:
        logger.info(
            "cashMovements: %d von %d Tagen ohne balance-Feld in irgendeiner Buchung dieses Tages",
            empty_balance_days, len(by_day_last),
        )

    if cash_result and live_cash_chf is not None:
        last_day = max(cash_result)
        shift = live_cash_chf - cash_result[last_day]
        if abs(shift) > 1e-6:
            logger.info(
                "Cash-Historie kalibriert: rekonstruiert(%s)=%.2f, live=%.2f, Verschiebung=%.2f",
                last_day, cash_result[last_day], live_cash_chf, shift,
            )
            cash_result = {d: v + shift for d, v in cash_result.items()}

    if not cash_result:
        return {}, {}

    trade_cashflow_by_day: dict[str, float] = {}
    running = 0.0
    for t in sorted(tx_items, key=lambda x: str(x.get("date"))):
        d = str(t.get("date"))[:10]
        amount = t.get("totalPlusAllFeesInBaseCurrency")
        if amount is None:
            amount = t.get("totalInBaseCurrency")
        day = date.fromisoformat(d)
        running += _to_chf(base_currency, amount, day)
        trade_cashflow_by_day[d] = running

    filled_trade_cf = _forward_fill(trade_cashflow_by_day, since_d, date.today())
    trade_cf_by_day = {c["date"]: (c["value"] or 0.0) for c in filled_trade_cf}
    filled_cash = _forward_fill(cash_result, since_d, date.today())

    deposits_result: dict[str, float] = {}
    for c in filled_cash:
        d = c["date"]
        if c["value"] is None:
            continue
        deposits_result[d] = c["value"] - trade_cf_by_day.get(d, 0.0)

    return cash_result, deposits_result


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
            request_list=[
                UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0),
                UpdateRequest(option=UpdateOption.CASH_FUNDS, last_updated=0),
            ],
            raw=True,
        )
        current_positions = _flatten_rows((account_update.get("portfolio") or {}).get("value") or [])
        current_ids = {str(p.get("id")) for p in current_positions if _num(p.get("size")) != 0}

        product_ids = [int(pid) for pid in by_product if pid.isdigit()]
        info_raw = trading_api.get_products_info(product_list=product_ids, raw=True) if product_ids else {}
        product_info = (info_raw or {}).get("data", {})

        live_cash_amount, live_cash_ccy = _get_cash_available_to_trade(account_update)
        live_cash_chf = live_cash_amount * get_latest_fx_rate(live_cash_ccy, TARGET_CURRENCY)

        cash_series, deposits_series = _get_cash_and_deposits_history_chf(
            trading_api, earliest, live_cash_chf, tx_items, base_currency
        )
        if not cash_series:
            # Rekonstruktion nicht verfuegbar/unplausibel - flache Linie mit dem
            # tatsaechlichen aktuellen Cash-Stand ist ehrlicher als eine falsche Kurve.
            cash_series = {earliest.isoformat(): live_cash_chf, date.today().isoformat(): live_cash_chf}

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
                # DEGIROs "quantity" ist bereits vorzeichenbehaftet (negativ bei
                # Verkauf) - NICHT zusaetzlich ueber buysell umdrehen, sonst wird
                # ein Verkauf faelschlich als weiterer Kauf gezaehlt.
                running += _num(t.get("quantity"))
                qty_by_day[d] = running

            price_series, source, verified, series_currency = resolve_price_series(
                SESSION["user_token"], vwd_id, info.get("isin"), info.get("symbol"), currency,
                info.get("closePrice"), str(info.get("closePriceDate") or "")[:10], first_date,
            )
            # Jede gefundene Serie wird verwendet, unabhaengig davon, ob sie
            # exakt gegen DEGIROs gemeldeten Schlusskurs passt (auf Wunsch -
            # kleine anbieterbedingte Abweichungen sind normal). Schutz gegen
            # grob falsch skalierte Daten bleibt der Tag-zu-Tag-Ausreisserfilter
            # weiter unten.
            usable = source != "none"

            fx_series = get_fx_series(first_date, date.today(), series_currency, TARGET_CURRENCY)
            fallback_price = _num(info.get("closePrice")) or 0.0
            # Mit dem aeltesten bekannten Kurs starten, nicht mit dem heutigen -
            # sonst wuerde bei stark gewachsenen Positionen der Ausreisserfilter
            # unten den allerersten echten Kurs faelschlich verwerfen.
            initial_price = price_series[min(price_series.keys())] if (usable and price_series) else fallback_price

            qty_running = 0.0
            prev_qty = 0.0
            price_running = initial_price
            d = first_date
            while d <= date.today():
                iso = d.isoformat()
                if iso in qty_by_day:
                    qty_running = qty_by_day[iso]
                if usable and iso in price_series:
                    candidate = price_series[iso]
                    # Ausreisser-Schutz: ein einzelner Tag, der um mehr als das
                    # 5-fache vom letzten bekannten Kurs abweicht, ist praktisch
                    # immer ein Datenfehler (falsches Symbol, GBX/GBP-Verwechslung,
                    # Chart-Decoding) statt eine echte Kursbewegung - verwerfen.
                    # Ausnahme: direkt nach einer Phase mit 0 gehaltenen Stuecken
                    # (Position komplett verkauft und spaeter neu gekauft) wird
                    # IMMER frisch verankert - sonst kann ein waehrend der
                    # Halte-Pause aufgetretener echter Kurssprung (Split, laengere
                    # Erholung/Einbruch) faelschlich als Ausreisser verworfen
                    # bleiben und den Kurs fuer die gesamte Historie nach dem
                    # Wiedereinstieg dauerhaft auf dem alten Stand einfrieren.
                    if prev_qty == 0 or price_running <= 0 or 0.2 <= (candidate / price_running) <= 5:
                        price_running = candidate
                fx = fx_rate_on(fx_series, d, fallback=1.0)
                value_chf = qty_running * price_running * fx
                daily_value_chf[iso] = daily_value_chf.get(iso, 0.0) + value_chf
                prev_qty = qty_running
                d += timedelta(days=1)

            per_product_meta.append({
                "productId": pid,
                "name": info.get("name"),
                "source": source,
                "verified": verified,
                "used": usable,
                "since": first_date.isoformat(),
            })

        filled_cash = _forward_fill(cash_series, earliest, date.today()) if cash_series else []
        cash_by_day = {c["date"]: c["value"] for c in filled_cash}
        filled_deposits = _forward_fill(deposits_series, earliest, date.today()) if deposits_series else []
        deposits_by_day = {c["date"]: c["value"] for c in filled_deposits}

        series = []
        d = earliest
        while d <= date.today():
            iso = d.isoformat()
            positions_value = round(daily_value_chf.get(iso, 0.0), 2)
            cash_v = cash_by_day.get(iso)
            deposits_v = deposits_by_day.get(iso)
            series.append({
                "date": iso,
                "positionsValueChf": positions_value,
                "cashChf": round(cash_v, 2) if cash_v is not None else None,
                "equityChf": round(positions_value + (cash_v or 0.0), 2),
                "netDepositsChf": round(deposits_v, 2) if deposits_v is not None else None,
            })
            d += timedelta(days=1)

        return jsonify({
            "since": earliest.isoformat(),
            "series": series,
            "positions": per_product_meta,
            "note": "used=true: echter Kursverlauf verwendet (source=degiro/yahoo/fmp; verified zeigt nur informativ an, ob er exakt zu DEGIROs gemeldetem Schlusskurs passt - entscheidet aber nicht mehr, ob er verwendet wird). used=false: keine der drei Quellen lieferte ueberhaupt Daten, es wird ein Naeherungswert (letzter bekannter Kurs, flach) verwendet. netDepositsChf: kumulierte Netto-Einzahlungen (Basis der einzahlungsbereinigten %-Performance).",
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
        # Jede gefundene Serie wird angezeigt, unabhaengig davon, ob sie exakt
        # gegen DEGIROs gemeldeten Schlusskurs passt (auf Wunsch).
        usable = source != "none"
        if not usable:
            price_series = {}

        fx_series = get_fx_series(first_date, date.today(), series_currency, TARGET_CURRENCY)
        series = []
        for d_str, price in sorted(price_series.items()):
            d = date.fromisoformat(d_str)
            fx = fx_rate_on(fx_series, d, fallback=1.0)
            series.append({"date": d_str, "priceNative": price, "priceChf": round(price * fx, 4)})

        if usable:
            verified_txt = "gegen DEGIROs gemeldeten Schlusskurs abgeglichen" if verified else "nicht exakt gegen DEGIROs Schlusskurs abgleichbar, trotzdem angezeigt"
            notes = {
                "degiro": f"Kursverlauf von DEGIRO ({verified_txt}).",
                "yahoo": f"Kursdaten von Yahoo Finance bezogen ({verified_txt}).",
                "fmp": f"Kursdaten von Financial Modeling Prep bezogen ({verified_txt}).",
            }
            note = notes[source]
        else:
            note = (
                "Kein verifizierter Kursverlauf gefunden (weder DEGIRO noch Yahoo Finance lieferten Daten, "
                "die zum von DEGIRO gemeldeten Schlusskurs passen) - es wird kein historischer Kurs angezeigt. "
                "Ab heute wird lokal exakt weitergefuehrt."
            )

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
            "used": usable,
            "since": first_date.isoformat(),
            "series": series,
            "note": note,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen der Positions-Historie")
        return jsonify({"error": f"Fehler beim Abrufen der Positions-Historie: {e}"}), 502


@app.route("/api/closed-positions", methods=["GET"])
@require_auth
@require_login
def closed_positions():
    """
    Realisiertes G/V vergangener, bereits verkaufter Positionen - reine
    Buchhaltung aus der Transaktionshistorie (kein Kurschart noetig, daher
    exakt statt Naeherung): Summe aller Cashflows (inkl. Gebuehren) je
    Produkt, das aktuell nicht mehr gehalten wird.
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
            return jsonify({"positions": []})

        by_product: dict[str, list[dict]] = {}
        for t in tx_items:
            pid = str(t.get("productId"))
            by_product.setdefault(pid, []).append(t)

        account_update = trading_api.get_update(
            request_list=[UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0)],
            raw=True,
        )
        current_positions = _flatten_rows((account_update.get("portfolio") or {}).get("value") or [])
        current_ids = {str(p.get("id")) for p in current_positions if _num(p.get("size")) != 0}

        closed_ids = [pid for pid in by_product if pid.isdigit() and pid not in current_ids]
        if not closed_ids:
            return jsonify({"positions": []})

        info_raw = trading_api.get_products_info(product_list=[int(p) for p in closed_ids], raw=True)
        product_info = (info_raw or {}).get("data", {})

        fx_series_cache: dict[str, float] = {}
        if base_currency != TARGET_CURRENCY:
            fx_series_cache = get_fx_series(date(2005, 1, 1), date.today(), base_currency, TARGET_CURRENCY)

        def to_chf(amount, day: date) -> float:
            if base_currency == TARGET_CURRENCY:
                return _num(amount)
            return _num(amount) * fx_rate_on(fx_series_cache, day, fallback=1.0)

        results = []
        for pid in closed_ids:
            txs = sorted(by_product[pid], key=lambda x: str(x.get("date")))
            info = product_info.get(pid, {})

            realized_pl_chf = 0.0
            invested_chf = 0.0
            proceeds_chf = 0.0
            for t in txs:
                day = date.fromisoformat(str(t.get("date"))[:10])
                amount = t.get("totalPlusAllFeesInBaseCurrency")
                if amount is None:
                    amount = t.get("totalInBaseCurrency")
                amount_chf = to_chf(amount, day)
                realized_pl_chf += amount_chf
                if t.get("buysell") == "B":
                    invested_chf += -amount_chf  # Kauf: amount ist negativ (Cash-Abfluss) -> Investition positiv
                else:
                    proceeds_chf += amount_chf

            pl_pct = (realized_pl_chf / invested_chf * 100) if invested_chf > 0 else None

            results.append({
                "productId": pid,
                "name": info.get("name"),
                "symbol": info.get("symbol"),
                "isin": info.get("isin"),
                "currency": info.get("currency"),
                "firstDate": str(txs[0].get("date"))[:10],
                "lastDate": str(txs[-1].get("date"))[:10],
                "investedChf": round(invested_chf, 2),
                "proceedsChf": round(proceeds_chf, 2),
                "realizedPlChf": round(realized_pl_chf, 2),
                "realizedPlPct": round(pl_pct, 2) if pl_pct is not None else None,
                "transactionCount": len(txs),
            })

        results.sort(key=lambda r: r["lastDate"], reverse=True)
        return jsonify({"positions": results})
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen der geschlossenen Positionen")
        return jsonify({"error": f"Fehler beim Abrufen der geschlossenen Positionen: {e}"}), 502


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
    print(f" Kursquellen: DEGIRO, Yahoo Finance, Financial Modeling Prep ({'aktiv' if FMP_API_KEY else 'kein FMP_API_KEY gesetzt - uebersprungen'})")
    print(f" Erlaubte Herkunft(en): {', '.join(ALLOWED_ORIGINS)}")
    print(f" Proxy-Token (in der Webseite eintragen): {PROXY_TOKEN}")
    print(f" Adresse: http://127.0.0.1:{args.port}")
    print(" Beenden mit Strg+C")
    print("=" * 70)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
