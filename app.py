import os, re, datetime
import requests
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory
from supabase import create_client, Client

app = Flask(__name__, static_folder="static", static_url_path="")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
DAILY_FREE_LIMIT = int(os.getenv("DAILY_FREE_LIMIT", "10"))

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "").strip()
PRICE_USDT = float(os.getenv("PRICE_USDT", "9.00"))

TRONSCAN_API_BASE = os.getenv("TRONSCAN_API_BASE", "https://apilist.tronscan.org/api").strip()
TRC20_USDT_CONTRACT = os.getenv("TRC20_USDT_CONTRACT", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------------------------
# Helpers: auth + profiles
# ---------------------------
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

def start_of_today_utc():
    now = utc_now()
    return datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)

def get_user_from_bearer():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return None
    try:
        user_resp = supabase.auth.get_user(token)
        return user_resp.user
    except Exception:
        return None

def ensure_profile(user):
    prof = supabase.table("profiles").select("*").eq("id", user.id).execute()
    if prof.data and len(prof.data) > 0:
        return prof.data[0]
    row = {
        "id": user.id,
        "email": user.email,
        "is_pro": False,
        "free_used_today": 0,
        "free_reset_at": start_of_today_utc().isoformat()
    }
    ins = supabase.table("profiles").insert(row).execute()
    return ins.data[0]

def reset_daily_if_needed(profile):
    try:
        reset_at = datetime.datetime.fromisoformat(profile["free_reset_at"].replace("Z", "+00:00"))
    except Exception:
        reset_at = start_of_today_utc()
    today = start_of_today_utc()
    if reset_at < today:
        upd = supabase.table("profiles").update({
            "free_used_today": 0,
            "free_reset_at": today.isoformat()
        }).eq("id", profile["id"]).execute()
        return upd.data[0]
    return profile

# ---------------------------
# TronScan payment verification (semi-auto)
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
# Fast Check (no downloads, no randomness)
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
    reasons = []
    score = 0

    if not url_l.startswith("http"):
        return (0, "invalid_url", ["URL must start with http/https"])

    host = normalize_host(url_l)
    if not host:
        return (0, "invalid_url", ["Could not parse URL host"])

    # 1) Source signal
    if host in HIGH_RISK_HOSTS:
        score += 30
        reasons.append("Source signal: X/Twitter has a high repost/bot content rate")
    elif host in MED_RISK_HOSTS:
        score += 18
        reasons.append("Source signal: short-form platforms have frequent reuploads")
    else:
        score += 10
        reasons.append("Source signal: unknown platform (harder to verify original source)")

    # 2) Keyword signals
    matched = [k for k in AI_KEYWORDS if k in url_l]
    if matched:
        score += 55
        reasons.append(f"AI keyword detected in link: {matched[0]}")

    # 3) Reupload/aggregator patterns
    if re.search(r"(download|dl=|save|repost|mirror|cdn|proxy)", url_l):
        score += 12
        reasons.append("Reupload pattern detected in link (download/repost/mirror)")

    # 4) Weak origin handle signal (heuristic)
    if host in MED_RISK_HOSTS and "@" not in url_l and "/@" not in url_l:
        score += 7
        reasons.append("Weak origin signal: no visible creator handle in the URL")

    score = max(0, min(100, score))

    if score >= 75:
        verdict = "high_risk"
    elif score >= 45:
        verdict = "medium_risk"
    else:
        verdict = "low_risk"

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

        # For UI compatibility we keep "result" + "percent"
        # But correct meaning is "RISK SCORE", not "AI probability".
        return jsonify({
            "result": "fake" if verdict == "high_risk" else "real",
            "percent": int(score),
            "verdict": verdict,
            "reasons": reasons,
            "type": "fast_check",
            "note": "Fast risk check based on source signals and public link patterns. Not 100% proof."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/me")
def api_me():
    user = get_user_from_bearer()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    profile = reset_daily_if_needed(ensure_profile(user))
    left = None if profile["is_pro"] else max(0, DAILY_FREE_LIMIT - int(profile["free_used_today"]))

    return jsonify({
        "ok": True,
        "user": {"id": user.id, "email": user.email},
        "is_pro": profile["is_pro"],
        "free_left_today": left,
        "daily_free_limit": DAILY_FREE_LIMIT
    })


@app.post("/api/consume-scan")
def api_consume_scan():
    user = get_user_from_bearer()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    profile = reset_daily_if_needed(ensure_profile(user))

    if profile["is_pro"]:
        return jsonify({"ok": True, "allowed": True, "reason": "pro"})

    used = int(profile["free_used_today"])
    if used >= DAILY_FREE_LIMIT:
        return jsonify({"ok": True, "allowed": False, "reason": "limit_reached", "free_left_today": 0})

    used += 1
    upd = supabase.table("profiles").update({"free_used_today": used}).eq("id", user.id).execute()
    newp = upd.data[0]
    left = max(0, DAILY_FREE_LIMIT - int(newp["free_used_today"]))

    return jsonify({"ok": True, "allowed": True, "reason": "free", "free_left_today": left})


@app.post("/api/payments/submit-txid")
def api_submit_txid():
    user = get_user_from_bearer()
    if not user:
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

    supabase.table("profiles").update({
        "is_pro": True,
        "pro_plan": "lifetime_pro",
        "pro_activated_at": utc_now().isoformat()
    }).eq("id", user.id).execute()

    return jsonify({"ok": True, "status": "activated", "is_pro": True, "meta": meta})
