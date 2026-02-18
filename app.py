import os, re, datetime, tempfile
import requests
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory

import numpy as np
import cv2
from yt_dlp import YoutubeDL

app = Flask(__name__, static_folder="static", static_url_path="")

# ===== ENV =====
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SECRET_KEY = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()  # sb_secret_...

DAILY_FREE_LIMIT = int(os.getenv("DAILY_FREE_LIMIT", "10"))

PAY_TO_ADDRESS = (os.getenv("PAY_TO_ADDRESS") or "").strip()
PRICE_USDT = float(os.getenv("PRICE_USDT", "9.00"))

TRONSCAN_API_BASE = (os.getenv("TRONSCAN_API_BASE") or "https://apilist.tronscan.org/api").strip()
TRC20_USDT_CONTRACT = (os.getenv("TRC20_USDT_CONTRACT") or "").strip()

if not SUPABASE_URL:
    raise RuntimeError("Missing env SUPABASE_URL (Supabase Project URL)")
if not SUPABASE_SECRET_KEY:
    raise RuntimeError("Missing env SUPABASE_SECRET_KEY (sb_secret_... from Supabase API Keys)")

REST_BASE = f"{SUPABASE_URL}/rest/v1"
AUTH_USER_ENDPOINT = f"{SUPABASE_URL}/auth/v1/user"

# ===== TIME HELPERS =====
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

def start_of_today_utc():
    now = utc_now()
    return datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)

# ===== AUTH HELPERS =====
def get_bearer_token():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth.replace("Bearer ", "").strip() or None

def get_user_from_access_token(access_token: str):
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {access_token}",
    }
    r = requests.get(AUTH_USER_ENDPOINT, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    j = r.json()
    uid = j.get("id")
    if not uid:
        return None
    return {"id": uid, "email": j.get("email")}

# ===== SUPABASE REST (server-side, privileged) =====
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

# ===== TRONSCAN PAYMENT VERIFY =====
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

# ===== FAST CHECK =====
AI_KEYWORDS = [
    "kling", "luma", "sora", "runway", "midjourney", "haiper", "synthesia", "heygen",
    "deepfake", "face swap", "faceswap", "ai generated", "aigenerated"
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

def fetch_x_oembed(url: str):
    try:
        api = "https://publish.twitter.com/oembed"
        r = requests.get(api, params={"url": url}, timeout=8)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def contains_ai_terms(text: str):
    if not text:
        return False, None
    t = text.lower()
    terms = [
        "ai", "a.i.", "deepfake", "face swap", "faceswap", "synthetic",
        "sora", "runway", "kling", "luma", "midjourney", "haiper",
        "generated", "ai-generated", "aigenerated"
    ]
    for term in terms:
        if term in t:
            return True, term
    return False, None

def fast_check_score(url: str):
    url_l = (url or "").strip().lower()
    reasons, score = [], 0

    if not url_l.startswith("http"):
        return (0, "invalid_url", ["URL must start with http/https"])

    host = normalize_host(url_l)
    if not host:
        return (0, "invalid_url", ["Could not parse URL host"])

    is_x = host in HIGH_RISK_HOSTS
    is_short = host in MED_RISK_HOSTS

    if is_x:
        score += 30
        reasons.append("Source signal: X/Twitter has a high repost/bot content rate")
    elif is_short:
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

    if is_x and "/status/" in url_l:
        oembed = fetch_x_oembed(url)
        if oembed:
            text_fields = " ".join([
                oembed.get("author_name") or "",
                oembed.get("title") or "",
                oembed.get("provider_name") or ""
            ]).strip()
            found, term = contains_ai_terms(text_fields)
            if found:
                score += 35
                reasons.append(f"Text signal: AI-related term detected in post metadata: {term}")

            html = (oembed.get("html") or "").lower()
            if "video" in html or "twitter-tweet" in html:
                score += 8
                reasons.append("Content signal: embed indicates rich media (possible video)")
        else:
            reasons.append("Metadata note: could not fetch oEmbed info (public metadata limited)")

    if is_short and "@" not in url_l and "/@" not in url_l:
        score += 7
        reasons.append("Weak origin signal: no visible creator handle in the URL")

    score = max(0, min(100, score))
    verdict = "high_risk" if score >= 75 else ("medium_risk" if score >= 45 else "low_risk")
    return (score, verdict, reasons[:6])

# ===== FULL SCAN (PRO-only, X-only) =====
def is_x_status_url(url: str) -> bool:
    u = (url or "").lower()
    return ("x.com" in u or "twitter.com" in u) and ("/status/" in u)

def download_video_x(url: str, out_dir: str):
    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "video.%(ext)s"),
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            for ext in ["mp4", "mkv", "webm"]:
                p = os.path.join(out_dir, f"video.{ext}")
                if os.path.exists(p):
                    return p
    except Exception:
        return None
    return None

def analyze_video_frames_basic(video_path: str, max_frames: int = 12):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return 0, ["Could not open video for analysis"]

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, total // max_frames) if total > 0 else 10

    blur_scores = []
    texture_scores = []
    frames_read = 0
    idx = 0

    while frames_read < max_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            break

        frames_read += 1
        idx += step

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        lap = cv2.Laplacian(gray, cv2.CV_64F)
        sharp = float(lap.var())
        blur_scores.append(sharp)

        f = np.fft.fft2(gray)
        fshift = np.fft.fftshift(f)
        magnitude = np.log(np.abs(fshift) + 1.0)
        hf = float(np.mean(magnitude))
        texture_scores.append(hf)

    cap.release()

    if frames_read < 3:
        return 0, ["Not enough frames to analyze"]

    sharp_m = float(np.median(blur_scores))
    tex_m = float(np.median(texture_scores))

    reasons = []
    score = 0

    if sharp_m < 80:
        score += 35
        reasons.append("Frame signal: unusually low sharpness (over-smoothed look)")
    elif sharp_m < 140:
        score += 18
        reasons.append("Frame signal: slightly low sharpness (possible heavy filtering)")

    if tex_m < 4.3:
        score += 35
        reasons.append("Frame signal: low texture detail (synthetic/denoised appearance)")
    elif tex_m < 4.8:
        score += 18
        reasons.append("Frame signal: reduced texture detail (possible AI smoothing)")

    if score >= 60:
        score += 10
        reasons.append("Combined signal: multiple synthetic-looking frame patterns")

    score = max(0, min(100, score))
    return score, reasons[:6]

# ===== DEBUG ROUTES =====
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "realorfake", "has_analyze_full": True})

