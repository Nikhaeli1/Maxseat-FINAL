from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, UploadFile, File
import httpx, os, shutil, base64, time, hashlib
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from typing import List
import json, datetime, secrets, uuid, asyncio, smtplib, ssl, hmac, re
import html as _html
from urllib.parse import urlparse
from markupsafe import Markup
from email.message import EmailMessage

from database import (
    init_db,
    db_save_user, db_delete_user, db_load_users,
    db_save_puv,  db_delete_puv,  db_load_puvs,
    db_append_audit,    db_load_audit_logs,
    db_append_citation, db_load_citations,
    db_append_complaint, db_load_complaints, db_update_complaint,
    db_save_avatar, db_load_avatar, db_delete_avatar,
    TokenStore, DB_KIND, DB_URL_SOURCE, DB_ENV_NAMES, db_live_snapshot,
)
import database as _database

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))

# How browsers get live updates:
#   "poll" — pages ask /api/live every few seconds (Vercel: no WebSockets there)
#   "ws"   — pages keep a WebSocket open to /ws (local runs / servers that support it)
LIVE_MODE = os.environ.get("LIVE_MODE") or ("poll" if ON_VERCEL else "ws")

# Shared key the Pi sends in the X-Device-Key header when posting sensor readings.
# If it isn't set, /api/sensor accepts readings from anyone (fine for local testing only).
SENSOR_API_KEY = os.environ.get("SENSOR_API_KEY", "")

# Mobile app login tokens — stored in the database so every server accepts them
mobile_tokens    = TokenStore("enforcer")    # enforcer Bearer tokens
passenger_tokens = TokenStore("passenger")   # passenger Bearer tokens

# In-memory complaint store
complaints_db: list = []

# ══════════════════════════════════════════════
# NOTIFICATION CONFIG — fill these in
# ══════════════════════════════════════════════
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "asok.marielle04@gmail.com").strip()
GMAIL_SENDER_NAME  = "MaxSeat Alert System"
# ── Gmail (sends to ANY email, free) ───────────────────────────────────────
# Resend's free test mode only delivers to your own address. To email every new
# user, set these in Vercel: GMAIL_ADDRESS = the sending Gmail account, and
# GMAIL_APP_PASSWORD = a 16-letter App Password from that account
# (Google Account → Security → 2-Step Verification → App passwords).
# When GMAIL_APP_PASSWORD is set, Gmail is used instead of Resend.
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

# ── Resend (HTTP email API — works on Vercel) ──────────────────────────────
# Sign up free at https://resend.com → API Keys → Create Key
# Then set this as an environment variable in Vercel: RESEND_API_KEY=re_xxxx
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# ── Google Apps Script mailer (recommended on Vercel, free, any recipient) ──
# Gmail blocks SMTP logins coming from Vercel's servers, so the site instead calls
# a small script in your own Google account over normal HTTPS, and the script
# sends the email from your Gmail (up to 100 recipients a day on a free account).
# Set in Vercel: GOOGLE_SCRIPT_URL = the web app URL ending in /exec, and
# GOOGLE_SCRIPT_SECRET = the same secret written in the script.
# The script itself is in tools/maxseat-mailer.gs. When set, it is used first.
GOOGLE_SCRIPT_URL    = os.environ.get("GOOGLE_SCRIPT_URL", "").strip()
GOOGLE_SCRIPT_SECRET = os.environ.get("GOOGLE_SCRIPT_SECRET", "").strip()

# ══════════════════════════════════════════════

app = FastAPI(title="MaxSeat Alert System")

class ServerBusy(Exception):
    """The database didn't answer. Say "try again" instead of "you're signed out"."""

@app.exception_handler(ServerBusy)
async def _server_busy(request: Request, exc: ServerBusy):
    return JSONResponse({"success": False, "error": "The server is busy. Please try again in a moment."}, status_code=503)

# --- MIDDLEWARE ---
# Login cookies are signed with this key, so it must never be a value that is public on GitHub.
# Best: set SESSION_SECRET in Vercel. Without it, a key is made from the database address
# (a secret setting that every server shares), so logins still work on every server.
def _session_secret() -> str:
    key = os.environ.get("SESSION_SECRET", "").strip()
    if key:
        return key
    if DB_URL_SOURCE and _database.DATABASE_URL:
        return hashlib.sha256(("maxseat-session:" + _database.DATABASE_URL).encode()).hexdigest()
    if ON_VERCEL:
        print("[CONFIG] WARNING: no SESSION_SECRET and no database; logins will not survive between servers.")
        return secrets.token_hex(32)
    return "maxseat-local-dev-only"
SESSION_SECRET = _session_secret()
# Stay logged in for 30 days. The cookie is renewed on every visit, so active users stay in.
SESSION_DAYS = 30
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=SESSION_DAYS * 86400,
                   https_only=ON_VERCEL, same_site="lax")

# Vercel sits in front of the app as a proxy; trust its X-Forwarded-Proto so links
# built with url_for() use https:// (avoids "mixed content" blocks in the browser).
if ON_VERCEL:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# --- STATIC & TEMPLATES ---
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["LIVE_MODE"] = LIVE_MODE
templates.env.globals["DB_KIND"]   = DB_KIND   # "temporary" = no database connected on Vercel
# Which deploy is running, shown in the admin "no database" warning to help fix it
VERCEL_ENV    = os.environ.get("VERCEL_ENV", "")
DEPLOY_BRANCH = os.environ.get("VERCEL_GIT_COMMIT_REF", "")
DEPLOY_COMMIT = os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7]
templates.env.globals.update(DB_ENV_NAMES=DB_ENV_NAMES, VERCEL_ENV=VERCEL_ENV, DEPLOY_BRANCH=DEPLOY_BRANCH, DEPLOY_COMMIT=DEPLOY_COMMIT)

