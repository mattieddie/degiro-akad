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
SECTOR_CACHE: dict[str, dict] = {}

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
    # Bis zu 3 Versuche: die kostenlose oeffentliche API antwortet gelegentlich
    # mit einem einzelnen Timeout/Verbindungsfehler, der kurz danach schon
    # wieder verschwunden ist (beobachtet waehrend Tests). Ohne Retry wuerde
    # ein einzelner Aussetzer sofort auf den 1:1-Fallback zurueckfallen -
    # das reicht z.B. bei der Waehrungserkennung (siehe login()) aus, um
    # JEDEN Kandidaten faelschlich als unplausibel zu verwerfen.
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.5)
        try:
            resp = requests.get(
                f"https://api.frankfurter.app/{start_d.isoformat()}..{end_d.isoformat()}",
                params={"from": from_ccy, "to": to_ccy},
                timeout=15,
            )
            resp.raise_for_status()
            rates = resp.json().get("rates", {})
            series = {d: v[to_ccy] for d, v in rates.items() if to_ccy in v}
            FX_SERIES_CACHE[key] = series
            return series
        except Exception as e:  # noqa: BLE001
            last_error = e
    # Bewusst NICHT cachen: ein anhaltender Fehler soll beim naechsten Aufruf
    # erneut versucht werden, statt fuer den Rest der laufenden Sitzung als
    # "leere Serie" (1:1-Fallback) haengenzubleiben.
    logger.warning("FX-Abruf %s->%s fehlgeschlagen nach 3 Versuchen (%s), verwende 1:1", from_ccy, to_ccy, last_error)
    return {}


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
    """
    "baseCurrency" ist DEGIROs tatsaechliche Konto-/Reporting-Waehrung - in
    dieser Waehrung sind sowohl das "value"-Feld jeder Portfolio-Position
    (siehe portfolio()) als auch alle "...InBaseCurrency"-Felder der
    Transaktionshistorie bereits angegeben. "currency" ist dagegen offenbar
    nur eine Kursanzeige-Praeferenz (bei diesem Konto z.B. "EUR", obwohl die
    tatsaechliche Basiswaehrung CHF ist) und wurde vorher faelschlich zuerst
    verwendet - das fuehrte zu einer doppelten (zu tiefen) Umrechnung bei
    Positionswert, realisiertem G/V, Transaktionsgebuehren und
    Netto-Einzahlungen, bestaetigt anhand des vom Nutzer separat
    verifizierten tatsaechlichen Gesamtwerts.
    """
    data = (client_details or {}).get("data", {})
    return data.get("baseCurrency") or data.get("currency") or "EUR"


def _verify_base_currency(candidate: str, positions: list[dict]) -> bool:
    """
    Prueft `candidate` gegen DEGIROs eigenes "value"-Feld je Position: dieses
    sollte ungefaehr size * price * fx_rate(Positionswaehrung -> candidate)
    ergeben (siehe detect_base_currency()). Toleranz +-5%, um leichten
    Zeitversatz zwischen Positions- und Tageskurs zuzulassen. Verlangt
    mindestens eine pruefbare Position (Fremdwaehrung ungleich candidate,
    da sich Positionen in candidate selbst nicht von anderen Kandidaten
    unterscheiden liessen) und dass ALLE pruefbaren Positionen passen -
    ein einzelner Ausreisser genuegt, um den Kandidaten zu verwerfen.
    """
    checked = 0
    for p in positions:
        currency = p.get("currency")
        size = _num(p.get("size"))
        price = _num(p.get("price"))
        raw_value = p.get("value")
        if not currency or currency == candidate or size == 0 or price == 0 or raw_value is None:
            continue
        native_value = size * price
        expected_rate = get_latest_fx_rate(currency, candidate)
        if native_value == 0 or expected_rate == 0:
            continue
        implied_rate = _num(raw_value) / native_value
        if abs(implied_rate - expected_rate) / expected_rate > 0.05:
            return False
        checked += 1
    return checked > 0


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


