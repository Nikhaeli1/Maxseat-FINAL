from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from typing import List
import json, datetime, secrets, uuid, os
from dotenv import load_dotenv

load_dotenv()  # loads .env locally; Railway uses its own env vars directly

# ── Input validation helpers ──────────────────────────────────────────────────
import re

PLATE_RE    = re.compile(r'^[A-Z0-9\-]{3,10}$')
USERNAME_RE = re.compile(r'^[A-Za-z0-9_\-\.]{3,32}$')

def _str(v, max_len=200) -> str:
    """Coerce to stripped string and enforce max length."""
    return str(v or '').strip()[:max_len]

def validate_plate(plate: str):
    """Return cleaned plate or raise ValueError."""
    p = _str(plate, 10).upper()
    if not p or not PLATE_RE.match(p):
        raise ValueError(f"Invalid plate number: '{plate}'")
    return p

def validate_complaint_type(t: str):
    allowed = {'overload', 'thermal', 'other'}
    t = _str(t, 20).lower()
    if t not in allowed:
        raise ValueError(f"Complaint type must be one of: {', '.join(allowed)}")
    return t

def validate_action(a: str):
    allowed = {'ticket', 'warning', 'apprehend'}
    a = _str(a, 20).lower()
    if a not in allowed:
        raise ValueError(f"Action must be one of: {', '.join(allowed)}")
    return a

# In-memory mobile token stores
mobile_tokens:    dict = {}   # enforcer Bearer tokens
passenger_tokens: dict = {}   # passenger Bearer tokens

# In-memory complaint store
complaints_db: list = []

app = FastAPI(title="MaxSeat Alert System")

# --- MIDDLEWARE ---
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "maxseat_dev_fallback_key_change_in_production"),
    max_age=604800,
)

# --- STATIC & TEMPLATES ---
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(data))
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.active_connections.remove(conn)

manager = ConnectionManager()

# --- GLOBAL IN-MEMORY DATABASE ---
users = {
    "admin": {
        "password": "123", "role": "admin", "email": "admin@maxseat.ph",
        "must_change_password": False, "full_name": "Bradley Sheen Sale",
        "department": "Central Command Authority", "clearance": "Level 5 - Global"
    },
    "LTFRB": {
        "password": "123", "role": "enforcer", "email": "cdo.precinct@pnp.gov.ph",
        "must_change_password": False, "full_name": "Marielle Joy F. Asok",
        "badge": "PNP-CDO-994", "precinct": "CDO Central Station",
        "mobile": "0917-123-4567", "shift_status": "On Duty"
    },
    "JuanD": {
        "password": "DelaCruz@001", "role": "driver", "email": "delacruz@maxseat.ph",
        "must_change_password": True, "full_name": "Juan Dela Cruz",
        "license": "L02-12-345678", "operator": "Señor Pedro Lines",
        "plate": "KVR-102", "mobile": "0920-987-6543",
        "emergency_contact": "Maria Dela Cruz (0999-000-0000)"
    },
    "OROTSCO-01": {
        "password": "123", "role": "cooperative", "email": "dispatch@orotsco.coop",
        "must_change_password": False, "full_name": "Elsie Jandayan",
        "organization": "Oro Transport Service Cooperative (OROTSCO)",
        "position": "General Manager", "mobile": "0912-345-6789",
        "address": "Bugo-Igpit Route, Cagayan de Oro City"
    },
    "passenger01": {
        "password": "pass123", "role": "passenger",
        "full_name": "Juan dela Cruz", "mobile": "09171234567",
    },
}