# Cache-buster for our own scripts (?v=...): changes whenever the files change, so browsers
# load the new live-update code right after a deploy instead of an old cached copy.
def _asset_version() -> str:
    h = hashlib.sha1()
    for name in ("ws-client.js", "maxseat-map.js", "peek.js"):
        try:
            with open(os.path.join(BASE_DIR, "static", "js", name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]
templates.env.globals["ASSET_V"] = _asset_version()
# CARTO map background key (https://carto.com/basemaps/apikey). Optional: without it the
# maps use free OpenStreetMap tiles. It is sent to browsers, like any map tile key.
templates.env.globals["CARTO_KEY"] = os.environ.get("CARTO_API_KEY", "").strip()
# {{ value|js }} gives a safe JavaScript value for inline handlers like onclick="f({{ x|js }})".
# (Plain '{{ x }}' inside onclick can be broken out of with a quote.)
templates.env.filters["js"] = lambda v: Markup(_html.escape(json.dumps(v), quote=True))

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        # Iterate over a copy: other tasks may connect/disconnect while we await sends
        message = json.dumps(data)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

# --- GLOBAL IN-MEMORY DATABASE ---
# Populated from the database at startup (see @app.on_event("startup") below).
# The default admin account is seeded only when the DB has no users yet.
_DEFAULT_ADMIN_PW = "admin@2026001"   # public in the README, so it must be changed on first use
_DEFAULT_ADMIN = {
    "password": _DEFAULT_ADMIN_PW, "role": "admin", "email": "admin@maxseat.ph",
    "must_change_password": True, "full_name": "System Administrator",
    "department": "Central Command Authority", "clearance": "Level 5 - Global"
}

users: dict        = {}
puv_database: list = []
audit_logs: list   = []
citations_db: list = []

# --- DATABASE SYNC ---
# On Vercel each request may run on a different, short-lived server, and servers don't share
# memory. So the database is the source of truth: the lists above are refreshed from it at the
# start of every request, and every change is written straight back with db_save_*/db_append_*.
_ready = False

def _ensure_ready():
    """Create tables and seed the default admin once per server (replaces the old startup event,
    which serverless platforms don't reliably run)."""
    global _ready
    if _ready:
        return
    init_db()
    # strict=True: if the database can't be read we must NOT treat it as empty —
    # that used to re-create the admin account (resetting its password).
    existing = db_load_users(strict=True)
    if not existing:
        # First run: seed the default admin account
        seed = _DEFAULT_ADMIN.copy()
        seed["password"] = hash_password(seed["password"])
        db_save_user("admin", seed)
    else:
        # One-time upgrade: older accounts were saved with the password in plain text
        for uname, rec in existing.items():
            pw = rec.get("password")
            if isinstance(pw, str) and pw and not pw.startswith(_PW_PREFIX):
                rec["password"] = hash_password(pw)
                db_save_user(uname, rec)
    _ready = True

def reload_puvs():
    try:
        puv_database[:] = db_load_puvs(strict=True)
    except Exception as e:
        print(f"[DB] vehicle reload skipped, keeping current data: {e}")

def reload_from_db():
    """Refresh the in-memory copies in place (other modules hold references to these objects).
    If the database can't be read right now, keep what we have instead of wiping it."""
    try:
        fresh_users      = db_load_users(strict=True)
        fresh_puvs       = db_load_puvs(strict=True)
        fresh_citations  = db_load_citations(strict=True)
        fresh_complaints = db_load_complaints(strict=True)
    except Exception as e:
        print(f"[DB] reload skipped, keeping current data: {e}")
        return
    users.clear()
    users.update(fresh_users)
    puv_database[:]  = fresh_puvs
    citations_db[:]  = fresh_citations
    complaints_db[:] = fresh_complaints

# Endpoints that are called often and only need vehicle data
# (/api/live loads its own snapshot in one go, so it skips the reload here)
_LIGHT_PATHS = ("/api/sensor", "/api/driver/status", "/api/health")
_SELF_LOADING = ("/api/live",)

# Apps that log in with a Bearer token (phone app, Pi). They don't use the login cookie,
# so the same-site check below doesn't apply to them.
_TOKEN_PATHS = ("/api/sensor", "/api/mobile/", "/api/passenger/")

def _same_site_ok(request: Request) -> bool:
    """Block forms or scripts on OTHER websites from making changes with a user's login cookie."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if request.url.path.startswith(_TOKEN_PATHS):
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True                      # not sent by a browser form or script on another site
    return urlparse(origin).netloc.lower() == request.headers.get("host", "").lower()

@app.middleware("http")
async def sync_with_database(request: Request, call_next):
    path = request.url.path
    if not _same_site_ok(request):
        return JSONResponse({"success": False, "error": "Blocked: request came from another website."}, status_code=403)
    if not path.startswith("/static") and path not in ("/favicon.ico", "/robots.txt"):
        _ensure_ready()
        if path in _SELF_LOADING:
            pass
        elif path in _LIGHT_PATHS:
            reload_puvs()
        else:
            reload_from_db()
            request.state.users_fresh = True
    response = await call_next(request)
    # Browser safety settings on every answer
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if response.headers.get("content-type", "").startswith("text/html"):
        # Pages hold private data: don't keep copies, so Back after logging out shows nothing.
        # (While logged in, Back simply loads the page again.)
        response.headers["Cache-Control"] = "no-store"
    return response

# --- PASSWORDS ---
# Saved as a salted PBKDF2-SHA256 hash, never as the password itself.
_PW_PREFIX = "pbkdf2_sha256$"
_PW_ROUNDS = 200_000

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PW_ROUNDS)
    return f"{_PW_PREFIX}{_PW_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"

def verify_password(stored, given) -> bool:
    if not isinstance(stored, str) or not isinstance(given, str) or not stored:
        return False
    if stored.startswith(_PW_PREFIX):
        try:
            rounds, salt, digest = stored[len(_PW_PREFIX):].split("$")
            test = hashlib.pbkdf2_hmac("sha256", given.encode(), base64.b64decode(salt), int(rounds))
            return hmac.compare_digest(test, base64.b64decode(digest))
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(stored.encode(), given.encode())   # old plain-text record

def set_password(username: str, new_password: str, must_change: bool = False):
    """Save a new password and sign out every other place this account is logged in."""
    u = users[username]
    u["password"]             = hash_password(new_password)
    u["must_change_password"] = must_change
    u["session_ver"]          = int(u.get("session_ver", 0)) + 1
    db_save_user(username, u)

# --- LOGIN LIMITS ---
# After 8 wrong passwords in 15 minutes, that account can't log in for 15 minutes.
# Kept in the database so every server sees the same count.
_login_fails = TokenStore("loginfail")
_FAIL_LIMIT, _FAIL_WINDOW = 8, 15 * 60

def _fail_key(kind: str, username: str) -> str:
    # One count per account, shared by the website and both phone apps
    return "fail:" + hashlib.sha256(f"acct:{str(username).strip().lower()}".encode()).hexdigest()[:40]

def login_locked(kind: str, username: str) -> bool:
    rec = _login_fails.get(_fail_key(kind, username)) or {}
    return rec.get("n", 0) >= _FAIL_LIMIT and time.time() - rec.get("t", 0) < _FAIL_WINDOW

def note_login_fail(kind: str, username: str):
    key = _fail_key(kind, username)
    rec = _login_fails.get(key) or {}
    if time.time() - rec.get("t", 0) > _FAIL_WINDOW:
        rec = {"n": 0, "t": time.time()}
    rec["n"] = rec.get("n", 0) + 1
    try:
        _login_fails[key] = rec
    except Exception:
        pass

def clear_login_fails(kind: str, username: str):
    _login_fails.pop(_fail_key(kind, username), None)

LOCKED_MSG = "Too many wrong passwords. Please wait 15 minutes and try again."

def check_password_login(kind: str, username: str, password: str, role: str):
    """Returns (user, error). Upgrades an old plain-text password to a hash on success."""
    if login_locked(kind, username):
        return None, LOCKED_MSG
    user = users.get(username)
    if not user or user.get("role") != role or not verify_password(user.get("password"), password):
        note_login_fail(kind, username)
        return None, None
    clear_login_fails(kind, username)
    if not str(user.get("password", "")).startswith(_PW_PREFIX):
        user["password"] = hash_password(password)
        db_save_user(username, user)
    return user, None

# --- TEXT FROM USERS ---
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
def clean_text(value, max_len: int = 120) -> str:
    """Plain, trimmed, length-limited text (no control characters, never a dict or list)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return _CTRL.sub("", str(value)).strip()[:max_len]

# --- HELPERS ---
def _session_ok(request: Request) -> bool:
    """The login cookie is only good while the account still exists with the same role and
    its password hasn't been changed or reset since (that signs out old logins)."""
    sess = request.session
    if not sess.get("logged_in"):
        return False
    if not getattr(request.state, "users_fresh", False):
        return True                      # accounts weren't re-read on this quick request
    u = users.get(sess.get("username"))
    if not u or u.get("role") != sess.get("role") or int(u.get("session_ver", 0)) != int(sess.get("ver", 0)):
        sess.clear()
        return False
    return True

def is_logged_in(request: Request):
    return _session_ok(request)

def get_role(request: Request):
    return request.session.get("role") if _session_ok(request) else None

def start_session(request: Request, username: str, user: dict):
    request.session.clear()              # new cookie contents on every login
    request.session['logged_in'] = True
    request.session['username']  = username
    request.session['role']      = user.get("role")
    request.session['ver']       = int(user.get("session_ver", 0))

def requires_password_change(request: Request):
    return request.session.get("pending_password_change", False)

def flash(request: Request, message: str):
    request.session["_flash"] = message

def get_flash(request: Request):
    msg = request.session.pop("_flash", None)
    return [msg] if msg else []

def get_coop_puvs(username: str):
    """Return only PUVs belonging to a cooperative.
    Vehicles are tagged with coop_owner when added via /api/coop/add_vehicle.
    Falls back to company-name matching for legacy vehicles.
    """
    udata = users.get(username, {})
    org   = udata.get('organization', '').strip().lower()

    result = []
    for p in puv_database:
        # Primary: vehicle was added by this coop
        if p.get('coop_owner') == username:
            result.append(p)
            continue
        # Secondary: older vehicles with no owner, matched by company name
        if p.get('coop_owner'):
            continue
        company = p.get('company', '').strip().lower()
        if org and company and (org in company or company in org):
            result.append(p)
    return result

def _puv_for_role(request: Request, puv_id):
    """The vehicle with this id, if the logged-in user may change it: admins any vehicle,
    a cooperative only its own. Everyone else gets None."""
    pid  = _as_int(puv_id, None)
    role = get_role(request)
    if role == 'admin':
        pool = puv_database
    elif role == 'cooperative':
        pool = get_coop_puvs(request.session.get('username'))
    else:
        return None
    return next((p for p in pool if p.get('id') == pid), None)

CDO_CENTER = (8.4822, 124.6472)   # where a vehicle is shown until its GPS reports a position

def _valid_coords(lat, lng):
    """(lat, lng) as floats, or None for missing/garbage values and 0,0 (a GPS with no fix)."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if abs(lat) < 0.001 and abs(lng) < 0.001:
        return None
    return lat, lng

def _has_gps(p: dict) -> bool:
    """True once the vehicle has a real GPS position (not just the CDO placeholder)."""
    coords = _valid_coords(p.get('lat'), p.get('lng'))
    if not coords:
        return False
    if p.get('gps_at'):
        return True
    # Older records have no gps_at: count them as tracked unless they still sit on the placeholder
    return not (abs(coords[0] - CDO_CENTER[0]) < 1e-6 and abs(coords[1] - CDO_CENTER[1]) < 1e-6)

def _normalize_puv(p: dict):
    """Guard against missing/null sensor values so templates never compare None with numbers."""
    p['passengers'] = p.get('passengers') or 0
    p['capacity']   = p.get('capacity') or 0
    p['temp']       = p.get('temp') or 0
    p.setdefault('status', 'Active')
    coords = _valid_coords(p.get('lat'), p.get('lng'))
    p['lat'], p['lng'] = coords if coords else CDO_CENTER   # maps never get null/text coordinates
    p['has_gps'] = _has_gps(p)

def compute_puv_stats():
    for p in puv_database:
        _normalize_puv(p)
        p['is_violator']    = p['passengers'] > p['capacity']
        p['is_overheating'] = p.get('temp', 0) >= 37.5
        p['load_percentage'] = int((p['passengers'] / p['capacity']) * 100) if p['capacity'] > 0 else 0
    return {
        "total_units":       len(puv_database),
        "active_violations": len([p for p in puv_database if p['is_violator']]),
        "thermal_warnings":  len([p for p in puv_database if p.get('is_overheating')]),
        "moving_units":      len([p for p in puv_database if p['status'] == 'Active']),
    }

def compute_puv_stats_for(puvs: list):
    for p in puvs:
        _normalize_puv(p)
        p['is_violator']    = p['passengers'] > p['capacity']
        p['is_overheating'] = p.get('temp', 0) >= 37.5
        p['load_percentage'] = int((p['passengers'] / p['capacity']) * 100) if p['capacity'] > 0 else 0
    return {
        "total_units":       len(puvs),
        "active_violations": len([p for p in puvs if p['is_violator']]),
        "thermal_warnings":  len([p for p in puvs if p.get('is_overheating')]),
        "moving_units":      len([p for p in puvs if p['status'] == 'Active']),
    }

def find_driver_puv(username: str):
    """Return the PUV assigned to this driver, or None if no vehicle is assigned yet."""
    if not username:
        return None
    return next((p for p in puv_database if p.get('username') == username), None)

def puv_live_payload(puv: dict) -> dict:
    """Single shape for every real-time update pushed to browsers over /ws."""
    passengers     = puv.get('passengers', 0) or 0
    capacity       = puv.get('capacity', 0) or 0
    temp           = puv.get('temp', 0) or 0
    is_violator    = passengers > capacity
    is_overheating = temp >= 37.5
    return {
        "type":           "violation" if is_violator or is_overheating else "update",
        "puv_id":         puv.get('id'),
        "plate":          puv.get('plate', ''),
        "company":        puv.get('company', ''),
        "driver":         puv.get('driver', ''),
        "passengers":     passengers,
        "capacity":       capacity,
        "temp":           temp,
        "is_violator":    is_violator,
        "is_overheating": is_overheating,
        "lat":            (_valid_coords(puv.get('lat'), puv.get('lng')) or CDO_CENTER)[0],
        "lng":            (_valid_coords(puv.get('lat'), puv.get('lng')) or CDO_CENTER)[1],
        "has_gps":        _has_gps(puv),
        "loc_name":       puv.get('loc_name', ''),
        "speed":          puv.get('speed', ''),
        "last_update":    puv.get('last_update', ''),
        "humidity":       puv.get('humidity'),
        "device_ts":      puv.get('device_ts'),     # when the in-vehicle counter last reported (Unix time)
        "gps_at":         puv.get('gps_at'),        # time of the last GPS position, e.g. "4:09:33 AM"
        "gps_ts":         puv.get('gps_ts'),        # same, as Unix time (for "x min ago")
    }

def _as_int(value, fallback):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return fallback

def _as_float(value, fallback):
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return fallback

async def notify_data_changed(scope: str, vehicle_id=None):
    """Tell every open page that fleet/citation data changed, so it refreshes itself
    (vehicle added/removed, driver reassigned, capacity changed, citation issued...)."""
    await manager.broadcast({"type": "data_changed", "scope": scope, "vehicle_id": vehicle_id})

def log_audit(actor: str, event: str, ip: str, category: str = "blue"):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "actor": actor, "event": event, "ip": ip, "category": category
    }
    audit_logs.insert(0, entry)
    db_append_audit(entry)

def TR(request: Request, name: str, context: dict = None):
    context = dict(context or {})
    context.setdefault("users_data", users)
    context.setdefault("new_reports", count_new_reports(request))
    return templates.TemplateResponse(request=request, name=name, context=context)

# ══════════════════════════════════════════════
# NOTIFICATION HELPERS
# ══════════════════════════════════════════════

def build_credentials_message(full_name: str, username: str, password: str, role: str) -> str:
    role_labels = {
        "admin":       "System Administrator",
        "enforcer":    "Field Enforcer",
        "driver":      "Driver",
        "cooperative": "Cooperative Manager",
        "passenger":   "Passenger",
    }
    role_label = role_labels.get(role, role.capitalize())
    return (
        f"Good day, {full_name}!\n\n"
        f"You have been registered on the MaxSeat Alert System as a {role_label}.\n\n"
        f"Your login credentials:\n"
        f"  Username : {username}\n"
        f"  Password : {password}\n\n"
        f"Please log in and change your password immediately.\n"
        f"Login at: https://maxseat-final.vercel.app/login\n\n"
        f"NOTE: Do not share your credentials with anyone.\n"
        f"- MaxSeat Alert System"
    )

async def send_email(to_email: str, full_name: str, message: str) -> dict:
    """Send the login details by email: Gmail when GMAIL_APP_PASSWORD is set, else Resend."""
    if not to_email:
        return {"success": False, "reason": "No email address provided."}
    if not GOOGLE_SCRIPT_URL and not RESEND_API_KEY and not GMAIL_APP_PASSWORD:
        return {"success": False, "reason": "Email not set up. Add GOOGLE_SCRIPT_URL and GOOGLE_SCRIPT_SECRET in the Vercel project environment variables."}

    safe_msg  = _html.escape(message)             # names and passwords are text, not HTML
    html_body = safe_msg.replace("\n\n", "</p><p>").replace("\n", "<br>")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
      <div style="background:#0f172a;padding:24px 32px;">
        <h2 style="color:#fff;margin:0;font-size:20px;">MaxSeat Alert System</h2>
        <p style="color:#94a3b8;margin:4px 0 0;font-size:13px;">Account Credentials</p>
      </div>
      <div style="padding:28px 32px;background:#fff;">
        <p style="color:#0f172a;">Good day, <strong>{_html.escape(full_name)}</strong>!</p>
        <p>{html_body}</p>
        <div style="background:#f1f5f9;border-radius:8px;padding:16px 20px;margin:20px 0;font-family:monospace;font-size:14px;">
          {safe_msg.split("credentials:")[1].split("Please")[0].strip().replace(chr(10),"<br>") if "credentials:" in safe_msg else ""}
        </div>
        <p style="color:#64748b;font-size:12px;">Do not share your credentials with anyone.</p>
      </div>
      <div style="background:#f8fafc;padding:16px 32px;border-top:1px solid #e2e8f0;">
        <p style="color:#94a3b8;font-size:11px;margin:0;">MaxSeat Alert System, CDO Region</p>
      </div>
    </div>"""

    subject = "MaxSeat Alert: Your Account Credentials"

    if GOOGLE_SCRIPT_URL:
        try:
            # Apps Script answers a POST with a redirect to the result; following it as a GET is correct
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.post(GOOGLE_SCRIPT_URL, json={
                    "secret":  GOOGLE_SCRIPT_SECRET,
                    "to":      to_email,
                    "subject": subject,
                    "text":    message,
                    "html":    html,
                    "name":    GMAIL_SENDER_NAME,
                })
            try:
                data = resp.json()
            except ValueError:
                print(f"[EMAIL] Apps Script gave a non-JSON reply {resp.status_code}: {resp.text[:300]}")
                return {"success": False, "reason": "The Google script did not answer properly. Check GOOGLE_SCRIPT_URL ends in /exec and the script is deployed with access set to Anyone."}
            if data.get("ok"):
                return {"success": True}
            return {"success": False, "reason": f"Google script: {data.get('error', 'unknown error')}"}
        except Exception as e:
            print(f"[EMAIL] Apps Script call failed for {to_email}: {e}")
            return {"success": False, "reason": f"Could not reach the Google script: {e}"}

    if GMAIL_APP_PASSWORD:
        msg = EmailMessage()
        msg["From"]    = f"{GMAIL_SENDER_NAME} <{GMAIL_ADDRESS}>"
        msg["To"]      = to_email
        msg["Subject"] = "MaxSeat Alert: Your Account Credentials"
        msg.set_content(message)
        msg.add_alternative(html, subtype="html")

        # Try Gmail's SSL port first, then its STARTTLS port. A fixed EHLO name is
        # used because the server's own host name can be one Gmail refuses.
        def _via_465():
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, local_hostname="maxseat-final.vercel.app",
                                  context=ssl.create_default_context(), timeout=10) as smtp:
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                smtp.send_message(msg)

        def _via_587():
            with smtplib.SMTP("smtp.gmail.com", 587, local_hostname="maxseat-final.vercel.app", timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                smtp.send_message(msg)

        errors = []
        for port, attempt in ((465, _via_465), (587, _via_587)):
            try:
                await asyncio.to_thread(attempt)
                return {"success": True}
            except smtplib.SMTPAuthenticationError:
                return {"success": False, "reason": "Gmail rejected the login. Check GMAIL_ADDRESS, and that GMAIL_APP_PASSWORD is the 16-letter App Password, not the normal password."}
            except Exception as e:
                print(f"[EMAIL] Gmail port {port} failed for {to_email}: {type(e).__name__}: {e}")
                errors.append(f"port {port}: {e or type(e).__name__}")
        return {"success": False, "reason": "Gmail could not send it (" + "; ".join(errors) + ")"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from":    f"{GMAIL_SENDER_NAME} <onboarding@resend.dev>",
                    "to":      [to_email],
                    "subject": "MaxSeat Alert: Your Account Credentials",
                    "text":    message,
                    "html":    html,
                }
            )
        if resp.status_code in (200, 201):
            return {"success": True}
        else:
            print(f"[EMAIL] Resend error {resp.status_code}: {resp.text}")
            return {"success": False, "reason": resp.text}
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to_email}: {e}")
        return {"success": False, "reason": str(e)}