# quoteSummary (Sektor/Branche) verlangt inzwischen, anders als Suche und
# Kurschart oben, ein gueltiges Cookie + "Crumb"-Token (CSRF-aehnlicher Schutz,
# den Yahoo seit einiger Zeit fuer diesen speziellen Endpunkt durchsetzt).
# Wird einmal pro Prozess geholt und wiederverwendet; bei Ablauf/401 einmalig
# neu geholt.
_yahoo_session: requests.Session | None = None
_yahoo_crumb: str | None = None


def _get_yahoo_crumb(force_refresh: bool = False) -> tuple[requests.Session, str | None]:
    global _yahoo_session, _yahoo_crumb
    if _yahoo_session is None:
        _yahoo_session = requests.Session()
        _yahoo_session.headers.update(YAHOO_HEADERS)
    if _yahoo_crumb is None or force_refresh:
        try:
            _yahoo_session.get("https://fc.yahoo.com", timeout=10)
            resp = _yahoo_session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
            resp.raise_for_status()
            _yahoo_crumb = resp.text.strip() or None
        except Exception:  # noqa: BLE001
            logger.warning("Yahoo-Crumb-Abruf fehlgeschlagen", exc_info=True)
            _yahoo_crumb = None
    return _yahoo_session, _yahoo_crumb