puv_database = [
    {
        "id": 1, "username": "JuanD", "company": "Señor Pedro Lines",
        "plate": "KVR-102", "driver": "Juan Dela Cruz", "passengers": 14,
        "capacity": 22, "loc_name": "Bulua Highway", "lat": 8.4965, "lng": 124.6235,
        "status": "Active", "speed": "45 km/h", "temp": 32.5,
        "last_update": "Just now", "show_name": True, "schedule": "06:00 AM - 08:00 PM",
        "route": "Bugo–Igpit"
    },
    {
        "id": 2, "username": "dummy2", "company": "Oro Transit",
        "plate": "AAB-5501", "driver": "Ricardo Dalisay", "passengers": 25,
        "capacity": 22, "loc_name": "CM Recto Ave", "lat": 8.4865, "lng": 124.6508,
        "status": "Active", "speed": "30 km/h", "temp": 38.0,
        "last_update": "1 min ago", "show_name": False, "schedule": "05:00 AM - 09:00 PM",
        "route": "Bugo–Igpit"
    },
    {
        "id": 3, "username": "dummy3", "company": "Bukidnon Express",
        "plate": "XYZ-998", "driver": "Pedro Santos", "passengers": 18,
        "capacity": 22, "loc_name": "Lapasan, CDO", "lat": 8.5012, "lng": 124.6310,
        "status": "Active", "speed": "38 km/h", "temp": 30.1,
        "last_update": "2 mins ago", "show_name": True, "schedule": "05:30 AM - 08:30 PM",
        "route": "Bugo–Igpit"
    }
]

audit_logs = [
    {"timestamp": "2026-05-26 08:10:00", "actor": "admin",      "event": "SESSION_INITIATED", "ip": "192.168.1.1",  "category": "green"},
    {"timestamp": "2026-05-26 07:55:11", "actor": "SYSTEM_CRON","event": "DATABASE_SYNC",     "ip": "127.0.0.1",    "category": "blue"},
    {"timestamp": "2026-05-25 15:22:30", "actor": "LTFRB",      "event": "DISPATCH_UNIT",     "ip": "110.54.22.1",  "category": "red"},
    {"timestamp": "2026-05-25 09:10:05", "actor": "JuanD",      "event": "SESSION_INITIATED", "ip": "192.168.1.45", "category": "green"},
    {"timestamp": "2026-05-24 20:01:00", "actor": "admin",      "event": "USER_CREATED",      "ip": "192.168.1.1",  "category": "blue"},
    {"timestamp": "2026-05-24 14:33:17", "actor": "LTFRB",      "event": "VIOLATION_LOGGED",  "ip": "110.54.22.1",  "category": "red"},
]

citations_db = [
    {"serial": "CT-9921-A", "plate": "KVR-102",  "company": "Señor Pedro Lines", "officer": "LTFRB", "fine": "₱5,000.00", "status": "PENDING",  "date": "2026-05-24"},
    {"serial": "CT-8810-B", "plate": "AAB-5501", "company": "Oro Transit",       "officer": "LTFRB", "fine": "₱2,500.00", "status": "RESOLVED", "date": "2026-05-22"},
    {"serial": "CT-7744-C", "plate": "XYZ-998",  "company": "Bukidnon Express",  "officer": "LTFRB", "fine": "₱5,000.00", "status": "RESOLVED", "date": "2026-05-20"},
]

# --- HELPERS ---
def is_logged_in(request: Request):
    return request.session.get("logged_in", False)

def get_role(request: Request):
    return request.session.get("role", None)

def requires_password_change(request: Request):
    return request.session.get("pending_password_change", False)

def flash(request: Request, message: str):
    request.session["_flash"] = message

def get_flash(request: Request):
    msg = request.session.pop("_flash", None)
    return [msg] if msg else []

def compute_puv_stats():
    for p in puv_database:
        p['is_violator']    = p['passengers'] > p['capacity']
        p['is_overheating'] = p.get('temp', 0) >= 37.5
        p['load_percentage'] = int((p['passengers'] / p['capacity']) * 100) if p['capacity'] > 0 else 0
    return {
        "total_units":       len(puv_database),
        "active_violations": len([p for p in puv_database if p['is_violator']]),
        "thermal_warnings":  len([p for p in puv_database if p.get('is_overheating')]),
        "moving_units":      len([p for p in puv_database if p['status'] == 'Active']),
    }

def log_audit(actor: str, event: str, ip: str, category: str = "blue"):
    audit_logs.insert(0, {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "actor": actor, "event": event, "ip": ip, "category": category
    })

def TR(request: Request, name: str, context: dict = {}):
    """Wrapper: new Starlette TemplateResponse API — request is separate from context."""
    return templates.TemplateResponse(request=request, name=name, context=context)

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
    return TR(request, "login.html", {"messages": get_flash(request)})