async def notify_new_user(user_data: dict, username: str, password: str):
    """Email the new user their login details (no SMS)."""
    full_name = user_data.get("full_name", username)
    role      = user_data.get("role", "")
    email     = user_data.get("email", "")
    message   = build_credentials_message(full_name, username, password, role)

    results = {}
    if email:
        results["email"] = await send_email(email, full_name, message)
    return results

# --- ROOT ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if is_logged_in(request):
        if requires_password_change(request):
            return RedirectResponse(url="/change_password", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)

# --- AUTHENTICATION ---
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    # Going Back to the login page while still logged in just returns to the app
    if is_logged_in(request):
        if requires_password_change(request):
            return RedirectResponse(url="/change_password", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "login.html", {"messages": get_flash(request)})

@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    role: str = Form(...),
    username: str = Form(...),
    password: str = Form(...)
):
    username = username.strip()
    if role == "passenger":
        flash(request, "Passengers use the MaxSeat phone app.")
        return RedirectResponse(url="/login", status_code=302)
    user, err = check_password_login("web", username, password, role)
    if user:
        start_session(request, username, user)
        log_audit(username, "SESSION_INITIATED", request.client.host if request.client else "unknown", "green")
        # The built-in admin password is public, so it has to be changed before going on
        if user.get('must_change_password', False) or (username == "admin" and password == _DEFAULT_ADMIN_PW):
            request.session['pending_password_change'] = True
            return RedirectResponse(url="/change_password", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)
    flash(request, err or "Invalid credentials for the selected role.")
    return RedirectResponse(url="/login", status_code=302)

@app.get("/change_password", response_class=HTMLResponse)
async def change_password_get(request: Request):
    if not is_logged_in(request) or not requires_password_change(request):
        return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "change_password.html", {"messages": get_flash(request)})

@app.post("/change_password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...)
):
    if not is_logged_in(request) or not requires_password_change(request):
        return RedirectResponse(url="/login", status_code=302)
    if new_password != confirm_password:
        flash(request, "Passwords do not match.")
        return RedirectResponse(url="/change_password", status_code=302)
    if len(new_password) < 6:
        flash(request, "Password must be at least 6 characters.")
        return RedirectResponse(url="/change_password", status_code=302)
    username = request.session.get('username')
    if new_password == _DEFAULT_ADMIN_PW or verify_password(users[username].get('password'), new_password):
        flash(request, "Please choose a new password, not the old one.")
        return RedirectResponse(url="/change_password", status_code=302)
    set_password(username, new_password)
    request.session['ver'] = users[username]['session_ver']    # keep THIS login, sign out the others
    request.session.pop('pending_password_change', None)
    log_audit(username, "PASSWORD_CHANGED", request.client.host if request.client else "unknown", "green")
    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

# --- SETTINGS ---
@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    if not is_logged_in(request):          return RedirectResponse(url="/login", status_code=302)
    if requires_password_change(request):  return RedirectResponse(url="/change_password", status_code=302)
    user_data = users.get(request.session.get('username'), {})
    return TR(request, "settings.html", {"role": get_role(request), "user_data": user_data, "messages": get_flash(request)})

@app.post("/update_settings")
async def update_settings_post(request: Request):
    if not is_logged_in(request): return RedirectResponse(url="/login", status_code=302)
    username = request.session.get('username')
    role     = get_role(request)
    form_data = await request.form()
    allowed = {'admin': ['email'], 'enforcer': ['mobile', 'shift_status'], 'driver': ['mobile', 'emergency_contact'], 'cooperative': ['mobile', 'email', 'address']}
    for key, value in form_data.items():
        if key in allowed.get(role, []):
            users[username][key] = clean_text(value)
    db_save_user(username, users[username])
    flash(request, "Settings updated successfully.")
    return RedirectResponse(url="/settings", status_code=302)

