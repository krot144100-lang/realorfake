import os, re, datetime
import requests
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SECRET_KEY = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()  # sb_secret_...
DAILY_FREE_LIMIT = int(os.getenv("DAILY_FREE_LIMIT", "10"))

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "").strip()
PRICE_USDT = float(os.getenv("PRICE_USDT", "9.00"))
TRONSCAN_API_BASE = os.getenv("TRONSCAN_API_BASE", "https://apilist.tronscan.org/api").strip()
TRC20_USDT_CONTRACT = os.getenv("TRC20_USDT_CONTRACT", "").strip()

if not SUPABASE_URL:
    raise RuntimeError("Missing env SUPABASE_URL")
if not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing env SUPABASE_SECRET_KEY (sb_secret_...)")

REST_BASE = f"{SUPABASE_URL}/rest/v1"
AUTH_USER_ENDPOINT = f"{SUPABASE_URL}/auth/v1/user"

# ---------------------------
# Helpers: time
# ---------------------------
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

def start_of_today_utc():
    now = utc_now()
    return datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)

# ---------------------------
# Helpers: auth
# ---------------------------
def get_bearer_token():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth.replace("Bearer ", "").strip() or None

def get_user_from_access_token(access_token: str):
    # Verify user token with Supabase Auth REST
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {access_token}",
    }
    r = requests.get(AUTH_USER_ENDPOINT, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    j = r.json()
    # expected: { id, email, ... }
    return {"id": j.get("id"), "email": j.get("email")}

# ---------------------------
# Helpers: PostgREST profiles table (server-side with secret key)
# ---------------------------
def sb_headers_server():
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }

def profile_get(user_id: str):
    url = f"{REST_BASE}/profiles?id=eq.{user_id}&select=*"
    r = requests.get(url, headers=sb_headers_server(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"profiles_get_failed: {r.status_code} {r.text}")
    data = r.json()
    return data[0] if data else None

def profile_insert(user_id: str, email: str):
    url = f"{REST_BASE}/profiles"
    payload = [{
        "id": user_id,
        "email": email,
        "is_pro": False,
        "free_used_today": 0,
        "free_reset_at": start_of_today_utc().isoformat()
    }]
    headers = sb_headers_server()
    headers["Prefer"] = "return=representation"
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"profiles_insert_failed: {r.status_code} {r.text}")
    return r.json()[0]

def profile_update(user_id: str, fields: dict):
    url = f"{REST_BASE}/profiles?id=eq.{user_id}"
    headers = sb_headers_server()
    headers["Prefer"] = "return=representation"
    r = requests.patch(url, headers=headers, json=fields, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"profiles_update_failed: {r.status_code} {r.text}")
    data = r.json()
    return data[0] if data else None

def ensure_profile(user):
    p = profile_get(user["id"])
    if p:
        return p
    return profile_insert(user["id"], user.get("email"))

def reset_daily_if_needed(profile):
    try:
        reset_at = datetime.datetime.fromisoformat(profile["free_reset_at"].replace("Z", "+00:00"))
    except Exception:
        reset_at = start_of_today_utc()
    today = start_of_today_utc()
    if reset_at < today:
        return profile_update(profile["id"], {"free_used_today": 0, "free_reset_at": today.isoformat()})
    return profile

# ---------------------------
# TronScan payment verification
# ---------------------------
def tronscan_txinfo(txid):
    url = f"{TRONSCAN_API_BASE}/transaction-info?hash={txid}"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return None
    return r.json()

def verify_usdt_trc20_tx(txid, expected_to, usdt_contract, min_amount):
    j = tronscan_txinfo(txid)
    if not j:
        return (False, "tronscan_unavailable", {})

    info = j.get("trc20TransferInfo")
    if not info:
        return (False, "not_a_trc20_transfer", {"raw_keys": list(j.keys())})

    to_addr = (info.get("to_address") or "").strip()
    contract = (info.get("contract_address") or "").strip()
    amount_str = info.get("amount_str")
    decimals = int(info.get("decimals") or 6)

    confirmed = (j.get("confirmed") is True) or (int(j.get("confirmations") or 0) > 0)
    if not confirmed:
        return (False, "not_confirmed_yet", {"confirmations": j.get("confirmations")})

    try:
        amount = float(amount_str) / (10 ** decimals)
    except Exception:
        return (False, "bad_amount", {"amount_str": amount_str, "decimals": decimals})

    if expected_to and to_addr != expected_to:
        return (False, "wrong_recipient", {"to": to_addr, "expected": expected_to})

    if usdt_contract and contract != usdt_contract:
        return (False, "wrong_token_contract", {"contract": contract, "expected": usdt_contract})

    if amount + 1e-9 < float(min_amount):
        return (False, "insufficient_amount", {"amount": amount, "min_amount": min_amount})

    return (True, "ok", {"to": to_addr, "contract": contract, "amount": amount, "decimals": decimals})