@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    role: str = Form(...),
    username: str = Form(...),
    password: str = Form(...)
):
    # Server-side validation
    if not username or not password or len(username) > 64 or len(password) > 128:
        flash(request, "Invalid credentials for the selected role.")
        return RedirectResponse(url="/login", status_code=302)

    user = users.get(username)
    if user and user['password'] == password and user['role'] == role:
        request.session['logged_in'] = True
        request.session['username']  = username
        request.session['role']      = role
        log_audit(username, "SESSION_INITIATED", request.client.host if request.client else "unknown", "green")
        if user.get('must_change_password', False):
            request.session['pending_password_change'] = True
            return RedirectResponse(url="/change_password", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)
    flash(request, "Invalid credentials for the selected role.")
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
    users[username]['password']             = new_password
    users[username]['must_change_password'] = False
    request.session.pop('pending_password_change', None)
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
            users[username][key] = value
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
        return TR(request, "admin/dashboard.html", {"puvs": puv_database, "role": role, "stats": stats})
    elif role == 'enforcer':
        sorted_puvs = sorted(puv_database, key=lambda x: (x['passengers'] / x['capacity']) if x['capacity'] > 0 else 0, reverse=True)
        return TR(request, "enforcer/dashboard.html", {"puvs": sorted_puvs, "role": role, "stats": stats})
    elif role == 'driver':
        username = request.session.get('username')
        my_puv   = next((p for p in puv_database if p['username'] == username), None)
        if my_puv is None:
            # Driver has no assigned PUV — return a safe placeholder so the template never errors
            my_puv = {
                "id": None, "plate": "UNASSIGNED",
                "driver": users.get(username, {}).get("full_name", username),
                "passengers": 0, "capacity": 22, "temp": 0.0, "speed": "—",
                "loc_name": "Not assigned", "lat": 8.4822, "lng": 124.6472,
                "status": "Inactive", "show_name": False, "schedule": "—",
                "company": "—", "last_update": "—", "route": "—",
                "is_violator": False, "is_overheating": False, "load_percentage": 0,
            }
        return TR(request, "driver/dashboard.html", {"puv": my_puv, "role": role})
    elif role == 'cooperative':
        return TR(request, "cooperative/dashboard.html", {"puvs": puv_database, "role": role, "stats": stats})
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
    username = _str(data.get("username", ""), 32)
    password = _str(data.get("password", ""), 128)
    role     = _str(data.get("role", "driver"), 20)
    email    = _str(data.get("email", ""), 100)
    full_name= _str(data.get("full_name", ""), 100)

    if not username or not USERNAME_RE.match(username):
        return JSONResponse({"success": False, "error": "Invalid username. Use 3–32 alphanumeric characters."})
    if username in users:
        return JSONResponse({"success": False, "error": "Username already exists."})
    if len(password) < 6:
        return JSONResponse({"success": False, "error": "Password must be at least 6 characters."})
    if role not in {"admin", "enforcer", "driver", "cooperative", "passenger"}:
        return JSONResponse({"success": False, "error": "Invalid role."})

    users[username] = {
        "password": password,
        "role":     role,
        "email":    email,
        "full_name": full_name,
        "must_change_password": True
    }
    log_audit(request.session.get('username', 'admin'), "USER_CREATED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

@app.post("/api/delete_puv")
async def delete_puv(request: Request):
    if get_role(request) not in ['admin', 'cooperative']:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data   = await request.json()
    puv_id = data.get("puv_id")
    puv    = next((p for p in puv_database if p['id'] == puv_id), None)
    if not puv:
        return JSONResponse({"success": False, "error": "PUV not found."})
    puv_database.remove(puv)
    log_audit(request.session.get('username', 'admin'), f"PUV_REMOVED: {puv['plate']}", request.client.host if request.client else "unknown", "red")
    return JSONResponse({"success": True, "plate": puv['plate']})

@app.post("/api/delete_user")
async def delete_user(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    username = data.get("username", "")
    if username == request.session.get('username'):
        return JSONResponse({"success": False, "error": "Cannot delete your own account."})
    if username in users:
        del users[username]
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "User not found."})