# --- DASHBOARDS ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_logged_in(request):         return RedirectResponse(url="/login", status_code=302)
    if requires_password_change(request): return RedirectResponse(url="/change_password", status_code=302)
    role  = get_role(request)
    stats = compute_puv_stats()
    if role == 'admin':
        return TR(request, "admin/dashboard.html", {
            "puvs": puv_database, "role": role, "stats": stats,
            "citations": citations_db, "audit_logs": audit_logs
        })
    elif role == 'enforcer':
        sorted_puvs = sorted(puv_database, key=lambda x: (x['passengers'] / x['capacity']) if x['capacity'] > 0 else 0, reverse=True)
        return TR(request, "enforcer/dashboard.html", {"puvs": sorted_puvs, "role": role, "stats": stats,
                                                       "recent_reports": visible_reports(request)[:4]})
    elif role == 'driver':
        # my_puv is None when the cooperative hasn't assigned this driver a vehicle yet;
        # the template shows a "no vehicle assigned" screen instead of crashing.
        my_puv = find_driver_puv(request.session.get('username'))
        if my_puv:
            _normalize_puv(my_puv)
        return TR(request, "driver/dashboard.html", {"puv": my_puv, "role": role,
                                                     "server_now": int(time.time()),
                                                     "device_online_s": DEVICE_ONLINE_S})
    elif role == 'cooperative':
        username   = request.session.get('username')
        udata      = users.get(username, {})
        coop_puvs  = get_coop_puvs(username)
        coop_stats = compute_puv_stats_for(coop_puvs)
        return TR(request, "cooperative/dashboard.html", {"puvs": coop_puvs, "role": role, "stats": coop_stats, "user_data": udata,
                                                          "recent_reports": visible_reports(request)[:4]})
    return RedirectResponse(url="/logout", status_code=302)

# ==========================================
# ADMIN ROUTES
# ==========================================
@app.get("/fleet", response_class=HTMLResponse)
async def fleet(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "admin/fleet.html", {"role": "admin", "puvs": puv_database})

@app.get("/user_management", response_class=HTMLResponse)
async def user_management(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "admin/user_management.html", {"role": "admin", "users_db": users, "messages": get_flash(request)})

@app.post("/api/create_user")
async def create_user(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)

    data     = await request.json()
    data     = {k: (clean_text(v, 200) if k != "password" else v) for k, v in data.items()} if isinstance(data, dict) else {}
    username = data.get("username", "").strip()
    role     = data.get("role", "driver")
    password = data.get("password", "")

    if role not in ("admin", "enforcer", "driver", "cooperative"):
        return JSONResponse({"success": False, "error": "Unknown role."})
    if not isinstance(password, str):
        return JSONResponse({"success": False, "error": "Password must be text."})
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,40}", username or ""):
        return JSONResponse({"success": False, "error": "Username can only use letters, numbers, dot, dash, underscore or @."})
    if not username or username in users:
        return JSONResponse({"success": False, "error": "Username already exists or is empty."})
    if len(password) < 6:
        return JSONResponse({"success": False, "error": "Password must be at least 6 characters."})

    # Base record — shared by every role
    record = {
        "password":             hash_password(password),
        "role":                 role,
        "full_name":            data.get("full_name", ""),
        "must_change_password": True,
    }

    # Role-specific fields
    if role == "admin":
        record["email"]      = data.get("email", "")
        record["department"] = data.get("department", "")
        record["clearance"]  = "Level 1"

    elif role == "enforcer":
        record["email"]        = data.get("email", "")
        record["badge"]        = data.get("badge", "")
        record["precinct"]     = data.get("precinct", "")
        record["mobile"]       = data.get("mobile", "")
        record["shift_status"] = "On Duty"

    elif role == "driver":
        record["license"]           = data.get("license", "")
        record["operator"]          = data.get("operator", "")
        record["mobile"]            = data.get("mobile", "")
        record["email"]             = data.get("email", "")
        record["emergency_contact"] = data.get("emergency_contact", "")
        record["plate"]             = ""  # assigned later

    elif role == "cooperative":
        record["email"]        = data.get("email", "")
        record["organization"] = data.get("organization", "")
        record["position"]     = data.get("position", "")
        record["mobile"]       = data.get("mobile", "")
        record["address"]      = data.get("address", "")

    elif role == "passenger":
        record["mobile"] = data.get("mobile", "")
        record["email"]  = data.get("email", "")

    users[username] = record
    db_save_user(username, record)
    log_audit(
        request.session.get('username', 'admin'),
        f"USER_CREATED: {username} ({role.upper()})",
        request.client.host if request.client else "unknown",
        "blue"
    )
    # Email the login details to the new user
    notify_results = await notify_new_user(record, username, password)
    return JSONResponse({"success": True, "notifications": notify_results})

@app.post("/api/delete_puv")
async def delete_puv(request: Request):
    if get_role(request) not in ['admin', 'cooperative']:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data   = await request.json()
    puv_id = data.get("puv_id")
    puv    = _puv_for_role(request, puv_id)
    if not puv:
        return JSONResponse({"success": False, "error": "Vehicle not found."})
    puv_database.remove(puv)
    db_delete_puv(puv['id'])
    log_audit(request.session.get('username', 'admin'), f"PUV_REMOVED: {puv['plate']}", request.client.host if request.client else "unknown", "red")
    await notify_data_changed("fleet", puv['id'])
    return JSONResponse({"success": True, "plate": puv['plate']})

@app.post("/api/delete_user")
async def delete_user(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    username = data.get("username", "")
    if username == request.session.get('username'):
        return JSONResponse({"success": False, "error": "You cannot delete your own account."})
    if username in users:
        del users[username]
        db_delete_user(username)
        log_audit(request.session.get('username', 'admin'), f"USER_DELETED: {username}",
                  request.client.host if request.client else "unknown", "red")
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "User not found."})

# --- ADMIN: view account details / reset password ---
_ROLE_FIELDS = {
    "admin":       [("email", "Email"), ("department", "Department"), ("clearance", "Clearance")],
    "enforcer":    [("email", "Email"), ("mobile", "Mobile"), ("badge", "Badge no."), ("precinct", "Precinct"), ("shift_status", "Shift status")],
    "driver":      [("mobile", "Mobile"), ("license", "License no."), ("operator", "Operator"), ("emergency_contact", "Emergency contact")],
    "cooperative": [("email", "Email"), ("mobile", "Mobile"), ("organization", "Organization"), ("position", "Position"), ("address", "Address")],
    "passenger":   [("email", "Email"), ("mobile", "Mobile")],
}

