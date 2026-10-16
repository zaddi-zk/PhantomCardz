"""NOWPayments integration for real-time crypto payment tracking.

Enabled only when ``NOWPAYMENTS_API_KEY`` is set; otherwise the app falls back
to the legacy static-address + manual-approval flow. Uses only the standard
library so no extra dependencies are required.

Docs: https://documenter.getpostman.com/view/7907941/S1a32n38
"""
import os
import json
import hmac
import hashlib
import urllib.parse
import urllib.request
import urllib.error

API_BASE = os.getenv("NOWPAYMENTS_API_BASE", "https://api.nowpayments.io").rstrip("/")
API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()

# NOWPayments payment_status -> our canonical state
COMPLETED_STATES = {"finished", "confirmed"}
FAILED_STATES = {"failed", "refunded", "expired"}
UNDERPAID_STATES = {"partially_paid"}


class ProviderError(Exception):
    pass


def enabled() -> bool:
    return bool(API_KEY)


def _req(method: str, path: str, body=None, params=None, timeout: int = 25):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("message", detail)
        except Exception:
            pass
        raise ProviderError(f"{e.code}: {detail}")
    except Exception as e:  # network, timeout, bad json
        raise ProviderError(str(e))


def create_payment(price_amount, price_currency: str, pay_currency: str,
                   order_id: str, description: str = "",
                   ipn_callback_url: str = "") -> dict:
    """Create a hosted payment. ``price_amount`` is fiat (USD)."""
    body = {
        "price_amount": round(float(price_amount), 2),
        "price_currency": price_currency.lower(),
        "pay_currency": pay_currency.lower(),
        "order_id": str(order_id),
        "order_description": (description or "payment")[:200],
    }
    if ipn_callback_url:
        body["ipn_callback_url"] = ipn_callback_url
    return _req("POST", "/v1/payment", body=body)


def get_payment(payment_id) -> dict:
    return _req("GET", "/v1/payment/" + urllib.parse.quote(str(payment_id)))


def get_currencies() -> list:
    data = _req("GET", "/v1/currencies")
    return [str(c).upper() for c in (data.get("currencies") or [])]


def map_status(np_status: str) -> str:
    """Map a NOWPayments status to completed | failed | underpaid | pending."""
    s = (np_status or "").lower()
    if s in COMPLETED_STATES:
        return "completed"
    if s in FAILED_STATES:
        return "failed"
    if s in UNDERPAID_STATES:
        return "underpaid"
    return "pending"


def verify_ipn(raw_body: bytes, signature: str) -> bool:
    """Verify the ``x-nowpayments-sig`` HMAC-SHA512 signature of an IPN call."""
    if not IPN_SECRET or not signature:
        return False
    try:
        data = json.loads(raw_body.decode())
    except Exception:
        return False
    # NOWPayments signs the JSON with sorted keys. Different clients serialize
    # with/without spaces, so accept either canonical form.
    candidates = [
        json.dumps(data, separators=(",", ":"), sort_keys=True),
        json.dumps(data, sort_keys=True),
    ]
    for payload in candidates:
        digest = hmac.new(IPN_SECRET.encode(), payload.encode(), hashlib.sha512).hexdigest()
        if hmac.compare_digest(digest, signature):
            return True
    return False