@app.get("/routes")
def routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

# ===== MAIN ROUTES =====
@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.post("/analyze")
def analyze():
    try:
        data = request.get_json() or {}
        url = (data.get("url", "") or "").strip()

        score, verdict, reasons = fast_check_score(url)
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

@app.post("/analyze_full")
def analyze_full():
    """
    PRO-only.
    For X status links: tries to download video + frame heuristics.
    If not X: returns Fast Check fallback.
    """
    try:
        token = get_bearer_token()
        if not token:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        user = get_user_from_access_token(token)
        if not user:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        profile = reset_daily_if_needed(ensure_profile(user))
        if not profile.get("is_pro"):
            return jsonify({"ok": False, "error": "pro_required", "message": "Full Scan (Beta) is available in PRO."}), 402

        data = request.get_json() or {}
        url = (data.get("url", "") or "").strip()

        # Not X -> fallback fast
        if not is_x_status_url(url):
            score, verdict, reasons = fast_check_score(url)
            if verdict == "invalid_url":
                return jsonify({"ok": False, "error": "invalid_url", "message": reasons[0]}), 400

            return jsonify({
                "ok": True,
                "result": "fake" if verdict == "high_risk" else "real",
                "percent": int(score),
                "verdict": verdict,
                "reasons": reasons,
                "type": "fast_check_fallback",
                "note": "Full Scan supports X links. Returned Fast Check fallback."
            })

        # X -> download + analyze
        with tempfile.TemporaryDirectory() as td:
            path = download_video_x(url, td)
            if not path:
                score, verdict, reasons = fast_check_score(url)
                return jsonify({
                    "ok": True,
                    "result": "fake" if verdict == "high_risk" else "real",
                    "percent": int(score),
                    "verdict": verdict,
                    "reasons": (["Download failed; returned Fast Check fallback."] + reasons)[:6],
                    "type": "fast_check_fallback",
                    "note": "Could not download X video. Returned Fast Check fallback."
                })

            score, reasons = analyze_video_frames_basic(path, max_frames=12)

        verdict = "high_risk" if score >= 75 else ("medium_risk" if score >= 45 else "low_risk")

        return jsonify({
            "ok": True,
            "result": "fake" if verdict == "high_risk" else "real",
            "percent": int(score),
            "verdict": verdict,
            "reasons": reasons,
            "type": "full_scan_beta",
            "note": "Full Scan (Beta) uses lightweight frame heuristics. Not 100% proof."
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ===== APP API =====
@app.get("/api/me")
def api_me():
    token = get_bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    user = get_user_from_access_token(token)
    if not user:
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
    if not user:
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

    profile_update(user["id"], {
        "is_pro": True,
        "pro_plan": "lifetime_pro",
        "pro_activated_at": utc_now().isoformat()
    })
    return jsonify({"ok": True, "status": "activated", "is_pro": True, "meta": meta})