@app.get("/api/admin/user/{username}")
async def admin_user_details(username: str, request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    u = users.get(username)
    if not u:
        return JSONResponse({"success": False, "error": "User not found."}, status_code=404)
    role    = u.get("role", "")
    details = [{"label": label, "value": u.get(key, "")} for key, label in _ROLE_FIELDS.get(role, []) if u.get(key)]
    vehicle = None
    if role == "driver":
        puv = find_driver_puv(username)
        if puv:
            vehicle = f"{puv.get('plate', '')} ({puv.get('company', '')})"
    elif role == "cooperative":
        coop_puvs = get_coop_puvs(username)
        vehicle   = f"{len(coop_puvs)} vehicle{'s' if len(coop_puvs) != 1 else ''}" + (
            ": " + ", ".join(p.get('plate', '') for p in coop_puvs[:6]) if coop_puvs else "")
    log_audit(request.session.get('username', 'admin'), f"ACCOUNT_VIEWED: {username}",
              request.client.host if request.client else "unknown", "blue")
    return JSONResponse({
        "success":          True,
        "username":         username,
        "full_name":        u.get("full_name", ""),
        "role":             role,
        "password_pending": bool(u.get("must_change_password", False)),
        "details":          details,
        "vehicle":          vehicle,
    })

@app.post("/api/admin/reset_password")
async def admin_reset_password(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    username = data.get("username", "")
    if username not in users:
        return JSONResponse({"success": False, "error": "User not found."})
    if username == request.session.get('username'):
        return JSONResponse({"success": False, "error": "Use Settings to change your own password."})
    # Readable temporary password; the user must change it on next login
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    temp_pw  = "".join(secrets.choice(alphabet) for _ in range(10))
    set_password(username, temp_pw, must_change=True)     # also signs them out everywhere
    log_audit(request.session.get('username', 'admin'), f"PASSWORD_RESET: {username}",
              request.client.host if request.client else "unknown", "red")
    return JSONResponse({"success": True, "username": username, "password": temp_pw})

@app.get("/configure_seating", response_class=HTMLResponse)
async def configure_seating(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    username  = request.session.get('username')
    coop_puvs = get_coop_puvs(username)
    return TR(request, "cooperative/configure_seating.html", {"role": "cooperative", "puvs": coop_puvs, "messages": get_flash(request)})

@app.post("/api/update_capacity")
async def update_capacity(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = _as_int(data.get("capacity"), 0)
    puv = _puv_for_role(request, puv_id)
    if puv and 0 < new_capacity <= 100:
        puv['capacity'] = int(new_capacity)
        db_save_puv(puv['id'], puv)
        await notify_data_changed("fleet", puv['id'])
        return JSONResponse({"success": True, "plate": puv['plate'], "capacity": puv['capacity']})
    return JSONResponse({"success": False, "error": "Vehicle not found or invalid capacity."})

@app.get("/audit_logs", response_class=HTMLResponse)
async def audit_logs_page(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    audit_logs[:] = db_load_audit_logs()
    return TR(request, "admin/audit_logs.html", {"role": "admin", "logs": audit_logs})

@app.get("/records", response_class=HTMLResponse)
async def records(request: Request):
    role = get_role(request)
    if role not in ['admin', 'enforcer', 'cooperative']: return RedirectResponse(url="/dashboard", status_code=302)
    if role == 'cooperative':
        username  = request.session.get('username')
        puvs      = get_coop_puvs(username)
        stats     = compute_puv_stats_for(puvs)
    else:
        puvs  = puv_database
        stats = compute_puv_stats()
    return TR(request, "records.html", {"role": role, "puvs": puvs, "stats": stats})

@app.get("/admin_reports", response_class=HTMLResponse)
async def admin_reports(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "enforcer/reports.html", {"role": "admin", "citations": citations_db})

# ==========================================
# ENFORCER ROUTES
# ==========================================
@app.get("/intercepts", response_class=HTMLResponse)
async def intercepts(request: Request):
    if not is_logged_in(request): return RedirectResponse(url="/login", status_code=302)
    if get_role(request) not in ('admin', 'enforcer', 'cooperative'): return RedirectResponse(url="/dashboard", status_code=302)
    violators = [p for p in puv_database if p['passengers'] > p['capacity'] or p.get('temp', 0) >= 37.5]
    stats     = compute_puv_stats()
    return TR(request, "intercepts.html", {"role": get_role(request), "puvs": violators, "stats": stats})

@app.get("/citations", response_class=HTMLResponse)
async def citations(request: Request):
    if get_role(request) != 'enforcer': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "enforcer/citations.html", {"role": "enforcer", "citations": citations_db})

@app.get("/reports", response_class=HTMLResponse)
async def reports(request: Request):
    if get_role(request) != 'enforcer': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "enforcer/reports.html", {"role": "enforcer", "citations": citations_db})

@app.get("/dispatch_orders", response_class=HTMLResponse)
async def dispatch_orders(request: Request):
    if get_role(request) != 'enforcer': return RedirectResponse(url="/dashboard", status_code=302)
    violators = [p for p in puv_database if p['passengers'] > p['capacity'] or p.get('temp', 0) >= 37.5]
    return TR(request, "enforcer/dispatch_order.html", {"role": "enforcer", "puvs": violators})

@app.get("/interception_status", response_class=HTMLResponse)
async def interception_status(request: Request):
    if get_role(request) != 'enforcer': return RedirectResponse(url="/dashboard", status_code=302)
    violators = [p for p in puv_database if p['passengers'] > p['capacity'] or p.get('temp', 0) >= 37.5]
    return TR(request, "enforcer/interception_status.html", {"role": "enforcer", "puvs": violators, "messages": get_flash(request)})

@app.post("/submit_interception")
async def submit_interception(
    request: Request,
    plate:  str = Form(...),
    action: str = Form(...),
    notes:  str = Form(...)
):
    if get_role(request) != 'enforcer': return RedirectResponse(url="/login", status_code=302)
    plate, notes = clean_text(plate, 20), clean_text(notes, 1000)
    citation = {
        "serial":  f"CT-{len(citations_db)+1:04d}-X",
        "plate":   plate,
        "company": next((p['company'] for p in puv_database if p['plate'] == plate), "Unknown"),
        "officer": request.session.get('username', 'Officer'),
        "fine":    "₱5,000.00" if action == "ticket" else "₱0.00",
        "status":  "PENDING" if action == "ticket" else "RESOLVED",
        "date":    datetime.date.today().isoformat()
    }
    citations_db.append(citation)
    db_append_citation(citation)
    log_audit(request.session.get('username', 'enforcer'), "INTERCEPTION_LOGGED", request.client.host if request.client else "unknown", "red")
    await notify_data_changed("citations")
    flash(request, f"Interception report for {plate} submitted successfully.")
    return RedirectResponse(url="/interception_status", status_code=302)

# ==========================================
# COOPERATIVE ROUTES
# ==========================================
@app.get("/coop/fleet", response_class=HTMLResponse)
async def coop_fleet(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    username  = request.session.get('username')
    udata     = users.get(username, {})
    coop_puvs = get_coop_puvs(username)
    drivers   = {k: v for k, v in users.items() if v.get('role') == 'driver'}
    return TR(request, "cooperative/fleet.html", {"role": "cooperative", "puvs": coop_puvs, "drivers": drivers, "user_data": udata})

@app.get("/coop/drivers", response_class=HTMLResponse)
async def coop_drivers(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    username  = request.session.get('username')
    coop_puvs = get_coop_puvs(username)
    # Only show drivers assigned to this coop's vehicles
    coop_driver_usernames = {p.get('username') for p in coop_puvs if p.get('username')}
    drivers = {k: v for k, v in users.items() if v.get('role') == 'driver' and k in coop_driver_usernames}
    # Also include unassigned drivers for assignment purposes
    all_drivers = {k: v for k, v in users.items() if v.get('role') == 'driver'}
    return TR(request, "cooperative/drivers.html", {"role": "cooperative", "drivers": drivers, "all_drivers": all_drivers, "puvs": coop_puvs, "messages": get_flash(request)})

@app.get("/coop/violations", response_class=HTMLResponse)
async def coop_violations(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    username  = request.session.get('username')
    coop_puvs = get_coop_puvs(username)
    coop_plates = {p['plate'] for p in coop_puvs}
    coop_cits = [c for c in citations_db if c.get('plate') in coop_plates]
    stats = compute_puv_stats_for(coop_puvs)
    return TR(request, "cooperative/violations.html", {"role": "cooperative", "puvs": coop_puvs, "citations": coop_cits, "stats": stats})

@app.get("/coop/reports", response_class=HTMLResponse)
async def coop_reports(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    username    = request.session.get('username')
    coop_puvs   = get_coop_puvs(username)
    coop_plates = {p['plate'] for p in coop_puvs}
    coop_cits   = [c for c in citations_db if c.get('plate') in coop_plates]
    stats = compute_puv_stats_for(coop_puvs)
    return TR(request, "cooperative/reports.html", {"role": "cooperative", "citations": coop_cits, "puvs": coop_puvs, "stats": stats})

@app.post("/api/coop/update_capacity")
async def coop_update_capacity(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = _as_int(data.get("capacity"), 0)
    puv = _puv_for_role(request, puv_id)
    if puv and 0 < new_capacity <= 100:
        puv['capacity'] = int(new_capacity)
        db_save_puv(puv['id'], puv)
        log_audit(request.session.get('username', 'cooperative'), "CAPACITY_UPDATED", request.client.host if request.client else "unknown", "blue")
        await notify_data_changed("fleet", puv['id'])
        return JSONResponse({"success": True, "plate": puv['plate'], "capacity": puv['capacity']})
    return JSONResponse({"success": False, "error": "Vehicle not found or invalid capacity."})

@app.post("/api/coop/update_driver")
async def coop_update_driver(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    username = data.get("username", "")
    allowed_fields = ["full_name", "license", "operator", "mobile", "emergency_contact", "email"]
    if username not in users or users[username].get("role") != "driver":
        return JSONResponse({"success": False, "error": "Driver not found."})
    my_ids = {p["id"] for p in get_coop_puvs(request.session.get('username'))}
    if any(p.get("username") == username and p["id"] not in my_ids for p in puv_database):
        return JSONResponse({"success": False, "error": "This driver belongs to another cooperative's vehicle."}, status_code=403)
    for field in allowed_fields:
        if field in data and data[field] is not None:
            users[username][field] = clean_text(data[field])
    assigned_puv_id = data.get("assigned_puv_id")
    if assigned_puv_id is not None:
        for p in puv_database:
            if p["username"] == username:
                p["username"] = ""
                p["driver"]   = "Unassigned"
                db_save_puv(p["id"], p)
        if assigned_puv_id != "":
            puv = next((p for p in puv_database if p["id"] == _as_int(assigned_puv_id, -1) and p["id"] in my_ids), None)
            if puv:
                puv["username"] = username
                puv["driver"]   = users[username].get("full_name", username)
                db_save_puv(puv["id"], puv)
    db_save_user(username, users[username])
    log_audit(request.session.get('username', 'cooperative'), f"DRIVER_UPDATED: {username}", request.client.host if request.client else "unknown", "blue")
    await notify_data_changed("fleet")
    return JSONResponse({"success": True})

# ==========================================
# MOBILE API  (React Native — Field Enforcer)
# ==========================================
def get_mobile_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        username = mobile_tokens.get(token, strict=True)
    except Exception:
        raise ServerBusy()
    if not username:
        return None
    user = users.get(username)
    if getattr(request.state, "users_fresh", False) and (not user or user.get("role") != "enforcer"):
        return None                       # account deleted or changed
    return {"username": username, **(user or {})}

@app.post("/api/mobile/login")
async def mobile_login(request: Request):
    data     = await request.json()
    username = clean_text(data.get("username", ""), 60)
    password = data.get("password", "")
    user, err = check_password_login("mobile", username, password if isinstance(password, str) else "", "enforcer")
    if not user:
        return JSONResponse({"success": False, "error": err or "Invalid credentials."}, status_code=429 if err else 401)
    for t, u in list(mobile_tokens.items()):
        if u == username:
            del mobile_tokens[t]
    token = secrets.token_hex(32)
    mobile_tokens[token] = username
    log_audit(username, "MOBILE_LOGIN", request.client.host if request.client else "unknown", "green")
    return JSONResponse({
        "success":      True,
        "token":        token,
        "username":     username,
        "full_name":    user.get("full_name", username),
        "badge":        user.get("badge", ""),
        "precinct":     user.get("precinct", ""),
        "shift_status": user.get("shift_status", "On Duty"),
    })

@app.post("/api/mobile/logout")
async def mobile_logout(request: Request):
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    auth  = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    mobile_tokens.pop(token, None)
    return JSONResponse({"success": True})

@app.get("/api/mobile/dispatch")
async def mobile_dispatch(request: Request):
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    compute_puv_stats()
    orders = []
    for p in puv_database:
        violations = []
        if p["passengers"] > p["capacity"]:
            violations.append({
                "type":    "OVERLOAD",
                "label":   "Passenger overload",
                "detail":  f"{p['passengers']}/{p['capacity']} passengers, over the legal limit",
                "severity": "critical",
            })
        if p.get("temp", 0) >= 37.5:
            violations.append({
                "type":    "THERMAL",
                "label":   "Thermal alert",
                "detail":  f"Cabin temperature at {p['temp']}°C, above the 37.5°C limit",
                "severity": "warning",
            })
        if violations:
            orders.append({
                "puv_id":      p["id"],
                "plate":       p["plate"],
                "company":     p["company"],
                "driver":      p["driver"],
                "loc_name":    p["loc_name"],
                "lat":         p["lat"],
                "lng":         p["lng"],
                "has_gps":     p.get("has_gps", False),
                "passengers":  p["passengers"],
                "capacity":    p["capacity"],
                "temp":        p.get("temp", 0),
                "speed":       p.get("speed", "—"),
                "last_update": p.get("last_update", "—"),
                "schedule":    p.get("schedule", "—"),
                "violations":  violations,
            })
    return JSONResponse({"success": True, "dispatch_orders": orders, "total": len(orders)})

@app.post("/api/mobile/intercept")
async def mobile_intercept(request: Request):
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    data   = await request.json()
    plate  = clean_text(data.get("plate", ""), 20)
    action = clean_text(data.get("action", ""), 20)
    notes  = clean_text(data.get("notes", ""), 1000)
    if action not in ("ticket", "apprehend", "warning"):
        action = "warning" if action else ""
    if not plate or not action:
        return JSONResponse({"success": False, "error": "Plate and action are required."})
    serial = f"CT-{len(citations_db)+1:04d}-M"
    fine_map = {"ticket": "₱5,000.00", "apprehend": "₱10,000.00", "warning": "₱0.00"}
    citation = {
        "serial":  serial,
        "plate":   plate,
        "company": next((p["company"] for p in puv_database if p["plate"] == plate), "Unknown"),
        "officer": mobile_user["username"],
        "fine":    fine_map.get(action, "₱0.00"),
        "status":  "PENDING" if action in ("ticket", "apprehend") else "RESOLVED",
        "date":    datetime.date.today().isoformat(),
        "notes":   notes,
        "source":  "mobile",
    }
    citations_db.append(citation)
    db_append_citation(citation)
    log_audit(mobile_user["username"], f"MOBILE_INTERCEPT: {plate} · {action.upper()}", request.client.host if request.client else "unknown", "red")
    await notify_data_changed("citations")
    return JSONResponse({
        "success": True,
        "serial":  serial,
        "plate":   plate,
        "action":  action,
        "message": f"Interception report {serial} submitted successfully.",
    })

# ==========================================
# MOBILE API  (React Native — Passenger)
# ==========================================
def get_passenger_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        data = passenger_tokens.get(token, strict=True)
    except Exception:
        raise ServerBusy()
    if not data:
        return None
    u = users.get(data.get("username"))
    if getattr(request.state, "users_fresh", False) and (not u or u.get("role") != "passenger"):
        return None                       # account was deleted
    return data

@app.post("/api/passenger/register")
async def passenger_register(request: Request):
    data      = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "Bad request."}, status_code=400)
    username  = clean_text(data.get("username", ""), 60).lower()
    password  = data.get("password", "")
    full_name = clean_text(data.get("full_name", ""), 80)
    mobile    = clean_text(data.get("mobile", ""), 20)

    if not username:
        return JSONResponse({"error": "Username is required."}, status_code=400)
    if not re.fullmatch(r"[a-z0-9._@-]{3,40}", username):
        return JSONResponse({"error": "Username can use letters, numbers, dot, dash, underscore or @ (3 to 40 characters)."}, status_code=400)
    if not isinstance(password, str) or len(password) > 200:
        return JSONResponse({"error": "Password is not valid."}, status_code=400)
    if username in users:
        return JSONResponse({"error": "Username already taken."}, status_code=409)
    if len(password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
    if not full_name:
        return JSONResponse({"error": "Full name is required."}, status_code=400)

    record = {
        "password":             hash_password(password),
        "role":                 "passenger",
        "full_name":            full_name,
        "mobile":               mobile,
        "email":                clean_text(data.get("email", ""), 120),
        "must_change_password": False,
    }
    users[username] = record
    db_save_user(username, record)
    log_audit("self-register", f"PASSENGER_REGISTERED: {username}", request.client.host if request.client else "unknown", "green")

    # Auto-issue a token so the user is logged in immediately after registering
    token = secrets.token_hex(32)
    passenger_tokens[token] = {
        "username":  username,
        "full_name": full_name,
        "mobile":    mobile,
        "role":      "passenger",
    }
    return JSONResponse({
        "token":     token,
        "username":  username,
        "full_name": full_name,
        "mobile":    mobile,
        "role":      "passenger",
    }, status_code=201)

@app.post("/api/passenger/login")
async def passenger_login(request: Request):
    data     = await request.json()
    username = clean_text(data.get("username", ""), 60)
    password = data.get("password", "")
    # Registration saves usernames in lowercase, so "Bradley" must find "bradley"
    if username not in users and username.lower() in users:
        username = username.lower()
    user, err = check_password_login("passenger", username, password if isinstance(password, str) else "", "passenger")
    if not user:
        return JSONResponse({"error": err or "Invalid credentials."}, status_code=429 if err else 401)
    token = secrets.token_hex(32)
    passenger_tokens[token] = {
        "username":  username,
        "full_name": user.get("full_name", username),
        "mobile":    user.get("mobile", ""),
        "role":      "passenger",
    }
    log_audit(username, "PASSENGER_LOGIN", request.client.host if request.client else "unknown", "green")
    return JSONResponse({
        "token":     token,
        "username":  username,
        "full_name": user.get("full_name", username),
        "mobile":    user.get("mobile", ""),
        "role":      "passenger",
    })

@app.post("/api/passenger/logout")
async def passenger_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        passenger_tokens.pop(auth[7:], None)
    return JSONResponse({"ok": True})

@app.get("/api/passenger/puvs")
async def passenger_list_puvs(request: Request):
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    compute_puv_stats()            # fills in missing numbers and clean coordinates
    result = []
    for puv in puv_database:
        if puv.get("status", "Active") != "Active":
            continue
        load_pct   = round((puv["passengers"] / puv["capacity"]) * 100) if puv["capacity"] else 0
        overloaded = puv["passengers"] > puv["capacity"]
        result.append({
            "puv_id":      puv["id"],
            "plate":       puv["plate"],
            "company":     puv.get("company", "OROTSCO"),
            "route":       puv.get("route", "Bugo–Igpit"),
            "driver":      puv.get("driver", "—"),
            "capacity":    puv["capacity"],
            "passengers":  puv["passengers"],
            "load_pct":    load_pct,
            "overloaded":  overloaded,
            "temp":        puv["temp"],
            "hot":         puv["temp"] >= 37.5,
            "loc_name":    puv.get("loc_name", "—"),
            "last_update": puv.get("last_update", "—"),
            "lat":         puv["lat"],
            "lng":         puv["lng"],
            "has_gps":     puv["has_gps"],
        })
    return JSONResponse({"puvs": result})

@app.get("/api/passenger/puv/{plate}")
async def passenger_puv_detail(plate: str, request: Request):
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    plate_up = plate.upper()
    puv = next((p for p in puv_database if p["plate"].upper() == plate_up), None)
    if not puv:
        return JSONResponse({"error": f"Vehicle '{plate_up}' not found."}, status_code=404)
    _normalize_puv(puv)
    load_pct   = round((puv["passengers"] / puv["capacity"]) * 100) if puv["capacity"] else 0
    overloaded = puv["passengers"] > puv["capacity"]
    hot        = puv["temp"] >= 37.5
    return JSONResponse({
        "puv_id":      puv["id"],
        "plate":       puv["plate"],
        "company":     puv.get("company", "OROTSCO"),
        "route":       puv.get("route", "Bugo–Igpit"),
        "driver":      puv.get("driver", "—"),
        "schedule":    puv.get("schedule", "—"),
        "capacity":    puv["capacity"],
        "passengers":  puv["passengers"],
        "load_pct":    load_pct,
        "overloaded":  overloaded,
        "temp":        puv["temp"],
        "hot":         hot,
        "speed":       puv.get("speed", "—"),
        "lat":         puv["lat"],
        "lng":         puv["lng"],
        "has_gps":     puv["has_gps"],
        "loc_name":    puv.get("loc_name", "—"),
        "status":      puv.get("status", "Active"),
        "last_update": puv.get("last_update", "—"),
    })

@app.post("/api/passenger/complaint")
async def passenger_complaint(request: Request):
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    body        = await request.json()
    plate       = clean_text(body.get("plate", ""), 20).upper()
    complaint   = clean_text(body.get("complaint", ""), 60)
    description = clean_text(body.get("description", ""), 1000)
    if not plate or not complaint:
        return JSONResponse({"error": "Plate and complaint type are required."}, status_code=400)
    puv    = next((p for p in puv_database if p["plate"].upper() == plate), None)
    serial = f"CMP-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "serial":      serial,
        "plate":       plate,
        "puv_id":      puv["id"]         if puv else None,
        "company":     puv.get("company", "") if puv else "",
        "driver":      puv.get("driver", "")  if puv else "",
        "loc_name":    puv.get("loc_name", "") if puv else "",
        "complaint":   complaint,
        "description": description,
        "passengers":  puv["passengers"] if puv else None,
        "capacity":    puv["capacity"]   if puv else None,
        "temp":        puv["temp"]       if puv else None,
        "reporter":    pax_user["username"],
        "timestamp":   ph_now_iso(),
        "status":      "open",
    }
    try:
        db_append_complaint(record)          # save first: never tell the passenger it was sent if it wasn't
    except Exception as e:
        print(f"[DB] complaint save failed: {e}")
        return JSONResponse({"error": "Couldn't send the report right now. Please try again."}, status_code=503)
    complaints_db.append(record)
    log_audit(pax_user["username"], f"PASSENGER_COMPLAINT: {plate} · {complaint.upper()}", request.client.host if request.client else "unknown", "blue")
    await notify_data_changed("complaints")
    return JSONResponse({
        "message": "Complaint submitted successfully. Thank you for your report.",
        "serial":  serial,
    })



# --- PASSENGER: own reports, profile, password (used by the mobile app) ---
@app.get("/api/passenger/my-complaints")
async def passenger_my_complaints(request: Request):
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    mine = [{k: v for k, v in c.items() if k != "status_by"}
            for c in complaints_db if c.get("reporter") == pax_user["username"]]
    mine.sort(key=lambda c: c.get("timestamp", ""), reverse=True)   # newest first
    return JSONResponse({"complaints": mine})


# --- PASSENGER REPORTS: LTFRB / Admin review them, the cooperative sees its own vehicles ---
REPORT_TYPES = {
    "overload": "Passenger overloading",
    "thermal":  "Elevated temperature",
    "other":    "Other violation",
}
REPORT_STATUSES = ("open", "reviewed", "resolved")
REPORT_VIEWERS  = ("admin", "enforcer", "cooperative")
REPORT_EDITORS  = ("admin", "enforcer")


def ph_now_iso() -> str:
    """Philippine time like 2026-11-27T09:15:02 (Vercel's clock is UTC)."""
    return datetime.datetime.now(PH_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def visible_reports(request: Request) -> list:
    """Passenger reports this user may see, newest first."""
    role = get_role(request)
    if role not in REPORT_VIEWERS:
        return []
    items = list(complaints_db)
    if role == "cooperative":
        plates = {p.get("plate", "").upper() for p in get_coop_puvs(request.session.get("username", ""))}
        items = [c for c in items if (c.get("plate") or "").upper() in plates]
    items.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return items


def count_new_reports(request: Request) -> int:
    """Number shown next to "Passenger reports" in the sidebar."""
    try:
        return sum(1 for c in visible_reports(request) if c.get("status", "open") == "open")
    except Exception:
        return 0


@app.get("/passenger_reports", response_class=HTMLResponse)
async def passenger_reports_page(request: Request, status: str = "all"):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=302)
    role = get_role(request)
    if role not in REPORT_VIEWERS:
        return RedirectResponse(url="/dashboard", status_code=302)
    items  = visible_reports(request)
    counts = {s: sum(1 for c in items if c.get("status", "open") == s) for s in REPORT_STATUSES}
    counts["all"] = len(items)
    if status not in REPORT_STATUSES:
        status = "all"
    shown = items if status == "all" else [c for c in items if c.get("status", "open") == status]
    company = {p.get("plate", "").upper(): p.get("company", "") for p in puv_database}
    return TR(request, "passenger_reports.html", {
        "role": role, "reports": shown, "counts": counts, "filter": status,
        "types": REPORT_TYPES, "company": company, "can_update": role in REPORT_EDITORS,
    })


async def set_report_status(serial: str, body: dict, username: str, ip: str) -> JSONResponse:
    """Mark a passenger report open / reviewed / resolved (website and LTFRB mobile app)."""
    new_status = clean_text(body.get("status", ""), 20).lower()
    if new_status not in REPORT_STATUSES:
        return JSONResponse({"error": "Status must be open, reviewed or resolved."}, status_code=400)
    serial = clean_text(serial, 40).upper()
    record = next((c for c in complaints_db if c.get("serial") == serial), None)
    if not record:
        return JSONResponse({"error": "That report no longer exists."}, status_code=404)
    changes = {
        "status":    new_status,
        "status_by": username,
        "status_at": ph_now_iso(),
    }
    if "note" in body:
        changes["note"] = clean_text(body.get("note", ""), 300)
    try:
        saved = db_update_complaint(serial, changes)
    except Exception as e:
        print(f"[DB] report status update failed: {e}")
        return JSONResponse({"error": "Couldn't save. Please try again."}, status_code=503)
    record.update(saved or changes)
    log_audit(username, f"PASSENGER_REPORT_{new_status.upper()}: {serial} · {record.get('plate', '')}",
              ip, "green" if new_status == "resolved" else "blue")
    await notify_data_changed("complaints")
    return JSONResponse({"success": True, "serial": serial, "status": new_status,
                         "status_at": record.get("status_at"), "note": record.get("note", "")})


@app.post("/api/complaints/{serial}/status")
async def update_report_status(serial: str, request: Request):
    if not is_logged_in(request):
        return JSONResponse({"error": "Please log in again."}, status_code=401)
    role = get_role(request)
    if role not in REPORT_EDITORS:
        return JSONResponse({"error": "Only LTFRB and the administrator can update reports."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await set_report_status(serial, body if isinstance(body, dict) else {},
                                   request.session.get("username", ""),
                                   request.client.host if request.client else "unknown")


# --- LTFRB field officers: passenger reports in the mobile app ---
@app.get("/api/mobile/passenger-reports")
async def mobile_passenger_reports(request: Request, status: str = "all"):
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    items = sorted(complaints_db, key=lambda c: c.get("timestamp", ""), reverse=True)
    counts = {s: sum(1 for c in items if c.get("status", "open") == s) for s in REPORT_STATUSES}
    counts["all"] = len(items)
    if status in REPORT_STATUSES:
        items = [c for c in items if c.get("status", "open") == status]
    company = {p.get("plate", "").upper(): p.get("company", "") for p in puv_database}
    reports = [{**{k: v for k, v in c.items() if k != "status_by"},
                "status":  c.get("status", "open"),
                "company": company.get((c.get("plate") or "").upper()) or c.get("company", ""),
                "type_label": REPORT_TYPES.get(c.get("complaint"), (c.get("complaint") or "Report").capitalize())}
               for c in items[:200]]
    return JSONResponse({"success": True, "reports": reports, "counts": counts})


@app.post("/api/mobile/passenger-reports/{serial}/status")
async def mobile_passenger_report_status(serial: str, request: Request):
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await set_report_status(serial, body if isinstance(body, dict) else {},
                                   mobile_user["username"],
                                   request.client.host if request.client else "unknown")

@app.put("/api/passenger/profile")
async def passenger_update_profile(request: Request):
    auth     = request.headers.get("Authorization", "")
    token    = auth[7:] if auth.startswith("Bearer ") else ""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    username = pax_user["username"]
    if username not in users:
        return JSONResponse({"error": "Account not found."}, status_code=404)
    data = await request.json()
    for key in ("mobile", "full_name", "email"):
        if key in data and isinstance(data[key], str):
            value = clean_text(data[key], 80 if key == "full_name" else 120)
            if key == "full_name" and not value:
                return JSONResponse({"error": "Full name can't be empty."}, status_code=400)
            users[username][key] = value
            if key in ("mobile", "full_name"):
                pax_user[key] = value
    db_save_user(username, users[username])
    passenger_tokens[token] = pax_user        # keep the saved login in step with the new details
    return JSONResponse({
        "success":   True,
        "username":  username,
        "full_name": users[username].get("full_name", ""),
        "mobile":    users[username].get("mobile", ""),
        "email":     users[username].get("email", ""),
    })

@app.put("/api/passenger/change-password")
async def passenger_change_password(request: Request):
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    username = pax_user["username"]
    user     = users.get(username)
    if not user:
        return JSONResponse({"error": "Account not found."}, status_code=404)
    data = await request.json()
    current_pw = data.get("current_password", "")
    new_pw     = data.get("new_password", "")
    if login_locked("passenger", username):
        return JSONResponse({"error": LOCKED_MSG}, status_code=429)
    if not verify_password(user.get("password"), current_pw):
        note_login_fail("passenger", username)
        return JSONResponse({"error": "Current password is incorrect."}, status_code=400)
    if not isinstance(new_pw, str) or not 6 <= len(new_pw) <= 200:
        return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
    set_password(username, new_pw)
    log_audit(username, "PASSWORD_CHANGED (mobile)", request.client.host if request.client else "unknown", "green")
    return JSONResponse({"success": True})

# --- ID GENERATOR ---
@app.get("/api/next_id")
async def next_id(role: str, request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    # Prefix per role: admin=2026, enforcer=2027, driver=2028, cooperative=2029
    prefixes = {"admin": "2026", "enforcer": "2027", "driver": "2028", "cooperative": "2029"}
    prefix = prefixes.get(role, "2030")
    # Find all existing IDs for this role that match the prefix
    existing = [
        int(uid[4:]) for uid in users
        if uid.startswith(prefix) and uid[4:].isdigit()
    ]
    next_seq = (max(existing) + 1) if existing else 1
    next_id_val = f"{prefix}{next_seq:03d}"
    return JSONResponse({"next_id": next_id_val})


# --- PROFILE & PASSWORD API ---
@app.post("/api/update_profile")
async def update_profile(request: Request):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')
    role     = get_role(request)
    data     = await request.json()

    allowed = {
        'admin':       ['full_name', 'email'],
        'enforcer':    ['full_name', 'email', 'mobile', 'shift_status'],
        'driver':      ['full_name', 'mobile', 'emergency_contact'],
        'cooperative': ['full_name', 'email', 'mobile', 'address'],
        'passenger':   ['full_name', 'mobile', 'email'],
    }
    if username not in users or not isinstance(data, dict):
        return JSONResponse({"success": False, "error": "Account not found."}, status_code=404)
    for key, value in data.items():
        if key in allowed.get(role, []) and value is not None:
            users[username][key] = clean_text(value)
    db_save_user(username, users[username])
    log_audit(username, "PROFILE_UPDATED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

@app.post("/api/change_password")
async def change_password_api(request: Request):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')
    data     = await request.json()
    current  = data.get("current_password", "")
    new_pw   = data.get("new_password", "")
    if login_locked("web", username):
        return JSONResponse({"success": False, "error": LOCKED_MSG})
    if not verify_password(users.get(username, {}).get('password'), current):
        note_login_fail("web", username)
        return JSONResponse({"success": False, "error": "Current password is incorrect."})
    if not isinstance(new_pw, str) or not 6 <= len(new_pw) <= 200:
        return JSONResponse({"success": False, "error": "New password must be at least 6 characters."})
    set_password(username, new_pw)
    request.session['ver'] = users[username]['session_ver']    # stay logged in here
    log_audit(username, "PASSWORD_CHANGED", request.client.host if request.client else "unknown", "green")
    return JSONResponse({"success": True})


# --- AVATAR UPLOAD ---
# Pictures are stored in the database (Vercel's disk is read-only) and served by /avatar/<username>.
@app.post("/api/upload_avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')
    if username not in users:
        return JSONResponse({"success": False, "error": "User not found."}, status_code=404)

    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed_types:
        return JSONResponse({"success": False, "error": "Only JPG, PNG, WEBP or GIF images are allowed."})

    # Validate file size (max 2MB)
    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        return JSONResponse({"success": False, "error": "Image must be under 2MB."})
    magic = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",),
             "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": (b"RIFF",)}
    if not contents.startswith(magic[file.content_type]) or (file.content_type == "image/webp" and contents[8:12] != b"WEBP"):
        return JSONResponse({"success": False, "error": "That file is not a real image."})

    db_save_avatar(username, file.content_type, contents)

    # ?v= changes on every upload so browsers don't keep showing the old picture
    avatar_url = f"/avatar/{username}?v={int(time.time())}"
    users[username]['avatar'] = avatar_url
    db_save_user(username, users[username])
    log_audit(username, "AVATAR_UPDATED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True, "avatar_url": avatar_url})

@app.get("/avatar/{username}")
async def get_avatar(username: str):
    found = db_load_avatar(username)
    if not found:
        return Response(status_code=404)
    content_type, image = found
    return Response(content=image, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=86400"})

@app.post("/api/remove_avatar")
async def remove_avatar(request: Request):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')
    if username not in users:
        return JSONResponse({"success": False, "error": "User not found."}, status_code=404)
    db_delete_avatar(username)
    users[username].pop('avatar', None)
    db_save_user(username, users[username])
    return JSONResponse({"success": True})


@app.post("/api/coop/add_vehicle")
async def coop_add_vehicle(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "error": "Bad request."})
    data     = {k: (clean_text(v, 120) if isinstance(v, str) else v) for k, v in data.items()}
    plate    = clean_text(data.get("plate", ""), 20).upper()
    company  = clean_text(data.get("company", ""), 80)
    capacity = _as_int(data.get("capacity", 22), 0)
    if not 0 < capacity <= 100:
        return JSONResponse({"success": False, "error": "Please enter a valid seated capacity."})

    if not plate:
        return JSONResponse({"success": False, "error": "Plate number is required."})
    if any(p["plate"].upper() == plate for p in puv_database):
        return JSONResponse({"success": False, "error": f"Plate {plate} is already registered."})

    # Assign driver if provided
    driver_username = data.get("driver_username", "")
    driver_name     = "Unassigned"
    if driver_username and driver_username in users:
        driver_name = users[driver_username].get("full_name", driver_username)

    new_id = max((p["id"] for p in puv_database), default=0) + 1
    coop_username = request.session.get('username', '')
    new_puv = {
        "id":                new_id,
        "username":          driver_username or "",
        "coop_owner":        coop_username,
        "company":           company,
        "plate":             plate,
        "driver":            driver_name,
        "passengers":        0,
        "capacity":          capacity,
        "standing_capacity": max(0, min(100, _as_int(data.get("standing_capacity", 0), 0))),
        "loc_name":          "CDO · Not yet tracked",
        "lat":               8.4822,
        "lng":               124.6472,
        "status":            "Active",
        "speed":             "0 km/h",
        "temp":              0.0,
        "last_update":       "Just now",
        "show_name":         True,
        "schedule":          data.get("schedule", ""),
        "route":             data.get("route", ""),
        "vehicle_type":      data.get("vehicle_type", ""),
        "engine_type":       data.get("engine_type", ""),
        "year_model":        data.get("year_model", ""),
        "chassis_no":        data.get("chassis_no", ""),
        "franchise_no":      data.get("franchise_no", ""),
        "color_coding":      data.get("color_coding", ""),
    }
    puv_database.append(new_puv)
    db_save_puv(new_id, new_puv)
    # Assign to driver record
    if driver_username and driver_username in users:
        users[driver_username]["plate"] = plate
        db_save_user(driver_username, users[driver_username])
    log_audit(
        request.session.get('username', 'cooperative'),
        f"VEHICLE_ADDED: {plate}",
        request.client.host if request.client else "unknown",
        "blue"
    )
    await notify_data_changed("fleet", new_id)
    return JSONResponse({"success": True, "plate": plate, "id": new_id})


@app.post("/api/coop/request_capacity")
async def coop_request_capacity(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = _as_int(data.get("capacity"), 0)
    puv = _puv_for_role(request, puv_id)
    if not puv:
        return JSONResponse({"success": False, "error": "Vehicle not found."})
    if not 0 < new_capacity <= 100:
        return JSONResponse({"success": False, "error": "Invalid capacity value."})
    standing = max(0, min(100, _as_int(data.get("standing_capacity", 0), 0)))
    # Store as pending — not applied until LTFRB approves
    puv['pending_capacity'] = {
        "seated":   int(new_capacity),
        "standing": standing,
        "total":    int(new_capacity) + standing
    }
    db_save_puv(puv['id'], puv)
    log_audit(
        request.session.get('username', 'cooperative'),
        f"CAPACITY_REQUEST: {puv['plate']} · {new_capacity} seated + {standing} standing (Pending LTFRB)",
        request.client.host if request.client else "unknown",
        "blue"
    )
    await notify_data_changed("fleet", puv['id'])
    return JSONResponse({"success": True, "plate": puv['plate'], "pending_capacity": puv['pending_capacity']})

# --- SENSOR APIs ---
PH_TZ = datetime.timezone(datetime.timedelta(hours=8))   # Philippine time (Vercel runs in UTC)

# The counter sends a reading at least every 60 s. No reading for this long = counter offline.
DEVICE_ONLINE_S = 150

def _stamp_now() -> str:
    return datetime.datetime.now(PH_TZ).strftime("%I:%M:%S %p").lstrip("0")

@app.get("/api/driver/status")
async def driver_status(request: Request):
    """Current reading for the logged-in driver's vehicle (used as a fallback when the WebSocket drops)."""
    if get_role(request) != 'driver':
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    puv = find_driver_puv(request.session.get('username'))
    if not puv:
        return JSONResponse({"assigned": False})
    return JSONResponse({"assigned": True, **puv_live_payload(puv)})

@app.post("/api/toggle_name")
async def toggle_name(request: Request):
    if get_role(request) != 'driver': return JSONResponse({"error": "Unauthorized"})
    puv = find_driver_puv(request.session.get('username'))
    if puv:
        puv['show_name'] = not puv.get('show_name', True)
        db_save_puv(puv['id'], puv)
        return JSONResponse({"success": True, "show_name": puv['show_name']})
    return JSONResponse({"error": "Vehicle not found"})

# --- SENSOR READINGS ---
# The Pi (or dummy_sensor.py) sends readings like:
#   {"puv_id": 1, "passengers": 12, "temp": 33.5, "lat": 8.48, "lng": 124.64, "speed": "18 km/h"}
# ("plate" may be sent instead of "puv_id").
#   • On Vercel:  POST it to /api/sensor with header  X-Device-Key: <SENSOR_API_KEY>
#   • Elsewhere:  POST to /api/sensor works too, or send it over the /ws WebSocket
#                 with "type": "sensor_update".
# Every reading is saved to the database, so pages on any server see it.
def _find_puv_for_payload(payload: dict):
    puv_id = payload.get("puv_id")
    if puv_id is not None:
        pid = _as_int(puv_id, None)
        puv = next((p for p in puv_database if p.get('id') == pid), None)
        if puv:
            return puv
    plate = str(payload.get("plate", "")).strip().upper()
    if plate:
        return next((p for p in puv_database if str(p.get('plate', '')).upper() == plate), None)
    return None

def apply_sensor_update(payload: dict):
    """Apply one reading to its vehicle and save it. Returns the vehicle, or None if unknown."""
    puv = _find_puv_for_payload(payload)
    if not puv:
        return None
    _normalize_puv(puv)
    # Coerce values so a sensor sending "12" or null can't break page rendering later
    if "passengers" in payload:
        puv['passengers'] = max(0, _as_int(payload["passengers"], puv.get('passengers', 0)))
    if "temp" in payload:
        puv['temp'] = _as_float(payload["temp"], puv.get('temp', 0))
    # Only move the vehicle on a real fix. 0,0 or missing values (GPS still searching,
    # e.g. indoors) keep the last good position instead of jumping into the ocean.
    coords = _valid_coords(payload.get("lat"), payload.get("lng"))
    if coords:
        puv['lat'], puv['lng'] = coords
        puv['gps_at'] = _stamp_now()
        puv['gps_ts'] = int(time.time())
    if payload.get("loc_name"):
        puv['loc_name'] = clean_text(payload["loc_name"], 80)
    if payload.get("speed") is not None:
        puv['speed'] = clean_text(payload["speed"], 20)
    if payload.get("humidity") is not None:
        puv['humidity'] = _as_float(payload["humidity"], puv.get('humidity'))
    puv['device_ts']      = int(time.time())     # the counter is online: shown on the driver dashboard
    puv['last_update']    = _stamp_now()
    puv['is_violator']    = puv['passengers'] > puv['capacity']
    puv['is_overheating'] = puv.get('temp', 0) >= 37.5
    db_save_puv(puv['id'], puv)
    return puv

@app.post("/api/sensor")
async def sensor_post(request: Request):
    """HTTP endpoint for the Pi. Works on Vercel (which has no WebSockets)."""
    if SENSOR_API_KEY and not secrets.compare_digest(request.headers.get("X-Device-Key", ""), SENSOR_API_KEY):
        return JSONResponse({"success": False, "error": "Invalid device key"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Body must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"success": False, "error": "Body must be a JSON object"}, status_code=400)
    puv = apply_sensor_update(payload)
    if not puv:
        return JSONResponse({"success": False, "error": "Unknown vehicle (check puv_id or plate)"}, status_code=404)
    live = puv_live_payload(puv)
    await manager.broadcast(live)            # reaches WebSocket pages when running outside Vercel
    return JSONResponse({"success": True, **live})

def _live_version(n_citations: int, n_complaints) -> str:
    """Changes whenever anything on the fleet changes (readings, vehicles added/removed,
    capacity, driver, citations). Pages refresh their server-rendered parts when it changes."""
    parts = [str(n_citations), str(n_complaints)]
    for p in sorted(puv_database, key=lambda x: x.get('id', 0)):
        parts.append(json.dumps(p, sort_keys=True, default=str))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

@app.get("/api/live")
async def live_feed(request: Request):
    """Polled every few seconds by open pages when LIVE_MODE is "poll"."""
    if not is_logged_in(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # One database session for the whole answer: this runs every few seconds per open page
    try:
        fresh, n_citations, n_complaints = db_live_snapshot()
        puv_database[:] = fresh
    except Exception as e:
        print(f"[DB] live snapshot failed, sending current data: {e}")
        n_citations = len(citations_db)
        n_complaints = "{}.{}.{}".format(len(complaints_db),
                                         sum(1 for c in complaints_db if c.get("status") == "reviewed"),
                                         sum(1 for c in complaints_db if c.get("status") == "resolved"))
    return JSONResponse(
        {"version": _live_version(n_citations, n_complaints),
         "puvs": [puv_live_payload(p) for p in puv_database]},
        headers={"Cache-Control": "no-store"},
    )

@app.get("/api/health")
async def health():
    """Quick check that the server and database are working.
    "database": "postgres" is what you want on Vercel. "temporary" means no database is
    connected, so accounts and data are lost on every deploy."""
    ok = True
    try:
        db_load_users(strict=True)
    except Exception:
        ok = False
    return JSONResponse({
        "ok":        ok,
        "database":  DB_KIND,
        "persistent": DB_KIND != "temporary",
        "db_setting": DB_URL_SOURCE or None,      # name of the setting used, never its value
        "deploy":    {"env": VERCEL_ENV or None, "branch": DEPLOY_BRANCH or None, "commit": DEPLOY_COMMIT or None},
        "vehicles":  len(puv_database),
    }, headers={"Cache-Control": "no-store"})

# --- WEBSOCKET (not available on Vercel; used when running locally or on a regular server) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue                      # ignore malformed frames instead of dropping the connection
            if not isinstance(payload, dict) or payload.get("type") != "sensor_update":
                continue
            if SENSOR_API_KEY and not secrets.compare_digest(str(payload.get("key", websocket.query_params.get("key", ""))), SENSOR_API_KEY):
                continue                      # readings must carry the device key
            _ensure_ready()
            reload_puvs()
            puv = apply_sensor_update(payload)
            if puv:
                await manager.broadcast(puv_live_payload(puv))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] connection closed after error: {e}")
    finally:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)