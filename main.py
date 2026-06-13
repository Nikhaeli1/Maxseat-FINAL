from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
import smtplib, httpx, os, shutil, base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from typing import List
import json, datetime, secrets, uuid

from database import (
    init_db,
    db_save_user, db_delete_user, db_load_users,
    db_save_puv,  db_delete_puv,  db_load_puvs,
    db_append_audit,    db_load_audit_logs,
    db_append_citation, db_load_citations,
    db_append_complaint, db_load_complaints,
)

# In-memory mobile token stores
mobile_tokens:    dict = {}   # enforcer Bearer tokens
passenger_tokens: dict = {}   # passenger Bearer tokens

# In-memory complaint store
complaints_db: list = []

# ══════════════════════════════════════════════
# NOTIFICATION CONFIG — fill these in
# ══════════════════════════════════════════════
SEMAPHORE_API_KEY  = "YOUR_SEMAPHORE_API_KEY"   # get from semaphore.co
SEMAPHORE_SENDER   = "MaxSeat"                   # your registered sender name (max 11 chars)

GMAIL_ADDRESS      = "asok.marielle04@gmail.com"            # your Gmail address
GMAIL_APP_PASSWORD = "vwut ckwz nuno dnto"       # Gmail App Password (not your login password)
GMAIL_SENDER_NAME  = "MaxSeat Alert System"

# ══════════════════════════════════════════════

app = FastAPI(title="MaxSeat Alert System")

# --- MIDDLEWARE ---
app.add_middleware(SessionMiddleware, secret_key="maxseat_enterprise_premium_secure_key", max_age=604800)

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
# Populated from the database at startup (see @app.on_event("startup") below).
# The default admin account is seeded only when the DB has no users yet.
_DEFAULT_ADMIN = {
    "password": "admin@2026001", "role": "admin", "email": "admin@maxseat.ph",
    "must_change_password": False, "full_name": "System Administrator",
    "department": "Central Command Authority", "clearance": "Level 5 - Global"
}

users: dict        = {}
puv_database: list = []
audit_logs: list   = []
citations_db: list = []

# --- STARTUP: init DB and load persisted data into memory ---
@app.on_event("startup")
async def startup_event():
    init_db()

    loaded_users = db_load_users()
    if loaded_users:
        users.update(loaded_users)
    else:
        # First run — seed the default admin account
        users["admin"] = _DEFAULT_ADMIN.copy()
        db_save_user("admin", users["admin"])

    puv_database.extend(db_load_puvs())
    audit_logs.extend(db_load_audit_logs())
    citations_db.extend(db_load_citations())
    complaints_db.extend(db_load_complaints())

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
        # Secondary: company name fuzzy match
        company = p.get('company', '').strip().lower()
        if org and company and (org in company or company in org):
            result.append(p)
    return result

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

def compute_puv_stats_for(puvs: list):
    for p in puvs:
        p['is_violator']    = p['passengers'] > p['capacity']
        p['is_overheating'] = p.get('temp', 0) >= 37.5
        p['load_percentage'] = int((p['passengers'] / p['capacity']) * 100) if p['capacity'] > 0 else 0
    return {
        "total_units":       len(puvs),
        "active_violations": len([p for p in puvs if p['is_violator']]),
        "thermal_warnings":  len([p for p in puvs if p.get('is_overheating')]),
        "moving_units":      len([p for p in puvs if p['status'] == 'Active']),
    }

def log_audit(actor: str, event: str, ip: str, category: str = "blue"):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "actor": actor, "event": event, "ip": ip, "category": category
    }
    audit_logs.insert(0, entry)
    db_append_audit(entry)

def TR(request: Request, name: str, context: dict = {}):
    context.setdefault("users_data", users)
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
        f"Login at: http://127.0.0.1:8000/login\n\n"
        f"NOTE: Do not share your credentials with anyone.\n"
        f"- MaxSeat Alert System"
    )

async def send_sms(mobile: str, message: str) -> dict:
    """Send SMS via Semaphore API."""
    if not mobile or SEMAPHORE_API_KEY == "YOUR_SEMAPHORE_API_KEY":
        return {"success": False, "reason": "SMS not configured or no mobile number."}
    # Clean mobile number — Semaphore expects 09XXXXXXXXX or +639XXXXXXXXX
    clean = mobile.replace("-", "").replace(" ", "").replace("+", "")
    if clean.startswith("63"):
        clean = "0" + clean[2:]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.semaphore.co/api/v4/messages",
                data={
                    "apikey":      SEMAPHORE_API_KEY,
                    "number":      clean,
                    "message":     message,
                    "sendername":  SEMAPHORE_SENDER,
                }
            )
        data = resp.json()
        return {"success": resp.status_code == 200, "data": data}
    except Exception as e:
        return {"success": False, "reason": str(e)}