@app.post("/api/reset_user_password")
async def reset_user_password(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse(
            {"success": False, "error": "Unauthorized"},
            status_code=403
        )

    data = await request.json()
    username = data.get("username", "")

    if username in users:
        users[username]["password"] = "123456"
        users[username]["must_change_password"] = True

        log_audit(
            request.session.get('username', 'admin'),
            f"PASSWORD_RESET: {username}",
            request.client.host if request.client else "unknown",
            "blue"
        )

        return JSONResponse({
            "success": True,
            "new_password": "123456"
        })

    return JSONResponse({
        "success": False,
        "error": "User not found."
    })

@app.get("/configure_seating", response_class=HTMLResponse)
async def configure_seating(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "admin/configure_seating.html", {"role": "admin", "puvs": puv_database, "messages": get_flash(request)})

@app.post("/api/update_capacity")
async def update_capacity(request: Request):
    if get_role(request) != 'admin':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = data.get("capacity")
    puv = next((p for p in puv_database if p['id'] == puv_id), None)
    if puv and new_capacity and int(new_capacity) > 0:
        puv['capacity'] = int(new_capacity)
        return JSONResponse({"success": True, "plate": puv['plate'], "capacity": puv['capacity']})
    return JSONResponse({"success": False, "error": "Vehicle not found or invalid capacity."})

@app.get("/audit_logs", response_class=HTMLResponse)
async def audit_logs_page(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "admin/audit_logs.html", {"role": "admin", "logs": audit_logs})

@app.get("/records", response_class=HTMLResponse)
async def records(request: Request):
    if get_role(request) not in ['admin', 'enforcer', 'cooperative']: return RedirectResponse(url="/dashboard", status_code=302)
    stats = compute_puv_stats()
    return TR(request, "records.html", {"role": get_role(request), "puvs": puv_database, "stats": stats})

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
    citations_db.append({
        "serial":  f"CT-{len(citations_db)+1:04d}-X",
        "plate":   plate,
        "company": next((p['company'] for p in puv_database if p['plate'] == plate), "Unknown"),
        "officer": request.session.get('username', 'Officer'),
        "fine":    "₱5,000.00" if action == "ticket" else "₱0.00",
        "status":  "PENDING" if action == "ticket" else "RESOLVED",
        "date":    datetime.date.today().isoformat()
    })
    log_audit(request.session.get('username', 'enforcer'), "INTERCEPTION_LOGGED", request.client.host if request.client else "unknown", "red")
    flash(request, f"Interception report for {plate} submitted successfully.")
    return RedirectResponse(url="/interception_status", status_code=302)

# ==========================================
# COOPERATIVE ROUTES
# ==========================================
@app.get("/coop/fleet", response_class=HTMLResponse)
async def coop_fleet(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    return TR(request, "cooperative/fleet.html", {"role": "cooperative", "puvs": puv_database})

@app.get("/coop/drivers", response_class=HTMLResponse)
async def coop_drivers(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    drivers = {k: v for k, v in users.items() if v.get('role') == 'driver'}
    return TR(request, "cooperative/drivers.html", {"role": "cooperative", "drivers": drivers, "puvs": puv_database, "messages": get_flash(request)})

@app.get("/coop/violations", response_class=HTMLResponse)
async def coop_violations(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    stats = compute_puv_stats()
    return TR(request, "cooperative/violations.html", {"role": "cooperative", "puvs": puv_database, "citations": citations_db, "stats": stats})

@app.get("/coop/reports", response_class=HTMLResponse)
async def coop_reports(request: Request):
    if get_role(request) != 'cooperative': return RedirectResponse(url="/dashboard", status_code=302)
    stats = compute_puv_stats()
    return TR(request, "cooperative/reports.html", {"role": "cooperative", "citations": citations_db, "puvs": puv_database, "stats": stats})

@app.post("/api/coop/update_capacity")
async def coop_update_capacity(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = data.get("capacity")
    puv = next((p for p in puv_database if p['id'] == puv_id), None)
    if puv and new_capacity and int(new_capacity) > 0:
        puv['capacity'] = int(new_capacity)
        log_audit(request.session.get('username', 'cooperative'), "CAPACITY_UPDATED", request.client.host if request.client else "unknown", "blue")
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
    for field in allowed_fields:
        if field in data and data[field] is not None:
            users[username][field] = data[field]
    # Handle PUV assignment
    assigned_puv_id = data.get("assigned_puv_id")
    if assigned_puv_id is not None:
        # Unassign from any current PUV
        for p in puv_database:
            if p["username"] == username:
                p["username"] = ""
                p["driver"]   = "Unassigned"
        # Assign to new PUV if not empty
        if assigned_puv_id != "":
            puv = next((p for p in puv_database if p["id"] == int(assigned_puv_id)), None)
            if puv:
                puv["username"] = username
                puv["driver"]   = users[username].get("full_name", username)
    log_audit(request.session.get('username', 'cooperative'), f"DRIVER_UPDATED: {username}", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

# ==========================================
# MOBILE API  (React Native — Field Enforcer)
# ==========================================
def get_mobile_user(request: Request):
    """Extract and validate Bearer token for enforcer role."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    username = mobile_tokens.get(token)
    if not username:
        return None
    user = users.get(username)
    if not user or user.get("role") != "enforcer":
        return None
    return {"username": username, **user}

@app.post("/api/mobile/login")
async def mobile_login(request: Request):
    """Authenticate a Field Enforcer and return a bearer token."""
    data     = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user     = users.get(username)
    if not user or user["password"] != password or user["role"] != "enforcer":
        return JSONResponse({"success": False, "error": "Invalid credentials."}, status_code=401)
    # Revoke any existing token for this user
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
    """Revoke the mobile bearer token."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    auth  = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    mobile_tokens.pop(token, None)
    return JSONResponse({"success": True})

@app.get("/api/mobile/dispatch")
async def mobile_dispatch(request: Request):
    """Return all active violations as dispatch orders for the mobile app."""
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
                "label":   "Passenger Overload",
                "detail":  f"{p['passengers']}/{p['capacity']} passengers — exceeds legal limit",
                "severity": "critical",
            })
        if p.get("temp", 0) >= 37.5:
            violations.append({
                "type":    "THERMAL",
                "label":   "Thermal Alert",
                "detail":  f"Cabin temperature at {p['temp']}°C — exceeds 37.5°C threshold",
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
    """Submit an interception report from the mobile app."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    data  = await request.json()
    notes = _str(data.get("notes", ""), 500)
    try:
        plate  = validate_plate(data.get("plate", ""))
        action = validate_action(data.get("action", ""))
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    serial = f"CT-{len(citations_db)+1:04d}-M"
    fine_map = {"ticket": "₱5,000.00", "apprehend": "₱10,000.00", "warning": "₱0.00"}
    citations_db.append({
        "serial":  serial,
        "plate":   plate,
        "company": next((p["company"] for p in puv_database if p["plate"] == plate), "Unknown"),
        "officer": mobile_user["username"],
        "fine":    fine_map.get(action, "₱0.00"),
        "status":  "PENDING" if action in ("ticket", "apprehend") else "RESOLVED",
        "date":    datetime.date.today().isoformat(),
        "notes":   notes,
        "source":  "mobile",
    })
    log_audit(mobile_user["username"], f"MOBILE_INTERCEPT: {plate} — {action.upper()}", request.client.host if request.client else "unknown", "red")
    return JSONResponse({
        "success": True,
        "serial":  serial,
        "plate":   plate,
        "action":  action,
        "message": f"Interception report {serial} submitted successfully.",
    })

# ── Enforcer Profile & Citations ──────────────────────────────────────────────

@app.get("/api/mobile/profile")
async def mobile_get_profile(request: Request):
    """Return the logged-in enforcer's full profile."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    username = mobile_user["username"]
    user = users.get(username, {})
    return JSONResponse({
        "username":     username,
        "full_name":    user.get("full_name", username),
        "badge":        user.get("badge", ""),
        "precinct":     user.get("precinct", ""),
        "mobile":       user.get("mobile", ""),
        "email":        user.get("email", ""),
        "shift_status": user.get("shift_status", "On Duty"),
    })

@app.put("/api/mobile/profile")
async def mobile_update_profile(request: Request):
    """Update mobile number and shift status for the enforcer."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    data     = await request.json()
    username = mobile_user["username"]
    for field in ["mobile", "shift_status"]:
        if field in data:
            users[username][field] = data[field]
    log_audit(username, "MOBILE_PROFILE_UPDATED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

@app.put("/api/mobile/change-password")
async def mobile_change_password(request: Request):
    """Change the enforcer's password."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    data     = await request.json()
    current  = data.get("current_password", "")
    new_pw   = data.get("new_password", "")
    username = mobile_user["username"]
    if users[username]["password"] != current:
        return JSONResponse({"error": "Current password is incorrect."}, status_code=400)
    if len(new_pw) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
    users[username]["password"] = new_pw
    log_audit(username, "PASSWORD_CHANGED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

@app.get("/api/mobile/citations")
async def mobile_citations(request: Request):
    """Return all citations submitted by the logged-in enforcer."""
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    username     = mobile_user["username"]
    my_citations = [c for c in citations_db if c.get("officer") == username]
    return JSONResponse({"citations": list(reversed(my_citations))})

# ==========================================
# MOBILE API  (React Native — Passenger)
# ==========================================
def get_passenger_user(request: Request):
    """Extract and validate Bearer token for passenger role."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    return passenger_tokens.get(token)

@app.post("/api/passenger/login")
async def passenger_login(request: Request):
    """Authenticate a passenger and return a bearer token."""
    data     = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user     = users.get(username)
    if not user or user["password"] != password or user["role"] != "passenger":
        return JSONResponse({"error": "Invalid credentials."}, status_code=401)
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
    """Revoke the passenger bearer token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        passenger_tokens.pop(auth[7:], None)
    return JSONResponse({"ok": True})

@app.get("/api/passenger/puvs")
async def passenger_list_puvs(request: Request):
    """Return a lightweight list of all active PUVs."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
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
        })
    return JSONResponse({"puvs": result})

@app.get("/api/passenger/puv/{plate}")
async def passenger_puv_detail(plate: str, request: Request):
    """Return full detail for a single PUV by plate number."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    plate_up = plate.upper()
    puv = next((p for p in puv_database if p["plate"].upper() == plate_up), None)
    if not puv:
        return JSONResponse({"error": f"PUV '{plate_up}' not found."}, status_code=404)
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
        "lat":         puv.get("lat", 8.4822),
        "lng":         puv.get("lng", 124.6472),
        "loc_name":    puv.get("loc_name", "—"),
        "status":      puv.get("status", "Active"),
        "last_update": puv.get("last_update", "—"),
    })

@app.post("/api/passenger/complaint")
async def passenger_complaint(request: Request):
    """Submit a passenger complaint about a PUV."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    body        = await request.json()
    description = _str(body.get("description", ""), 500)
    try:
        plate     = validate_plate(body.get("plate", ""))
        complaint = validate_complaint_type(body.get("complaint", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    puv    = next((p for p in puv_database if p["plate"].upper() == plate), None)
    serial = f"CMP-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "serial":      serial,
        "plate":       plate,
        "puv_id":      puv["id"]         if puv else None,
        "complaint":   complaint,
        "description": description,
        "passengers":  puv["passengers"] if puv else None,
        "capacity":    puv["capacity"]   if puv else None,
        "temp":        puv["temp"]       if puv else None,
        "reporter":    pax_user["username"],
        "timestamp":   datetime.datetime.now().isoformat(timespec="seconds"),
        "status":      "open",
    }
    complaints_db.append(record)
    log_audit(pax_user["username"], f"PASSENGER_COMPLAINT: {plate} — {complaint.upper()}", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({
        "message": "Complaint submitted successfully. Thank you for your report.",
        "serial":  serial,
    })

# ── Passenger Profile & Complaint History ─────────────────────────────────────

@app.get("/api/passenger/my-complaints")
async def passenger_my_complaints(request: Request):
    """Return all complaints submitted by the logged-in passenger."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    username      = pax_user["username"]
    my_complaints = [c for c in complaints_db if c.get("reporter") == username]
    return JSONResponse({"complaints": list(reversed(my_complaints))})

@app.put("/api/passenger/profile")
async def passenger_update_profile(request: Request):
    """Update the passenger's mobile number."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    data     = await request.json()
    username = pax_user["username"]
    if "mobile" in data:
        users[username]["mobile"] = data["mobile"]
        # Keep cached token profile in sync
        for token, profile in passenger_tokens.items():
            if profile.get("username") == username:
                passenger_tokens[token]["mobile"] = data["mobile"]
    return JSONResponse({"success": True})

@app.put("/api/passenger/change-password")
async def passenger_change_password(request: Request):
    """Change the passenger's password."""
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    data     = await request.json()
    current  = data.get("current_password", "")
    new_pw   = data.get("new_password", "")
    username = pax_user["username"]
    if users[username]["password"] != current:
        return JSONResponse({"error": "Current password is incorrect."}, status_code=400)
    if len(new_pw) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
    users[username]["password"] = new_pw
    return JSONResponse({"success": True})

# --- SENSOR APIs ---
@app.post("/api/update_sensor")
async def update_sensor(request: Request):
    if get_role(request) != 'driver': return JSONResponse({"error": "Unauthorized"})
    data   = await request.json()
    action = data.get('action')
    puv    = next((p for p in puv_database if p['username'] == request.session.get('username')), None)
    if puv:
        if action == 'add':
            puv['passengers'] += 1
        elif action == 'sub' and puv['passengers'] > 0:
            puv['passengers'] -= 1
        is_violator    = puv['passengers'] > puv['capacity']
        is_overheating = puv.get('temp', 0) >= 37.5
        await manager.broadcast({
            "type": "violation" if is_violator or is_overheating else "update",
            "puv_id": puv['id'], "plate": puv['plate'],
            "passengers": puv['passengers'], "capacity": puv['capacity'],
            "temp": puv.get('temp', 0), "is_violator": is_violator,
            "is_overheating": is_overheating, "lat": puv['lat'], "lng": puv['lng'],
            "loc_name": puv['loc_name'],
        })
        return JSONResponse({"success": True, "passengers": puv['passengers'], "capacity": puv['capacity'], "temp": puv.get('temp', 0)})
    return JSONResponse({"error": "PUV not found"})

@app.post("/api/toggle_name")
async def toggle_name(request: Request):
    if get_role(request) != 'driver': return JSONResponse({"error": "Unauthorized"})
    puv = next((p for p in puv_database if p['username'] == request.session.get('username')), None)
    if puv:
        puv['show_name'] = not puv['show_name']
        return JSONResponse({"success": True, "show_name": puv['show_name']})
    return JSONResponse({"error": "PUV not found"})

# --- WEBSOCKET ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data    = await websocket.receive_text()
            payload = json.loads(data)
            if payload.get("type") == "sensor_update":
                puv_id = payload.get("puv_id")
                puv    = next((p for p in puv_database if p['id'] == puv_id), None)
                if puv:
                    puv['passengers']  = payload.get("passengers", puv['passengers'])
                    puv['temp']        = payload.get("temp", puv.get('temp', 0))
                    puv['lat']         = payload.get("lat", puv['lat'])
                    puv['lng']         = payload.get("lng", puv['lng'])
                    puv['last_update'] = "Just now"
                    is_violator    = puv['passengers'] > puv['capacity']
                    is_overheating = puv['temp'] >= 37.5
                    puv['is_violator'] = is_violator
                    await manager.broadcast({
                        "type": "violation" if is_violator or is_overheating else "update",
                        "puv_id": puv['id'], "plate": puv['plate'], "company": puv['company'],
                        "passengers": puv['passengers'], "capacity": puv['capacity'],
                        "temp": puv['temp'], "is_violator": is_violator,
                        "is_overheating": is_overheating,
                        "lat": puv['lat'], "lng": puv['lng'], "loc_name": puv.get('loc_name', ''),
                    })
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