# ---------------------------
# Fast Check (no downloads)
# ---------------------------
AI_KEYWORDS = [
    "kling","luma","sora","runway","midjourney","haiper","synthesia","heygen",
    "deepfake","face swap","faceswap","ai generated","aigenerated"
]
HIGH_RISK_HOSTS = {"x.com", "twitter.com"}
MED_RISK_HOSTS = {"tiktok.com", "www.tiktok.com", "instagram.com", "www.instagram.com"}

def normalize_host(url):
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if host.startswith("m."):
            host = host[2:]
        return host
    except Exception:
        return ""

def score_fast_check(url: str):
    url_l = (url or "").strip().lower()
    reasons, score = [], 0

    if not url_l.startswith("http"):
        return (0, "invalid_url", ["URL must start with http/https"])

    host = normalize_host(url_l)
    if not host:
        return (0, "invalid_url", ["Could not parse URL host"])

    if host in HIGH_RISK_HOSTS:
        score += 30
        reasons.append("Source signal: X/Twitter has a high repost/bot content rate")
    elif host in MED_RISK_HOSTS:
        score += 18
        reasons.append("Source signal: short-form platforms have frequent reuploads")
    else:
        score += 10
        reasons.append("Source signal: unknown platform (harder to verify original source)")

    matched = [k for k in AI_KEYWORDS if k in url_l]
    if matched:
        score += 55
        reasons.append(f"AI keyword detected in link: {matched[0]}")

    if re.search(r"(download|dl=|save|repost|mirror|cdn|proxy)", url_l):
        score += 12
        reasons.append("Reupload pattern detected in link (download/repost/mirror)")

    if host in MED_RISK_HOSTS and "@" not in url_l and "/@" not in url_l:
        score += 7
        reasons.append("Weak origin signal: no visible creator handle in the URL")

    score = max(0, min(100, score))
    verdict = "high_risk" if score >= 75 else ("medium_risk" if score >= 45 else "low_risk")
    return (score, verdict, reasons[:6])

# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.post("/analyze")
def analyze():
    try:
        data = request.get_json() or {}
        url = (data.get("url", "") or "").strip()

        score, verdict, reasons = score_fast_check(url)
        if verdict == "invalid_url":
            return jsonify({"error": "invalid_url", "message": reasons[0]}), 400

        return jsonify({
            "result": "fake" if verdict == "high_risk" else "real",
            "percent": int(score),
            "verdict": verdict,
            "reasons": reasons,
            "type": "fast_check",
            "note": "Fast risk check based on link/source signals. Not 100% proof."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/me")
def api_me():
    token = get_bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    user = get_user_from_access_token(token)
    if not user or not user.get("id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    profile = reset_daily_if_needed(ensure_profile(user))
    left = None if profile["is_pro"] else max(0, DAILY_FREE_LIMIT - int(profile["free_used_today"]))

    return jsonify({
        "ok": True,
        "user": {"id": user["id"], "email": user.get("email")},
        "is_pro": profile["is_pro"],
        "free_left_today": left,
        "daily_free_limit": DAILY_FREE_LIMIT
    })

@app.post("/api/consume-scan")
def api_consume_scan():
    token = get_bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    user = get_user_from_access_token(token)
    if not user or not user.get("id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    profile = reset_daily_if_needed(ensure_profile(user))
    if profile["is_pro"]:
        return jsonify({"ok": True, "allowed": True, "reason": "pro"})

    used = int(profile["free_used_today"])
    if used >= DAILY_FREE_LIMIT:
        return jsonify({"ok": True, "allowed": False, "reason": "limit_reached", "free_left_today": 0})

    used += 1
    profile_update(profile["id"], {"free_used_today": used})
    left = max(0, DAILY_FREE_LIMIT - used)
    return jsonify({"ok": True, "allowed": True, "reason": "free", "free_left_today": left})

@app.post("/api/payments/submit-txid")
def api_submit_txid():
    token = get_bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    user = get_user_from_access_token(token)
    if not user or not user.get("id"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json() or {}
    txid = (body.get("txid", "") or "").strip()
    if len(txid) < 10:
        return jsonify({"ok": False, "error": "txid_required"}), 400

    ok, code, meta = verify_usdt_trc20_tx(
        txid=txid,
        expected_to=PAY_TO_ADDRESS,
        usdt_contract=TRC20_USDT_CONTRACT,
        min_amount=PRICE_USDT
    )
    if not ok:
        return jsonify({"ok": False, "error": code, "meta": meta}), 400

    profile_update(user["id"], {
        "is_pro": True,
        "pro_plan": "lifetime_pro",
        "pro_activated_at": utc_now().isoformat()
    })
    return jsonify({"ok": True, "status": "activated", "is_pro": True, "meta": meta})