def send_email(to_email: str, full_name: str, message: str) -> dict:
    """Send email via Gmail SMTP."""
    if not to_email or GMAIL_ADDRESS == "your@gmail.com":
        return {"success": False, "reason": "Email not configured or no email address."}
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "MaxSeat Alert — Your Account Credentials"
        msg["From"]    = f"{GMAIL_SENDER_NAME} <{GMAIL_ADDRESS}>"
        msg["To"]      = to_email

        # Plain text
        msg.attach(MIMEText(message, "plain"))

        # HTML version
        html_body = message.replace("\n\n", "</p><p>").replace("\n", "<br>")
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
          <div style="background:#0f172a;padding:24px 32px;">
            <h2 style="color:#fff;margin:0;font-size:20px;">MaxSeat Alert System</h2>
            <p style="color:#94a3b8;margin:4px 0 0;font-size:13px;">Account Credentials</p>
          </div>
          <div style="padding:28px 32px;background:#fff;">
            <p style="color:#0f172a;">Good day, <strong>{full_name}</strong>!</p>
            <p>{html_body}</p>
            <div style="background:#f1f5f9;border-radius:8px;padding:16px 20px;margin:20px 0;font-family:monospace;font-size:14px;">
              {message.split("credentials:")[1].split("Please")[0].strip().replace(chr(10),"<br>") if "credentials:" in message else ""}
            </div>
            <p style="color:#64748b;font-size:12px;">Do not share your credentials with anyone.</p>
          </div>
          <div style="background:#f8fafc;padding:16px 32px;border-top:1px solid #e2e8f0;">
            <p style="color:#94a3b8;font-size:11px;margin:0;">MaxSeat Alert System — CDO Region</p>
          </div>
        </div>"""
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        return {"success": True}
    except Exception as e:
        return {"success": False, "reason": str(e)}

async def notify_new_user(user_data: dict, username: str, password: str):
    """Send credentials via SMS and/or email."""
    full_name = user_data.get("full_name", username)
    role      = user_data.get("role", "")
    mobile    = user_data.get("mobile", "")
    email     = user_data.get("email", "")
    message   = build_credentials_message(full_name, username, password, role)

    results = {}
    if mobile:
        results["sms"]   = await send_sms(mobile, message)
    if email:
        results["email"] = send_email(email, full_name, message)
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
    return TR(request, "login.html", {"messages": get_flash(request)})

@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    role: str = Form(...),
    username: str = Form(...),
    password: str = Form(...)
):
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
    db_save_user(username, users[username])
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
        return TR(request, "enforcer/dashboard.html", {"puvs": sorted_puvs, "role": role, "stats": stats})
    elif role == 'driver':
        my_puv = next((p for p in puv_database if p['username'] == request.session.get('username')), None)
        return TR(request, "driver/dashboard.html", {"puv": my_puv, "role": role})
    elif role == 'cooperative':
        username   = request.session.get('username')
        udata      = users.get(username, {})
        coop_puvs  = get_coop_puvs(username)
        coop_stats = compute_puv_stats_for(coop_puvs)
        return TR(request, "cooperative/dashboard.html", {"puvs": coop_puvs, "role": role, "stats": coop_stats, "user_data": udata})
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
    username = data.get("username", "").strip()
    role     = data.get("role", "driver")
    password = data.get("password", "")

    if not username or username in users:
        return JSONResponse({"success": False, "error": "Username already exists or is empty."})
    if len(password) < 6:
        return JSONResponse({"success": False, "error": "Password must be at least 6 characters."})

    # Base record — shared by every role
    record = {
        "password":             password,
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
    # Send credentials via SMS and/or email
    notify_results = await notify_new_user(record, username, password)
    return JSONResponse({"success": True, "notifications": notify_results})

@app.post("/api/delete_puv")
async def delete_puv(request: Request):
    if get_role(request) not in ['admin', 'cooperative']:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data   = await request.json()
    puv_id = data.get("puv_id")
    puv    = next((p for p in puv_database if p['id'] == puv_id), None)
    if not puv:
        return JSONResponse({"success": False, "error": "Vehicle not found."})
    puv_database.remove(puv)
    db_delete_puv(puv['id'])
    log_audit(request.session.get('username', 'admin'), f"PUV_REMOVED: {puv['plate']}", request.client.host if request.client else "unknown", "red")
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
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "error": "User not found."})

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
    new_capacity = data.get("capacity")
    puv = next((p for p in puv_database if p['id'] == puv_id), None)
    if puv and new_capacity and int(new_capacity) > 0:
        puv['capacity'] = int(new_capacity)
        db_save_puv(puv['id'], puv)
        return JSONResponse({"success": True, "plate": puv['plate'], "capacity": puv['capacity']})
    return JSONResponse({"success": False, "error": "Vehicle not found or invalid capacity."})

@app.get("/audit_logs", response_class=HTMLResponse)
async def audit_logs_page(request: Request):
    if get_role(request) != 'admin': return RedirectResponse(url="/dashboard", status_code=302)
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
    new_capacity = data.get("capacity")
    puv = next((p for p in puv_database if p['id'] == puv_id), None)
    if puv and new_capacity and int(new_capacity) > 0:
        puv['capacity'] = int(new_capacity)
        db_save_puv(puv['id'], puv)
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
    assigned_puv_id = data.get("assigned_puv_id")
    if assigned_puv_id is not None:
        for p in puv_database:
            if p["username"] == username:
                p["username"] = ""
                p["driver"]   = "Unassigned"
                db_save_puv(p["id"], p)
        if assigned_puv_id != "":
            puv = next((p for p in puv_database if p["id"] == int(assigned_puv_id)), None)
            if puv:
                puv["username"] = username
                puv["driver"]   = users[username].get("full_name", username)
                db_save_puv(puv["id"], puv)
    db_save_user(username, users[username])
    log_audit(request.session.get('username', 'cooperative'), f"DRIVER_UPDATED: {username}", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True})

# ==========================================
# MOBILE API  (React Native — Field Enforcer)
# ==========================================
def get_mobile_user(request: Request):
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
    data     = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user     = users.get(username)
    if not user or user["password"] != password or user["role"] != "enforcer":
        return JSONResponse({"success": False, "error": "Invalid credentials."}, status_code=401)
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
                "detail":  f"{p['passengers']}/{p['capacity']} passengers — exceeds legal limit",
                "severity": "critical",
            })
        if p.get("temp", 0) >= 37.5:
            violations.append({
                "type":    "THERMAL",
                "label":   "Thermal alert",
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
    mobile_user = get_mobile_user(request)
    if not mobile_user:
        return JSONResponse({"success": False, "error": "Unauthorized."}, status_code=401)
    data   = await request.json()
    plate  = data.get("plate", "")
    action = data.get("action", "")
    notes  = data.get("notes", "")
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
    log_audit(mobile_user["username"], f"MOBILE_INTERCEPT: {plate} — {action.upper()}", request.client.host if request.client else "unknown", "red")
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
    return passenger_tokens.get(token)

@app.post("/api/passenger/login")
async def passenger_login(request: Request):
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
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        passenger_tokens.pop(auth[7:], None)
    return JSONResponse({"ok": True})

@app.get("/api/passenger/puvs")
async def passenger_list_puvs(request: Request):
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
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    plate_up = plate.upper()
    puv = next((p for p in puv_database if p["plate"].upper() == plate_up), None)
    if not puv:
        return JSONResponse({"error": f"Vehicle '{plate_up}' not found."}, status_code=404)
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
    pax_user = get_passenger_user(request)
    if not pax_user:
        return JSONResponse({"error": "Unauthorized."}, status_code=401)
    body        = await request.json()
    plate       = body.get("plate", "").strip().upper()
    complaint   = body.get("complaint", "").strip()
    description = body.get("description", "").strip()
    if not plate or not complaint:
        return JSONResponse({"error": "Plate and complaint type are required."}, status_code=400)
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
    db_append_complaint(record)
    log_audit(pax_user["username"], f"PASSENGER_COMPLAINT: {plate} — {complaint.upper()}", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({
        "message": "Complaint submitted successfully. Thank you for your report.",
        "serial":  serial,
    })


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
    for key, value in data.items():
        if key in allowed.get(role, []) and value is not None:
            users[username][key] = value
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
    if users.get(username, {}).get('password') != current:
        return JSONResponse({"success": False, "error": "Current password is incorrect."})
    if len(new_pw) < 6:
        return JSONResponse({"success": False, "error": "New password must be at least 6 characters."})
    users[username]['password']             = new_pw
    users[username]['must_change_password'] = False
    db_save_user(username, users[username])
    log_audit(username, "PASSWORD_CHANGED", request.client.host if request.client else "unknown", "green")
    return JSONResponse({"success": True})


# --- AVATAR UPLOAD ---
AVATAR_DIR = "static/avatars"
os.makedirs(AVATAR_DIR, exist_ok=True)

@app.post("/api/upload_avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')

    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed_types:
        return JSONResponse({"success": False, "error": "Only JPG, PNG, WEBP or GIF images are allowed."})

    # Validate file size (max 2MB)
    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        return JSONResponse({"success": False, "error": "Image must be under 2MB."})

    # Save with username as filename
    ext      = file.filename.rsplit(".", 1)[-1].lower()
    filename = f"{username}.{ext}"
    filepath = os.path.join(AVATAR_DIR, filename)

    # Remove old avatar if different extension
    for old_ext in ["jpg", "jpeg", "png", "webp", "gif"]:
        old_path = os.path.join(AVATAR_DIR, f"{username}.{old_ext}")
        if os.path.exists(old_path) and old_path != filepath:
            os.remove(old_path)

    with open(filepath, "wb") as f_out:
        f_out.write(contents)

    avatar_url = f"/static/avatars/{filename}"
    users[username]['avatar'] = avatar_url
    db_save_user(username, users[username])
    log_audit(username, "AVATAR_UPDATED", request.client.host if request.client else "unknown", "blue")
    return JSONResponse({"success": True, "avatar_url": avatar_url})

@app.post("/api/remove_avatar")
async def remove_avatar(request: Request):
    if not is_logged_in(request):
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    username = request.session.get('username')
    for ext in ["jpg", "jpeg", "png", "webp", "gif"]:
        path = os.path.join(AVATAR_DIR, f"{username}.{ext}")
        if os.path.exists(path):
            os.remove(path)
    users[username].pop('avatar', None)
    db_save_user(username, users[username])
    return JSONResponse({"success": True})


@app.post("/api/coop/add_vehicle")
async def coop_add_vehicle(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data     = await request.json()
    plate    = data.get("plate", "").strip().upper()
    company  = data.get("company", "").strip()
    capacity = data.get("capacity", 22)

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
        "capacity":          int(capacity),
        "standing_capacity": int(data.get("standing_capacity", 0)),
        "loc_name":          "CDO — Not yet tracked",
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
    return JSONResponse({"success": True, "plate": plate, "id": new_id})


@app.post("/api/coop/request_capacity")
async def coop_request_capacity(request: Request):
    if get_role(request) != 'cooperative':
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)
    data         = await request.json()
    puv_id       = data.get("puv_id")
    new_capacity = data.get("capacity")
    puv = next((p for p in puv_database if p['id'] == puv_id), None)
    if not puv:
        return JSONResponse({"success": False, "error": "Vehicle not found."})
    if not new_capacity or int(new_capacity) < 1:
        return JSONResponse({"success": False, "error": "Invalid capacity value."})
    standing = int(data.get("standing_capacity", 0))
    # Store as pending — not applied until LTFRB approves
    puv['pending_capacity'] = {
        "seated":   int(new_capacity),
        "standing": standing,
        "total":    int(new_capacity) + standing
    }
    db_save_puv(puv['id'], puv)
    log_audit(
        request.session.get('username', 'cooperative'),
        f"CAPACITY_REQUEST: {puv['plate']} — {new_capacity} seated + {standing} standing (Pending LTFRB)",
        request.client.host if request.client else "unknown",
        "blue"
    )
    return JSONResponse({"success": True, "plate": puv['plate'], "pending_capacity": puv['pending_capacity']})

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
    return JSONResponse({"error": "Vehicle not found"})

@app.post("/api/toggle_name")
async def toggle_name(request: Request):
    if get_role(request) != 'driver': return JSONResponse({"error": "Unauthorized"})
    puv = next((p for p in puv_database if p['username'] == request.session.get('username')), None)
    if puv:
        puv['show_name'] = not puv['show_name']
        return JSONResponse({"success": True, "show_name": puv['show_name']})
    return JSONResponse({"error": "Vehicle not found"})

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
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)