def fetch_yahoo_sector(isin: str | None, fallback_symbol: str | None) -> dict:
    """
    Liefert {"sector": str|None, "industry": str|None, "quoteType": str|None}
    fuer eine Position, per Prozess-Cache (SECTOR_CACHE) ueber die Ziel-
    Waehrung/den Sitzungslauf hinweg wiederverwendet. Reine Aktien haben i.d.R.
    einen echten GICS-Sektor (z.B. "Real Estate"); ETFs/ETPs haben KEINEN
    Sektor im eigentlichen Sinn (Yahoo liefert dafuer ein leeres assetProfile),
    werden aber ueber "quoteType" als "ETF" erkennbar. Zertifikate/Hebel-
    produkte sind in Yahoo meist gar nicht auffindbar - dann bleibt alles None
    und das Frontend gruppiert sie unter "Unbekannt".
    """
    symbol = yahoo_resolve_symbol(isin, fallback_symbol)
    if not symbol:
        return {"sector": None, "industry": None, "quoteType": None}
    if symbol in SECTOR_CACHE:
        return SECTOR_CACHE[symbol]

    result = {"sector": None, "industry": None, "quoteType": None}
    for attempt in range(2):
        session, crumb = _get_yahoo_crumb(force_refresh=(attempt == 1))
        try:
            resp = session.get(
                f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                params={"modules": "assetProfile,quoteType", **({"crumb": crumb} if crumb else {})},
                timeout=15,
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # Crumb evtl. abgelaufen - einmal mit frischem Crumb erneut versuchen.
            resp.raise_for_status()
            entries = ((resp.json() or {}).get("quoteSummary") or {}).get("result") or []
            if entries:
                profile = entries[0].get("assetProfile") or {}
                quote_type = entries[0].get("quoteType") or {}
                result = {
                    "sector": profile.get("sector"),
                    "industry": profile.get("industry"),
                    "quoteType": quote_type.get("quoteType"),
                }
            break
        except Exception:  # noqa: BLE001
            logger.warning("Yahoo-Sektor fuer Symbol=%s nicht abrufbar", symbol, exc_info=True)
            break

    SECTOR_CACHE[symbol] = result
    return result


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
        # Provisorische Bestimmung der Konto-Basiswaehrung direkt beim Login.
        # client_details liefert bei manchen Konten weder "currency" noch
        # "baseCurrency" (beide None) - detect_base_currency() faellt dann
        # auf "EUR" zurueck, was nicht zwingend stimmt. Als bessere Vermutung
        # wird, falls vorhanden, die tatsaechlich genutzte CASH_FUNDS-Waehrung
        # verwendet (_get_cash_available_to_trade waehlt dafuer bewusst den
        # Betrag mit dem groessten CHF-Gegenwert unabhaengig vom Vorzeichen,
        # siehe dort - ein Margin-Debit ist genauso aussagekraeftig wie ein
        # Guthaben). Endgueltig VERIFIZIERT wird diese Vermutung erst in
        # portfolio() gegen DEGIROs eigenes "value"-Feld der tatsaechlich
        # gehaltenen Positionen (siehe _verify_base_currency) - direkt hier
        # beim Login sind die Positions-/Kursdaten im frischen get_update()
        # manchmal noch nicht vollstaendig synchronisiert, was die Pruefung
        # faelschlich fuer jeden Kandidaten scheitern liess.
        base_currency = detect_base_currency(client_details)
        try:
            cash_update = trading_api.get_update(
                request_list=[UpdateRequest(option=UpdateOption.CASH_FUNDS, last_updated=0)],
                raw=True,
            )
            _cash_amount, cash_currency = _get_cash_available_to_trade(cash_update)
            if cash_currency:
                base_currency = cash_currency
        except Exception:  # noqa: BLE001
            logger.warning("Konto-Waehrung ueber cashFunds nicht ermittelbar, verwende Fallback", exc_info=True)

        SESSION["base_currency"] = base_currency
        SESSION["logged_in_at"] = datetime.now().isoformat()
        logger.info(
            "Konto-Waehrung (provisorisch): client_details.currency=%s client_details.baseCurrency=%s -> verwendet=%s "
            "(wird beim ersten Portfolio-Abruf verifiziert)",
            client_details.get("data", {}).get("currency"),
            client_details.get("data", {}).get("baseCurrency"),
            SESSION["base_currency"],
        )
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
    mehrdeutigen totalPortfolio-Reportfeldern). DEGIRO listet dort IMMER eine
    Zeile pro unterstuetzter Waehrung, auch fuer Waehrungen, die dieses Konto
    nie genutzt hat (Betrag exakt 0) - nur die tatsaechlich genutzte(n)
    Waehrung(en) haben einen von 0 verschiedenen Betrag, der bei vollem
    Investitionsgrad/Margin-Debit auch NEGATIV sein kann (Konto schuldet
    Geld). Ein reiner Groessenvergleich der (nach CHF umgerechneten)
    signierten Betraege waere daher irrefuehrend: 0 > jeder negative Betrag,
    wodurch eine bedeutungslose 0-Platzhalterzeile faelschlich den echten
    (negativen) Saldo verdraengen wuerde - und darueber, weil dieselbe
    Waehrung auch als Konto-Basiswaehrung uebernommen wird (siehe login()),
    saemtliche CHF-Werte im Portfolio verzerrt. Deshalb wird nach dem
    CHF-Gegenwert mit dem groessten BETRAG (unabhaengig vom Vorzeichen)
    gewaehlt; zurueckgegeben wird weiterhin der native (ggf. negative)
    Betrag in seiner Original-Waehrung.
    """
    raw_cash = (account_update.get("cashFunds") or {}).get("value") or []
    rows = _flatten_rows(raw_cash)
    best = None
    best_chf = None
    for row in rows:
        amount = _num(row.get("value"))
        currency = row.get("currencyCode") or row.get("currency")
        if not currency:
            continue
        amount_chf = amount * get_latest_fx_rate(currency, TARGET_CURRENCY)
        if best is None or abs(amount_chf) > abs(best_chf):
            best = (amount, currency)
            best_chf = amount_chf
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

        # Verifiziert/korrigiert die beim Login nur provisorisch bestimmte
        # Basiswaehrung (siehe login()) gegen DEGIROs eigenes "value"-Feld
        # der jetzt zuverlaessig geladenen Positionen: value_native sollte
        # size * price * fx_rate(Positionswaehrung -> Basiswaehrung) ergeben
        # (siehe _verify_base_currency). Findet sich kein besserer Kandidat
        # als der aktuell gesetzte, bleibt dieser unveraendert - so wird eine
        # bereits korrekte, per client_details gelieferte Basiswaehrung nicht
        # grundlos durch einen zufaellig auch passenden Kandidaten ersetzt.
        if not _verify_base_currency(base_currency, positions):
            for candidate in ("CHF", "EUR", "USD", "GBP"):
                if candidate != base_currency and _verify_base_currency(candidate, positions):
                    logger.info("Basiswaehrung korrigiert: %s -> %s (gegen Positionswerte verifiziert)", base_currency, candidate)
                    base_currency = candidate
                    SESSION["base_currency"] = candidate
                    break

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

            fx = get_latest_fx_rate(currency, TARGET_CURRENCY)
            raw_value = p.get("value")
            if raw_value is not None:
                # DEGIROs eigenes "value"-Feld ist bereits in der Konto-
                # Basiswaehrung (base_currency, siehe detect_base_currency())
                # angegeben - NICHT in der Notierungswaehrung der Boerse
                # (p["currency"], z.B. EUR fuer eine an einer EUR-Boerse
                # gehandelte Position, waehrend die Basiswaehrung z.B. CHF
                # ist). Nochmals ueber den Boersen-FX-Kurs umzurechnen wuerde
                # diesen Wert ein zweites Mal (und damit zu tief) umrechnen -
                # bestaetigt, weil das genau den vom Nutzer separat
                # verifizierten Gesamtwert traf. Stattdessen nur ueber
                # Basiswaehrung->CHF umrechnen (meist ein No-Op).
                value_native = _num(raw_value)
                value_chf = value_native * get_latest_fx_rate(base_currency, TARGET_CURRENCY)
            else:
                value_native = size * price
                value_chf = value_native * fx
            avg_cost_chf = avg_price * fx
            # Unrealisierter G/V = Stueck * (aktueller Kurs - Einstandskurs), in Originalwaehrung,
            # dann nach CHF umgerechnet. (Vorher faelschlich das mehrdeutige "plBase"-Feld verwendet.)
            pl_unrealized_native = size * (price - avg_price) if avg_price else None
            pl_unrealized_chf = pl_unrealized_native * fx if pl_unrealized_native is not None else None
            pl_pct = ((price - avg_price) / avg_price * 100) if avg_price else None

            # Tages-G/V = Stueck * (aktueller Kurs - DEGIROs zuletzt gemeldetem
            # Schlusskurs "closePrice", derselbe Referenzwert wie bei der
            # Seit-Kauf-Rekonstruktion, siehe history_backfill()). Fehlt er
            # (z.B. Produkt ohne Kursdaten), bleibt der Tages-G/V unbekannt statt
            # mit einem falschen Wert (z.B. 0) aufzufuellen.
            raw_close_price = info.get("closePrice")
            close_price = float(raw_close_price) if isinstance(raw_close_price, (int, float)) else None
            pl_day_native = size * (price - close_price) if close_price else None
            pl_day_chf = pl_day_native * fx if pl_day_native is not None else None

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
                "closePrice": close_price,
                "value": value_native,
                "valueChf": value_chf,
                "plUnrealized": pl_unrealized_native,
                "plUnrealizedChf": pl_unrealized_chf,
                "plUnrealizedPct": pl_pct,
                "plDay": pl_day_native,
                "plDayChf": pl_day_chf,
            })

        equity_chf = total_value_chf + cash_chf
        pl_total_chf_values = [p["plUnrealizedChf"] for p in enriched if p["plUnrealizedChf"] is not None]
        pl_day_chf_values = [p["plDayChf"] for p in enriched if p["plDayChf"] is not None]
        pl_total_chf = sum(pl_total_chf_values) if pl_total_chf_values else None
        pl_day_chf_total = sum(pl_day_chf_values) if pl_day_chf_values else None

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
                "plTotalChf": pl_total_chf,
                "plDayChf": pl_day_chf_total,
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
    base_currency = SESSION["base_currency"] or "EUR"
    days = int(request.args.get("days", 3650))
    since_d = date.today() - timedelta(days=days)
    try:
        history = trading_api.get_transactions_history(
            transaction_request=HistoryRequest(from_date=since_d, to_date=date.today()),
            raw=False,
        )
        items = _json_safe(history.data if hasattr(history, "data") else history)

        # DEGIROs "totalInBaseCurrency" ist der reine Handelswert ohne
        # Gebuehren; "totalPlusAllFeesInBaseCurrency" enthaelt Kauf-/Verkaufs-
        # gebuehren bereits (das ist auch die Zahl, die an anderer Stelle fuer
        # realisiertes G/V und Netto-Einzahlungen verwendet wird - siehe
        # closed_positions()). Gebuehr = Differenz der beiden, das
        # funktioniert vorzeichenunabhaengig fuer Kauf UND Verkauf. Damit die
        # hier angezeigten Transaktionen 1:1 mit der Gesamtperformance
        # zusammenpassen, wird beides zusaetzlich nach CHF umgerechnet.
        fx_series = get_fx_series(since_d, date.today(), base_currency, TARGET_CURRENCY)
        for t in items:
            d_str = str(t.get("date") or "")[:10]
            try:
                day = date.fromisoformat(d_str)
            except ValueError:
                day = date.today()
            fx = fx_rate_on(fx_series, day, fallback=1.0)

            total_base = t.get("totalInBaseCurrency")
            total_incl_fees_base = t.get("totalPlusAllFeesInBaseCurrency")
            if total_incl_fees_base is None:
                total_incl_fees_base = total_base
            fee_base = (
                _num(total_base) - _num(total_incl_fees_base)
                if total_base is not None and total_incl_fees_base is not None
                else 0.0
            )

            t["feesBaseCurrency"] = round(fee_base, 4)
            t["feesChf"] = round(fee_base * fx, 2)
            t["totalChf"] = round(_num(total_base) * fx, 2) if total_base is not None else None
            t["totalPlusFeesChf"] = (
                round(_num(total_incl_fees_base) * fx, 2) if total_incl_fees_base is not None else None
            )

        return jsonify({
            "fetchedAt": datetime.now().isoformat(),
            "currency": TARGET_CURRENCY,
            "baseCurrency": base_currency,
            "transactions": items,
        })
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

    Cash-Historie: rekonstruiert durch Aufsummieren des "change"-Deltas jeder
    einzelnen Buchung aus DEGIROs 'cashMovements' (raw=False, also das
    typisierte degiro_connector-Modell: jede Buchung hat ein klar definiertes
    change:float in ihrer eigenen currency:str). Das im rohen JSON gelieferte
    "balance"-Feld je Buchung (ein Momentaufnahme-Saldo je Waehrung) wurde
    zuvor verwendet, war aber fuer dieses Konto durchgehend leer/0 (egal ob
    die chronologisch letzte oder eine "nicht-leere" Buchung des Tages
    gewaehlt wurde) und lieferte dadurch eine komplett flache, falsche Kurve -
    das trieb die einzahlungsbereinigte %-Performance auf unsinnige Werte
    (z.B. +18%, obwohl der Gesamtwert unter der Einzahlung lag). Die
    kumulierte Delta-Summe hat dieses Problem nicht, braucht aber trotzdem
    eine Verankerung: die absolute Summe ab since_d kennt keine Aktivitaet
    von VOR since_d, daher wird die gesamte Serie um eine Konstante
    verschoben, sodass sie am letzten bekannten Tag exakt dem live
    abgefragten "available to trade"-Guthaben entspricht.

    Netto-Einzahlungen: NICHT durch Klassifizieren einzelner Kontobuchungen
    bestimmt (zwei Anlaeufe damit - ueber 'type' geraten, dann ueber
    Anwesenheit von 'productId' - waren beide nachweislich falsch: manche
    Einzahlungen/Sweeps in den Cash-Fund tragen offenbar auch eine productId
    und wurden faelschlich als Handel statt als Einzahlung gezaehlt, was die
    Kennzahl bei jeder so verpassten Einzahlung sprunghaft nach oben trieb).
    Stattdessen rein rechnerisch aus drei bereits verlaesslichen Groessen
    hergeleitet:
        NettoEinzahlungen(t) = Cash-Saldo(t) - kumulierter Handels-Cashflow(t)
                                              - kumulierter Dividenden-Cashflow(t)
    Der Handels-Cashflow kommt direkt aus der Transaktionshistorie (bereits
    an anderer Stelle erfolgreich genutzt), nicht aus den mehrdeutigen
    Kontobuchungen. Ohne den Dividenden-Abzug wuerden erhaltene Dividenden
    (und die darauf abgezogene Quellensteuer) faelschlich wie eine Einzahlung
    gezaehlt statt wie ein Gewinn, da sie den Cash-Saldo erhoehen, aber nicht
    Teil des Handels-Cashflows sind - das druecke "Seit Einzahlung" ungefaehr
    um die Summe der erhaltenen Dividenden zu tief. Anders als bei "Einzahlung
    vs. Handel" ist die Dividenden-Klassifizierung hier zuverlaessig moeglich:
    DEGIROs 'type'-Feld unterscheidet Dividenden nicht von anderen Cash-
    Buchungen (durchgehend "CASH_TRANSACTION"), aber das 'description'-Feld
    enthaelt bei diesem (deutschsprachigen) Konto zuverlaessig "Dividende"
    bzw. "Dividendensteuer" - verifiziert anhand der echten Kontobuchungen
    dieses Nutzers. Stornierte/korrigierte Dividenden (negative Betraege mit
    passender Beschreibung) gleichen sich beim Aufsummieren automatisch
    korrekt aus. Diese Herleitung setzt voraus, dass der Cash-Saldo am
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
            raw=False,
        )
        movements = (overview.cash_movements if overview else None) or []
    except Exception:  # noqa: BLE001
        logger.warning("Kontoauszug (cashMovements) nicht abrufbar", exc_info=True)
        movements = []

    logger.info("cashMovements: %d Eintraege gefunden (seit %s)", len(movements), since_d.isoformat())

    daily_delta_chf: dict[str, float] = {}
    skipped = 0
    for m in movements:
        if m.date is None or m.change is None:
            skipped += 1
            continue
        day = m.date.date()
        d = day.isoformat()
        daily_delta_chf[d] = daily_delta_chf.get(d, 0.0) + _to_chf(m.currency, m.change, day)
    if skipped:
        logger.info("cashMovements: %d von %d Buchungen ohne date/change uebersprungen", skipped, len(movements))

    cash_result: dict[str, float] = {}
    running = 0.0
    for d in sorted(daily_delta_chf):
        running += daily_delta_chf[d]
        cash_result[d] = running

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

    dividend_movements = sorted(
        (m for m in movements if m.date is not None and m.change is not None
         and m.description and "divid" in m.description.lower()),
        key=lambda mv: mv.date,
    )
    dividend_cashflow_by_day: dict[str, float] = {}
    running = 0.0
    for m in dividend_movements:
        day = m.date.date()
        running += _to_chf(m.currency, m.change, day)
        dividend_cashflow_by_day[day.isoformat()] = running
    filled_dividend_cf = _forward_fill(dividend_cashflow_by_day, since_d, date.today())
    dividend_cf_by_day = {c["date"]: (c["value"] or 0.0) for c in filled_dividend_cf}

    filled_cash = _forward_fill(cash_result, since_d, date.today())

    deposits_result: dict[str, float] = {}
    for c in filled_cash:
        d = c["date"]
        if c["value"] is None:
            continue
        deposits_result[d] = c["value"] - trade_cf_by_day.get(d, 0.0) - dividend_cf_by_day.get(d, 0.0)

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


@app.route("/api/dividends", methods=["GET"])
@require_auth
@require_login
def dividends():
    """
    Erhaltene Dividenden pro Jahr (netto, nach Quellensteuer) plus eine
    einfache Prognose fuer die naechsten 12 Monate: je aktuell gehaltener
    Position die Summe der in den letzten 12 Monaten TATSAECHLICH fuer genau
    diese Position erhaltenen Dividenden (kein hochgerechneter Pro-Aktie-Wert,
    keine externe Dividendenkalender-Quelle - nur echte, bereits verbuchte
    Zahlungen). Das ist eine grobe Naeherung: unterschaetzt die Prognose bei
    kuerzlich aufgestockten Positionen (noch keine 12 Monate Historie in
    dieser Groesse) und ueberschaetzt sie bei kuerzlich reduzierten.

    Dividenden werden wie bei der Netto-Einzahlungs-Berechnung (siehe
    build_cash_and_net_deposits_history) ueber das 'description'-Feld der
    Kontobuchungen erkannt ("Dividende"/"Dividendensteuer") - DEGIROs 'type'
    unterscheidet sie nicht von anderen Cash-Buchungen.
    """
    trading_api = SESSION["trading_api"]
    try:
        overview = trading_api.get_account_overview(
            overview_request=OverviewRequest(from_date=date(2005, 1, 1), to_date=date.today()),
            raw=False,
        )
        movements = (overview.cash_movements if overview else None) or []
    except Exception as e:  # noqa: BLE001
        logger.exception("Kontoauszug (cashMovements) nicht abrufbar")
        return jsonify({"error": f"Kontoauszug nicht abrufbar: {e}"}), 502

    dividend_movements = [
        m for m in movements
        if m.date is not None and m.change is not None
        and m.description and "divid" in m.description.lower()
    ]
    if not dividend_movements:
        return jsonify({"yearly": [], "forecastChf": 0.0, "forecastPositions": [],
                         "forecastPeriodStart": None, "forecastPeriodEnd": None})

    since_d = min(m.date.date() for m in dividend_movements)
    fx_cache: dict[str, dict[str, float]] = {}

    def _to_chf(ccy, amount, day: date) -> float:
        if not isinstance(ccy, str) or len(ccy) != 3 or not ccy.isalpha():
            return 0.0
        ccy = ccy.upper()
        if ccy not in fx_cache:
            fx_cache[ccy] = get_fx_series(since_d, date.today(), ccy, TARGET_CURRENCY)
        return _num(amount) * fx_rate_on(fx_cache[ccy], day, fallback=1.0)

    # Pro Jahr UND pro Position aufgeschluesselt (nicht nur die Jahressumme),
    # damit das Frontend die Saeulen nach ausschuettender Position einfaerben
    # kann. Bewegungen ohne erkennbare productId (in der Praxis bislang keine
    # beobachtet) fliessen nicht in die Aufschluesselung ein, wohl aber in
    # "totalChf" - beide sollten also normalerweise uebereinstimmen.
    yearly_total: dict[int, float] = {}
    yearly_by_product: dict[int, dict[int, float]] = {}
    forecast_by_product: dict[int, float] = {}
    forecast_start = date.today() - timedelta(days=365)
    all_product_ids: set[int] = set()
    for m in dividend_movements:
        day = m.date.date()
        amount_chf = _to_chf(m.currency, m.change, day)
        yearly_total[day.year] = yearly_total.get(day.year, 0.0) + amount_chf
        if m.product_id is not None:
            all_product_ids.add(m.product_id)
            by_product = yearly_by_product.setdefault(day.year, {})
            by_product[m.product_id] = by_product.get(m.product_id, 0.0) + amount_chf
            if day >= forecast_start:
                forecast_by_product[m.product_id] = forecast_by_product.get(m.product_id, 0.0) + amount_chf

    held_ids: set[int] = set()
    try:
        account_update = trading_api.get_update(
            request_list=[UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0)],
            raw=True,
        )
        current_positions = _flatten_rows((account_update.get("portfolio") or {}).get("value") or [])
        held_ids = {int(p["id"]) for p in current_positions
                    if p.get("id") is not None and p.get("positionType") in (None, "PRODUCT")
                    and _num(p.get("size")) != 0}
    except Exception:  # noqa: BLE001
        logger.warning("Aktuelle Positionen fuer Dividenden-Prognose nicht ermittelbar", exc_info=True)

    # Ein Produkt-Info-Abruf fuer ALLE jemals dividendenzahlenden Produkte -
    # deckt sowohl die historischen Saeulen (auch laengst verkaufte Positionen)
    # als auch die Prognose (nur noch gehaltene) mit Name/Symbol ab.
    product_info: dict = {}
    if all_product_ids:
        info_raw = trading_api.get_products_info(product_list=list(all_product_ids), raw=True)
        product_info = (info_raw or {}).get("data", {})

    def _position_label(pid: int) -> dict:
        info = product_info.get(str(pid), {})
        return {"productId": pid, "name": info.get("name"), "symbol": info.get("symbol")}

    yearly_list = []
    for y in sorted(yearly_total):
        by_product = yearly_by_product.get(y, {})
        yearly_list.append({
            "year": y,
            "totalChf": round(yearly_total[y], 2),
            "byProduct": [
                {**_position_label(pid), "amountChf": round(amt, 2)}
                for pid, amt in sorted(by_product.items(), key=lambda kv: -kv[1])
            ],
        })

    forecast_positions = []
    forecast_total = 0.0
    for pid in sorted((p for p in forecast_by_product if p in held_ids), key=lambda p: -forecast_by_product[p]):
        amount_chf = forecast_by_product[pid]
        forecast_total += amount_chf
        forecast_positions.append({**_position_label(pid), "trailing12mChf": round(amount_chf, 2)})

    return jsonify({
        "yearly": yearly_list,
        "forecastChf": round(forecast_total, 2),
        "forecastPositions": forecast_positions,
        "forecastPeriodStart": forecast_start.isoformat(),
        "forecastPeriodEnd": date.today().isoformat(),
    })


@app.route("/api/sectors", methods=["GET"])
@require_auth
@require_login
def sectors():
    """
    Wirtschaftssektor je aktuell gehaltener Position, ueber Yahoo Finance
    (siehe fetch_yahoo_sector) - DEGIROs eigene Produktdaten enthalten keine
    Sektor-/Brancheninformation. Reine Best-Effort-Zuordnung: ETFs/ETPs haben
    keinen echten Sektor (werden als "ETF" gekennzeichnet), nicht ueber Yahoo
    auffindbare Produkte (v.a. Zertifikate/Hebelprodukte) liefern ueberall
    null - das Frontend gruppiert das unter "Unbekannt".
    """
    trading_api = SESSION["trading_api"]
    try:
        account_update = trading_api.get_update(
            request_list=[UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0)],
            raw=True,
        )
        positions = _flatten_rows((account_update.get("portfolio") or {}).get("value") or [])
        positions = [p for p in positions if p.get("positionType") in (None, "PRODUCT") and _num(p.get("size")) != 0]
        product_ids = [int(p["id"]) for p in positions if p.get("id") is not None]
        product_info = {}
        if product_ids:
            info_raw = trading_api.get_products_info(product_list=product_ids, raw=True)
            product_info = (info_raw or {}).get("data", {})
    except Exception as e:  # noqa: BLE001
        logger.exception("Positionen fuer Sektor-Zuordnung nicht abrufbar")
        return jsonify({"error": f"Positionen nicht abrufbar: {e}"}), 502

    results = []
    for p in positions:
        pid = str(p.get("id"))
        info = product_info.get(pid, {})
        sector_info = fetch_yahoo_sector(info.get("isin"), info.get("symbol"))
        results.append({"productId": pid, **sector_info})

    return jsonify({"positions": results})


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
