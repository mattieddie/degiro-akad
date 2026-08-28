"""
Lokaler DEGIRO-Proxy fuer das degiro-akad Dashboard.

Laeuft NUR auf deinem eigenen Rechner (127.0.0.1) und leitet Anfragen von der
GitHub-Pages-Seite an DEGIRO weiter. Es wird NICHTS auf die Festplatte oder
nach GitHub geschrieben - Login-Daten und DEGIRO-Antworten leben ausschliesslich
im Arbeitsspeicher dieses Prozesses, solange er laeuft. Beendest du das Skript
(Strg+C), ist alles weg.

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

from flask import Flask, jsonify, request

from degiro_connector.core.exceptions import DeGiroConnectionError
from degiro_connector.trading.api import API as TradingAPI
from degiro_connector.trading.models.account import UpdateOption, UpdateRequest
from degiro_connector.trading.models.credentials import Credentials
from degiro_connector.trading.models.transaction import HistoryRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("degiro-proxy")

app = Flask(__name__)

# --- In-Memory-Session (nichts wird persistiert) -----------------------------
SESSION: dict = {
    "trading_api": None,
    "int_account": None,
    "logged_in_at": None,
}

# Wird beim Start einmalig erzeugt und in der Konsole ausgegeben. Die Seite
# muss dieses Token kennen, um den Proxy ansprechen zu duerfen.
PROXY_TOKEN = secrets.token_urlsafe(24)

ALLOWED_ORIGINS: list[str] = []


# --- Hilfsfunktionen -----------------------------------------------------------

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
        return _json_safe(obj.model_dump(mode="json"))
    return obj


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


# --- Endpunkte -------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "logged_in": SESSION["trading_api"] is not None,
        "logged_in_at": SESSION["logged_in_at"],
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
        trading_api.credentials.int_account = int_account

        SESSION["trading_api"] = trading_api
        SESSION["int_account"] = int_account
        SESSION["logged_in_at"] = datetime.now().isoformat()

        logger.info("Login erfolgreich (int_account=%s)", int_account)
        return jsonify({"success": True})
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
    SESSION["trading_api"] = None
    SESSION["int_account"] = None
    SESSION["logged_in_at"] = None
    return jsonify({"success": True})


@app.route("/api/portfolio", methods=["GET"])
@require_auth
@require_login
def portfolio():
    trading_api = SESSION["trading_api"]
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
        positions = [p for p in positions if p.get("positionType") in (None, "PRODUCT")]

        totals = _flatten_dict(account_update.get("totalPortfolio") or {})

        product_ids = [int(p["id"]) for p in positions if p.get("id") is not None]
        product_info = {}
        if product_ids:
            info_raw = trading_api.get_products_info(product_list=product_ids, raw=True)
            product_info = (info_raw or {}).get("data", {})

        cash = (
            totals.get("totalCashFundValueInBaseCurrency")
            or totals.get("flatexCashAvailable")
            or totals.get("degiroCashFundValue")
            or 0
        )

        enriched = []
        total_value = 0.0
        for p in positions:
            pid = str(p.get("id"))
            info = product_info.get(pid, {})
            value = p.get("value") or 0
            total_value += value if isinstance(value, (int, float)) else 0
            enriched.append({
                "productId": pid,
                "name": info.get("name") or p.get("name"),
                "symbol": info.get("symbol"),
                "isin": info.get("isin"),
                "currency": p.get("currency") or info.get("currency"),
                "size": p.get("size"),
                "price": p.get("price"),
                "value": p.get("value"),
                "breakEvenPrice": p.get("breakEvenPrice"),
                "averagePrice": p.get("averagePrice") or p.get("breakEvenPrice"),
                "plToday": p.get("todayPlBase"),
                "plUnrealized": p.get("plBase"),
                "realizedProductPl": p.get("realizedProductPl"),
            })

        equity = totals.get("equity") or (cash + total_value)

        return jsonify({
            "fetchedAt": datetime.now().isoformat(),
            "positions": enriched,
            "totals": {
                "equity": equity,
                "cash": cash,
                "positionsValue": total_value,
                "totalDepositWithdrawal": totals.get("totalDepositWithdrawal"),
            },
            "rawTotals": totals,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("Fehler beim Abrufen des Portfolios")
        return jsonify({"error": f"Fehler beim Abrufen des Portfolios: {e}"}), 502


@app.route("/api/transactions", methods=["GET"])
@require_auth
@require_login
def transactions():
    trading_api = SESSION["trading_api"]
    days = int(request.args.get("days", 730))
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
    print(f" Erlaubte Herkunft(en): {', '.join(ALLOWED_ORIGINS)}")
    print(f" Proxy-Token (in der Webseite eintragen): {PROXY_TOKEN}")
    print(f" Adresse: http://127.0.0.1:{args.port}")
    print(" Beenden mit Strg+C")
    print("=" * 70)

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
