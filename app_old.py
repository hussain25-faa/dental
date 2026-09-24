import datetime as _dt
import email.policy
import errno
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory
from urllib.parse import parse_qs, quote, urlparse


APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get("DENTAL_SECRET_KEY", secrets.token_hex(32))
DB_PATH = Path(os.environ.get("DENTAL_DB", str(APP_DIR / "clinic.sqlite3")))
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
UPLOADS_DIR = APP_DIR / "uploads"


class DentalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _now_local() -> _dt.datetime:
    return _dt.datetime.now()


def _iso_now_minutes() -> str:
    return _now_local().replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _html_escape(s: object) -> str:
    if s is None:
        return ""
    text = str(s)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _money_to_paise(text: str) -> int:
    t = (text or "").strip()
    if not t:
        return 0
    # Accept "100", "100.5", "100.50"
    negative = t.startswith("-")
    if negative:
        t = t[1:].strip()
    if "." in t:
        whole, frac = t.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = t, "00"
    whole = whole.strip() or "0"
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError("Invalid money value")
    val = int(whole) * 100 + int(frac)
    return -val if negative else val


def _paise_to_money(paise: int) -> str:
    neg = paise < 0
    paise = abs(int(paise))
    whole = paise // 100
    frac = paise % 100
    s = f"{whole}.{frac:02d}"
    return f"-{s}" if neg else s


_db_lock = threading.Lock()
_sessions_lock = threading.Lock()
_sessions: dict[str, dict[str, str]] = {}
_request_ctx = threading.local()
MODULES = ["dashboard", "patients", "appointments", "attendance", "staff", "expenses", "reports", "settings"]
MODULE_LABELS = {
    "dashboard": "Dashboard",
    "patients": "Patients",
    "appointments": "Appointments",
    "attendance": "Attendance",
    "staff": "Staff",
    "expenses": "Expenses",
    "reports": "Reports",
    "settings": "Settings",
}
DEFAULT_ROLE_PERMISSIONS = {
    "Doctor": ["dashboard", "patients", "appointments", "attendance"],
    "Receptionist": ["dashboard", "patients", "appointments", "attendance"],
    "Assistant": ["dashboard", "attendance"],
    "Manager": ["dashboard", "patients", "appointments", "attendance", "staff", "expenses", "reports", "settings"],
    "Staff": ["attendance"],
}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def db_init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        conn = db_connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS patients (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  patient_code TEXT UNIQUE NOT NULL,
                  name TEXT NOT NULL,
                  age INTEGER,
                  gender TEXT,
                  phone TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS visits (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                  visit_at TEXT NOT NULL,
                  doctor_assigned TEXT,
                  treatment_details TEXT,
                  notes TEXT,
                  cost_paise INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS appointments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                  appointment_at TEXT NOT NULL,
                  doctor_name TEXT,
                  note TEXT,
                  status TEXT NOT NULL DEFAULT 'Scheduled',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS staff (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT UNIQUE NOT NULL,
                  phone TEXT,
                  address TEXT,
                  aadhar_no TEXT,
                  staff_image_path TEXT,
                  aadhar_image_path TEXT,
                  role TEXT NOT NULL DEFAULT 'Staff',
                  password TEXT NOT NULL DEFAULT '',
                  monthly_salary_paise INTEGER NOT NULL DEFAULT 0,
                  active INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attendance (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                  day TEXT NOT NULL,
                  status TEXT NOT NULL, -- Present / Absent
                  check_in TEXT,
                  check_out TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(staff_id, day)
                );

                CREATE TABLE IF NOT EXISTS staff_adjustments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
                  day TEXT NOT NULL,
                  kind TEXT NOT NULL, -- Expense / Bonus / Deduction
                  amount_paise INTEGER NOT NULL DEFAULT 0,
                  note TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clinic_expenses (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  day TEXT NOT NULL,
                  category TEXT NOT NULL, -- Equipment / Medicines / Rent / Electricity / Other
                  amount_paise INTEGER NOT NULL DEFAULT 0,
                  note TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS billing_invoices (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                  invoice_no TEXT UNIQUE NOT NULL,
                  invoice_day TEXT NOT NULL,
                  treatment_details TEXT,
                  amount_paise INTEGER NOT NULL DEFAULT 0,
                  paid_amount_paise INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'Pending',
                  note TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_visits_patient_id ON visits(patient_id);
                CREATE INDEX IF NOT EXISTS idx_appointments_at ON appointments(appointment_at);
                CREATE INDEX IF NOT EXISTS idx_attendance_day ON attendance(day);
                CREATE INDEX IF NOT EXISTS idx_staff_adj_day ON staff_adjustments(day);
                CREATE INDEX IF NOT EXISTS idx_clinic_exp_day ON clinic_expenses(day);
                CREATE INDEX IF NOT EXISTS idx_billing_patient_day ON billing_invoices(patient_id, invoice_day);
                """
            )
            # Simple migrations for existing DBs (keep offline + no tooling).
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(staff)").fetchall()]
            if "phone" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN phone TEXT;")
            if "role" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN role TEXT NOT NULL DEFAULT 'Staff';")
            if "password" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN password TEXT NOT NULL DEFAULT '';")
            if "address" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN address TEXT;")
            if "aadhar_no" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN aadhar_no TEXT;")
            if "staff_image_path" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN staff_image_path TEXT;")
            if "aadhar_image_path" not in cols:
                conn.execute("ALTER TABLE staff ADD COLUMN aadhar_image_path TEXT;")
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('clinic_name', 'Dental Clinic')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('clinic_tagline', 'Management System')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('admin_password', 'admin123')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('theme', 'ocean')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('role_options', 'Doctor,Receptionist,Assistant,Manager,Staff')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('clinic_phone', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('clinic_email', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('clinic_address', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('currency_symbol', 'Rs.')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('appointment_minutes', '30')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('role_permissions', '{}')"
            )
            conn.commit()
        finally:
            conn.close()


def _next_patient_code(conn: sqlite3.Connection) -> str:
    # Stable and readable: PT-000001
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM patients").fetchone()
    next_id = int(row["max_id"]) + 1
    return f"PT-{next_id:06d}"


def _next_invoice_no(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM billing_invoices").fetchone()
    next_id = int(row["max_id"]) + 1
    return f"INV-{next_id:06d}"


def _render_template(name: str, ctx: dict) -> str:
    path = TEMPLATES_DIR / name
    text = path.read_text(encoding="utf-8")
    # Minimal templating: {{key}} replacement only.
    # Keep it simple to stay dependency-free and offline-friendly.
    for k, v in ctx.items():
        text = text.replace("{{" + k + "}}", str(v))
    # Remove unreplaced placeholders (avoid showing raw tokens to end users).
    while "{{" in text and "}}" in text:
        start = text.find("{{")
        end = text.find("}}", start + 2)
        if end == -1:
            break
        text = text[:start] + "" + text[end + 2 :]
    return text


def _layout(title: str, body_html: str, *, flash: str = "", user_name: str = "Admin") -> str:
    flash_html = ""
    if flash:
        flash_html = f'<div class="flash">{_html_escape(flash)}</div>'
    current_name = getattr(_request_ctx, "user_name", user_name)
    clinic_name = getattr(_request_ctx, "clinic_name", "Dental Clinic")
    clinic_tagline = getattr(_request_ctx, "clinic_tagline", "Management System")
    clinic_phone = getattr(_request_ctx, "clinic_phone", "")
    clinic_email = getattr(_request_ctx, "clinic_email", "")
    clinic_address = getattr(_request_ctx, "clinic_address", "")
    theme_name = getattr(_request_ctx, "theme", "ocean")
    current_role = getattr(_request_ctx, "user_role", "Admin")
    nav_items_html = getattr(_request_ctx, "nav_items_html", "")
    alert_bell_html = getattr(_request_ctx, "alert_bell_html", "")
    return _render_template(
        "layout.html",
        {
            "title": _html_escape(title),
            "flash_html": flash_html,
            "body_html": body_html,
            "welcome_name": _html_escape(current_name),
            "welcome_role": _html_escape(current_role),
            "clinic_name": _html_escape(clinic_name),
            "clinic_tagline": _html_escape(clinic_tagline),
            "clinic_phone": _html_escape(clinic_phone),
            "clinic_email": _html_escape(clinic_email),
            "clinic_address": _html_escape(clinic_address),
            "theme_name": _html_escape(theme_name),
            "nav_items_html": nav_items_html,
            "alert_bell_html": alert_bell_html,
        },
    )


def _get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    data = {str(r["key"]): str(r["value"]) for r in rows}
    data.setdefault("clinic_name", "Dental Clinic")
    data.setdefault("clinic_tagline", "Management System")
    data.setdefault("admin_password", "admin123")
    data.setdefault("theme", "ocean")
    data.setdefault("role_options", "Doctor,Receptionist,Assistant,Manager,Staff")
    data.setdefault("clinic_phone", "")
    data.setdefault("clinic_email", "")
    data.setdefault("clinic_address", "")
    data.setdefault("currency_symbol", "Rs.")
    data.setdefault("appointment_minutes", "30")
    data.setdefault("role_permissions", "{}")
    return data


def _role_values(settings: dict[str, str]) -> list[str]:
    raw = settings.get("role_options", "")
    roles = [part.strip() for part in raw.split(",") if part.strip()]
    return roles or ["Staff"]


def _role_options_html(settings: dict[str, str], selected: str = "Staff") -> str:
    return "".join(
        f"<option value='{_html_escape(role)}'{' selected' if role == selected else ''}>{_html_escape(role)}</option>"
        for role in _role_values(settings)
    )


def _current_role() -> str:
    return str(getattr(_request_ctx, "user_role", "Admin") or "Admin")


def _is_admin_user() -> bool:
    return str(getattr(_request_ctx, "is_admin", "0")) == "1"


def _can_delete_records() -> bool:
    if _is_admin_user():
        return True
    return _current_role() in {"Manager"}


def _normalize_module_name(value: str) -> str:
    cleaned = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    aliases = {
        "dashboard": "dashboard",
        "home": "dashboard",
        "patient": "patients",
        "patients": "patients",
        "appointment": "appointments",
        "appointments": "appointments",
        "attendance": "attendance",
        "staff": "staff",
        "expense": "expenses",
        "expenses": "expenses",
        "report": "reports",
        "reports": "reports",
        "settings": "settings",
    }
    return aliases.get(cleaned, "")


def _default_role_permissions(settings: dict[str, str]) -> dict[str, list[str]]:
    defaults: dict[str, list[str]] = {}
    for role in _role_values(settings):
        defaults[role] = list(DEFAULT_ROLE_PERMISSIONS.get(role, ["dashboard"]))
    defaults.setdefault("Staff", list(DEFAULT_ROLE_PERMISSIONS["Staff"]))
    return defaults


def _get_role_permissions(settings: dict[str, str]) -> dict[str, list[str]]:
    defaults = _default_role_permissions(settings)
    raw = settings.get("role_permissions", "{}").strip() or "{}"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return defaults
    if not isinstance(loaded, dict):
        return defaults
    parsed: dict[str, list[str]] = {}
    for role, modules in loaded.items():
        if not isinstance(role, str):
            continue
        if isinstance(modules, str):
            tokens = [m.strip() for m in modules.split(",")]
        elif isinstance(modules, list):
            tokens = [str(m).strip() for m in modules]
        else:
            continue
        cleaned = []
        for token in tokens:
            module_name = _normalize_module_name(token)
            if module_name and module_name not in cleaned:
                cleaned.append(module_name)
        parsed[role.strip()] = cleaned or list(defaults.get(role.strip(), ["dashboard"]))
    for role, modules in defaults.items():
        parsed.setdefault(role, list(modules))
    return parsed


def _permissions_table_html(settings: dict[str, str]) -> str:
    permissions = _get_role_permissions(settings)
    headers = "".join(f"<th>{_html_escape(MODULE_LABELS[module])}</th>" for module in MODULES)
    rows = []
    for role in _role_values(settings):
        allowed = set(permissions.get(role, []))
        checks = []
        for module in MODULES:
            checked = "checked" if module in allowed else ""
            checks.append(
                "<td>"
                f"<label class='checkslot'><input type='checkbox' name='perm__{_html_escape(role)}__{module}' value='1' {checked} />"
                "<span></span></label>"
                "</td>"
            )
        rows.append(f"<tr><th>{_html_escape(role)}</th>{''.join(checks)}</tr>")
    return (
        "<div class='tablewrap'><table class='permissions-table'>"
        f"<thead><tr><th>Role</th>{headers}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _roles_from_staff(conn: sqlite3.Connection, *, active_only: bool = False) -> list[str]:
    where = "active = 1 AND " if active_only else ""
    rows = conn.execute(
        f"SELECT DISTINCT role FROM staff WHERE {where}trim(COALESCE(role, '')) <> '' ORDER BY role COLLATE NOCASE ASC"
    ).fetchall()
    roles = [str(row["role"]).strip() for row in rows if str(row["role"]).strip()]
    return roles or ["Staff"]


def _allowed_modules(session: dict[str, str] | None, settings: dict[str, str]) -> set[str]:
    if session and session.get("is_admin") == "1":
        return set(MODULES)
    role = (session or {}).get("user_role", "Staff")
    return set(_get_role_permissions(settings).get(role, ["dashboard"]))


def _has_module_access(session: dict[str, str] | None, settings: dict[str, str], module: str) -> bool:
    return module in _allowed_modules(session, settings)


def _nav_items_html(session: dict[str, str] | None, settings: dict[str, str]) -> str:
    allowed = _allowed_modules(session, settings)
    items = [
        ("/", "dashboard", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1v-9.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>"""),
        ("/patients", "patients", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" stroke="currentColor" stroke-width="1.8"/><path d="M22 21v-2a4 4 0 0 0-3-3.87" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M16 3.13a4 4 0 0 1 0 7.75" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>"""),
        ("/appointments", "appointments", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M8 3v3M16 3v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M4 8h16" stroke="currentColor" stroke-width="1.8"/><rect x="4" y="5" width="16" height="16" rx="2" stroke="currentColor" stroke-width="1.8"/><path d="m9 14 2 2 4-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>"""),
        ("/attendance", "attendance", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M8 7V3M16 7V3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M4 9h16" stroke="currentColor" stroke-width="1.8"/><path d="M6 5h12a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" stroke="currentColor" stroke-width="1.8"/><path d="m9 14 2 2 4-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>"""),
        ("/staff", "staff", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" stroke="currentColor" stroke-width="1.8"/></svg>"""),
        ("/expenses", "expenses", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M21 7H3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M21 10v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V10" stroke="currentColor" stroke-width="1.8"/><path d="M7 14h2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M3 7l1-3h16l1 3" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>"""),
        ("/reports", "reports", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M5 20V10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12 20V4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M19 20v-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M3 20h18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>"""),
        ("/settings", "settings", """<svg class="navicon" viewBox="0 0 24 24" fill="none"><path d="M12 15.5A3.5 3.5 0 1 0 12 8.5a3.5 3.5 0 0 0 0 7Z" stroke="currentColor" stroke-width="1.8"/><path d="M19.4 15a1 1 0 0 0 .2 1.1l.1.1a2 2 0 0 1 0 2.8l-.1.1a2 2 0 0 1-2.8 0l-.1-.1a1 1 0 0 0-1.1-.2 1 1 0 0 0-.6.9V20a2 2 0 0 1-2 2h-.2a2 2 0 0 1-2-2v-.2a1 1 0 0 0-.7-.9 1 1 0 0 0-1.1.2l-.1.1a2 2 0 0 1-2.8 0l-.1-.1a2 2 0 0 1 0-2.8l.1-.1a1 1 0 0 0 .2-1.1 1 1 0 0 0-.9-.6H4a2 2 0 0 1-2-2v-.2a2 2 0 0 1 2-2h.2a1 1 0 0 0 .9-.7 1 1 0 0 0-.2-1.1l-.1-.1a2 2 0 0 1 0-2.8l.1-.1a2 2 0 0 1 2.8 0l.1.1a1 1 0 0 0 1.1.2H9a1 1 0 0 0 .6-.9V4a2 2 0 0 1 2-2h.2a2 2 0 0 1 2 2v.2a1 1 0 0 0 .6.9 1 1 0 0 0 1.1-.2l.1-.1a2 2 0 0 1 2.8 0l.1.1a2 2 0 0 1 0 2.8l-.1.1a1 1 0 0 0-.2 1.1V9c0 .4.2.7.6.9H20a2 2 0 0 1 2 2v.2a2 2 0 0 1-2 2h-.2a1 1 0 0 0-.6.9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>"""),
    ]
    parts = []
    for href, module, icon in items:
        if module not in allowed:
            continue
        parts.append(
            f"<a class='navitem' data-path='{href}' href='{href}'><span class='navdot'></span>{icon}{_html_escape(MODULE_LABELS[module])}</a>"
        )
    return "".join(parts)


def _default_landing_path(session: dict[str, str] | None, settings: dict[str, str]) -> str:
    allowed = _allowed_modules(session, settings)
    for module in MODULES:
        if module not in allowed:
            continue
        if module == "dashboard":
            return "/"
        return f"/{module}"
    return "/login"


def _read_session(handler: BaseHTTPRequestHandler) -> dict[str, str] | None:
    raw = handler.headers.get("Cookie") or ""
    if not raw:
        return None
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie.get("session_id")
    if not morsel:
        return None
    with _sessions_lock:
        session = _sessions.get(morsel.value)
        return dict(session) if session else None


def _create_session(data: dict[str, str]) -> str:
    session_id = secrets.token_hex(16)
    with _sessions_lock:
        _sessions[session_id] = dict(data)
    return session_id


def _destroy_session(handler: BaseHTTPRequestHandler) -> None:
    raw = handler.headers.get("Cookie") or ""
    if not raw:
        return
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie.get("session_id")
    if not morsel:
        return
    with _sessions_lock:
        _sessions.pop(morsel.value, None)


def page_login(error: str = "") -> str:
    error_html = f"<div class='flash error'>{_html_escape(error)}</div>" if error else ""
    with _db_lock:
        conn = db_connect()
        try:
            settings = _get_settings(conn)
        finally:
            conn.close()
    body = _render_template(
        "login.html",
        {
            "error_html": error_html,
            "clinic_name": _html_escape(settings.get("clinic_name", "Dental Clinic")),
            "clinic_tagline": _html_escape(settings.get("clinic_tagline", "Management System")),
            "demo_note": "Admin login works with the clinic admin password. Staff login works with each staff member name and password.",
        },
    )
    return _render_template(
        "login_layout.html",
        {
            "title": "Clinic Login",
            "body_html": body,
            "theme_name": _html_escape(settings.get("theme", "ocean")),
        },
    )


def _redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def _appointment_alert_payload(conn: sqlite3.Connection, settings: dict[str, str]) -> tuple[int, str]:
    today = _now_local().date().isoformat()
    overdue_appointments = conn.execute(
        """
        SELECT a.id, a.appointment_at, p.name AS patient_name, p.phone AS patient_phone
        FROM appointments a
        JOIN patients p ON p.id = a.patient_id
        WHERE a.status = 'Scheduled' AND substr(a.appointment_at,1,10) < ?
        ORDER BY a.appointment_at ASC, a.id ASC
        LIMIT 8
        """,
        (today,),
    ).fetchall()
    rows = []
    for row in overdue_appointments:
        wa_link = _whatsapp_link(
            row["patient_phone"] or "",
            f"Hello {row['patient_name']}, please contact {settings.get('clinic_name', 'Dental Clinic')} about your missed appointment.",
        )
        wa_btn = (
            f"<a class='alertlink' target='_blank' rel='noopener noreferrer' href='{wa_link}'>WhatsApp</a>"
            if wa_link
            else ""
        )
        rows.append(
            "<div class='alertitem'>"
            f"<div class='alerttitle'>{_html_escape(row['patient_name'])}</div>"
            f"<div class='alertmeta'>{_html_escape(row['appointment_at'])}</div>"
            f"<div class='alertactions'><a class='alertlink' href='/appointments'>Open</a>{wa_btn}</div>"
            "</div>"
        )
    items_html = "".join(rows) if rows else "<div class='alertempty'>No previous-date appointment alerts.</div>"
    count = len(overdue_appointments)
    bell_html = (
        "<details class='alertbell'>"
        "<summary class='alerttoggle' title='Appointment alerts'>"
        "<svg viewBox='0 0 24 24' fill='none'><path d='M15 17H5l1.2-1.6A2 2 0 0 0 6.6 14V10a5.4 5.4 0 0 1 10.8 0v4c0 .5.2 1 .5 1.4L19 17h-4Z' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M10 20a2 2 0 0 0 4 0' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'/></svg>"
        f"{'<span class=\"alertdot\"></span>' if count else ''}"
        "</summary>"
        "<div class='alertpanel'>"
        "<div class='alerthead'>Appointment Alerts</div>"
        f"<div class='alertsub'>{count} pending follow-up</div>"
        f"{items_html}"
        "</div>"
        "</details>"
    )
    return count, bell_html


def _apply_request_context(session: dict[str, str], settings: dict[str, str], conn: sqlite3.Connection) -> None:
    _request_ctx.user_name = session.get("user_name", "Admin")
    _request_ctx.user_role = session.get("user_role", "Admin")
    _request_ctx.is_admin = session.get("is_admin", "0")
    _request_ctx.clinic_name = settings.get("clinic_name", "Dental Clinic")
    _request_ctx.clinic_tagline = settings.get("clinic_tagline", "Management System")
    _request_ctx.clinic_phone = settings.get("clinic_phone", "")
    _request_ctx.clinic_email = settings.get("clinic_email", "")
    _request_ctx.clinic_address = settings.get("clinic_address", "")
    _request_ctx.theme = settings.get("theme", "ocean")
    _request_ctx.nav_items_html = _nav_items_html(session, settings)
    if _has_module_access(session, settings, "appointments") or _has_module_access(session, settings, "dashboard"):
        _, bell_html = _appointment_alert_payload(conn, settings)
        _request_ctx.alert_bell_html = bell_html
    else:
        _request_ctx.alert_bell_html = ""


def _access_denied_page() -> str:
    return _layout(
        "Access Denied",
        "<div class='card'><h1>Access denied</h1><div class='muted'>Your role does not have permission to open this module.</div></div>",
    )


def _parse_multipart_form(raw: bytes, content_type: str) -> dict:
    msg = BytesParser(policy=email.policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
    )
    values: dict[str, object] = {}
    if not msg.is_multipart():
        return values
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            values[name] = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": payload,
            }
        else:
            values[name] = payload.decode("utf-8", "replace")
    return values


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _whatsapp_link(phone: str, message: str) -> str:
    clean = _normalize_phone(phone)
    if not clean:
        return ""
    return f"https://wa.me/{clean}?text={quote(message or '')}"


def _read_form(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b""
    ct = handler.headers.get("Content-Type") or ""
    if "application/x-www-form-urlencoded" in ct:
        data = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in data.items()}
    if "multipart/form-data" in ct:
        return _parse_multipart_form(raw, ct)
    return {}


def _save_uploaded_file(file_obj: object, prefix: str) -> str:
    if not isinstance(file_obj, dict):
        return ""
    filename = str(file_obj.get("filename") or "").strip()
    data = file_obj.get("data")
    if not filename or not isinstance(data, (bytes, bytearray)) or not data:
        return ""
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        ext = ".bin"
    safe_name = f"{prefix}_{secrets.token_hex(8)}{ext}"
    target = UPLOADS_DIR / safe_name
    target.write_bytes(bytes(data))
    return safe_name


def _image_html(rel_name: str, alt_text: str, css_class: str = "thumb") -> str:
    if not rel_name:
        return "<span class='muted'>No image</span>"
    src = f"/uploads/{_html_escape(rel_name)}"
    return f"<img class='{css_class}' src='{src}' alt='{_html_escape(alt_text)}' />"


def _query_params(handler: BaseHTTPRequestHandler) -> dict:
    u = urlparse(handler.path)
    data = parse_qs(u.query, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in data.items()}


def _path(handler: BaseHTTPRequestHandler) -> str:
    return urlparse(handler.path).path


def _bind_server(host: str, preferred_port: int, attempts: int = 20) -> tuple[DentalHTTPServer, int]:
    last_error = None
    for offset in range(attempts):
        port = preferred_port + offset
        try:
            return DentalHTTPServer((host, port), Handler), port
        except OSError as exc:
            last_error = exc
            if exc.errno not in {errno.EADDRINUSE, 10048}:
                raise
    assert last_error is not None
    raise last_error


def page_dashboard() -> str:
    with _db_lock:
        conn = db_connect()
        try:
            p = conn.execute("SELECT COUNT(*) AS c FROM patients").fetchone()["c"]
            v = conn.execute("SELECT COUNT(*) AS c FROM visits").fetchone()["c"]
            s = conn.execute("SELECT COUNT(*) AS c FROM staff WHERE active = 1").fetchone()["c"]
            today = _now_local().date().isoformat()
            a = conn.execute(
                "SELECT COUNT(*) AS c FROM attendance WHERE day = ? AND status = 'Present'",
                (today,),
            ).fetchone()["c"]
            today_visits = conn.execute(
                "SELECT COUNT(*) AS c FROM visits WHERE substr(visit_at,1,10) = ?",
                (today,),
            ).fetchone()["c"]
            month_start = _now_local().date().replace(day=1).isoformat()
            visits_month = conn.execute(
                "SELECT COUNT(*) AS c FROM visits WHERE substr(visit_at,1,10) >= ?",
                (month_start,),
            ).fetchone()["c"]
            clinic_month = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS s FROM clinic_expenses WHERE day >= ?",
                (month_start,),
            ).fetchone()["s"]
            staff_month = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS s FROM staff_adjustments WHERE day >= ? AND kind = 'Expense'",
                (month_start,),
            ).fetchone()["s"]
            expenses_month = int(clinic_month) + int(staff_month)
            today_rows = conn.execute(
                """
                SELECT v.visit_at, v.doctor_assigned, v.treatment_details, v.cost_paise,
                       p.name AS patient_name
                FROM visits v
                JOIN patients p ON p.id = v.patient_id
                WHERE substr(v.visit_at,1,10) = ?
                ORDER BY v.visit_at ASC, v.id ASC
                LIMIT 8
                """,
                (today,),
            ).fetchall()
            overdue_appointments = conn.execute(
                """
                SELECT a.id, a.appointment_at, p.name AS patient_name, p.phone AS patient_phone
                FROM appointments a
                JOIN patients p ON p.id = a.patient_id
                WHERE a.status = 'Scheduled' AND substr(a.appointment_at,1,10) < ?
                ORDER BY a.appointment_at ASC, a.id ASC
                LIMIT 8
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
    trs = []
    for r in today_rows:
        t = (r["visit_at"] or "")
        time = _html_escape(t[11:16] if len(t) >= 16 else t)
        patient = _html_escape(r["patient_name"] or "")
        treat = _html_escape(r["treatment_details"] or "")
        doc = _html_escape(r["doctor_assigned"] or "")
        cost = _html_escape(_paise_to_money(r["cost_paise"]))
        trs.append(
            "<tr>"
            f"<td class='mono'>{time}</td>"
            f"<td>{patient}</td>"
            f"<td>{treat}</td>"
            f"<td>{doc}</td>"
            f"<td class='right mono'>{cost}</td>"
            "</tr>"
        )
    today_visits_rows_html = "".join(trs) if trs else "<tr><td colspan='5' class='muted'>No visits for today.</td></tr>"
    alert_rows = []
    for row in overdue_appointments:
        alert_rows.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(row['appointment_at'])}</td>"
            f"<td>{_html_escape(row['patient_name'])}</td>"
            f"<td>{_html_escape(row['patient_phone'] or '')}</td>"
            f"<td class='right'><a class='btn secondary btn-sm' href='/appointments'>Open</a></td>"
            "</tr>"
        )
    overdue_alerts_html = "".join(alert_rows) if alert_rows else "<tr><td colspan='4' class='muted'>No previous-date appointment alerts.</td></tr>"
    body = _render_template(
        "dashboard.html",
        {
            "patients_count": str(p),
            "visits_count": str(v),
            "staff_count": str(s),
            "today": _html_escape(today),
            "today_present": str(a),
            "db_path": _html_escape(str(DB_PATH)),
            "today_visits": str(today_visits),
            "visits_month": str(visits_month),
            "expenses_month": _html_escape(_paise_to_money(expenses_month)),
            "today_visits_rows_html": today_visits_rows_html,
            "overdue_count": str(len(overdue_appointments)),
            "overdue_alerts_html": overdue_alerts_html,
        },
    )
    return _layout("Dashboard", body)


def page_patients_list(q: dict, flash: str = "") -> str:
    search = (q.get("q") or "").strip()
    with _db_lock:
        conn = db_connect()
        try:
            totals = conn.execute(
                """
                SELECT
                  COUNT(*) AS total_patients,
                  COUNT(CASE WHEN substr(created_at,1,10) = ? THEN 1 END) AS added_today
                FROM patients
                """,
                (_now_local().date().isoformat(),),
            ).fetchone()
            if search:
                rows = conn.execute(
                    """
                    SELECT p.*,
                           (SELECT COUNT(*) FROM visits v WHERE v.patient_id = p.id) AS visit_count,
                           lv.visit_at AS last_visit_at,
                           lv.treatment_details AS last_treatment_details
                    FROM patients p
                    LEFT JOIN visits lv ON lv.id = (
                      SELECT v2.id FROM visits v2
                      WHERE v2.patient_id = p.id
                      ORDER BY v2.visit_at DESC, v2.id DESC
                      LIMIT 1
                    )
                    WHERE p.name LIKE ? OR p.phone LIKE ? OR p.patient_code LIKE ?
                    ORDER BY p.id DESC
                    LIMIT 200
                    """,
                    (f"%{search}%", f"%{search}%", f"%{search}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT p.*,
                           (SELECT COUNT(*) FROM visits v WHERE v.patient_id = p.id) AS visit_count,
                           lv.visit_at AS last_visit_at,
                           lv.treatment_details AS last_treatment_details
                    FROM patients p
                    LEFT JOIN visits lv ON lv.id = (
                      SELECT v2.id FROM visits v2
                      WHERE v2.patient_id = p.id
                      ORDER BY v2.visit_at DESC, v2.id DESC
                      LIMIT 1
                    )
                    ORDER BY p.id DESC
                    LIMIT 200
                    """
                ).fetchall()
        finally:
            conn.close()
    trs = []
    color_i = 0
    can_delete = _can_delete_records()
    for r in rows:
        name = _html_escape(r["name"])
        age = "" if r["age"] is None else _html_escape(r["age"])
        phone = _html_escape(r["phone"] or "")
        last_at = _html_escape((r["last_visit_at"] or ""))
        treatment_text = (r["last_treatment_details"] or "").strip()
        treatment = _html_escape(treatment_text) if treatment_text else "<span class='muted'>No treatment added</span>"
        wa_link = _whatsapp_link(
            r["phone"] or "",
            f"Hello {r['name']}, we have appointment time available at our clinic. Please reply if you want to book your visit.",
        )
        wa_btn = (
            f"<a class='iconbtn' title='WhatsApp' target='_blank' rel='noopener noreferrer' href='{wa_link}'>"
            f"<svg viewBox='0 0 24 24' fill='none'><path d='M20 11.5A8.5 8.5 0 0 1 7.4 19l-3.4 1 1.1-3.2A8.5 8.5 0 1 1 20 11.5Z' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M9.5 8.8c.2-.4.4-.4.6-.4h.5c.2 0 .4 0 .5.4l.6 1.5c.1.2.1.4 0 .6l-.5.8c.4.8 1 1.4 1.8 1.8l.8-.5c.2-.1.4-.1.6 0l1.5.6c.4.2.4.3.4.5v.5c0 .2 0 .4-.4.6-.5.3-1 .4-1.6.3-2.7-.7-4.8-2.8-5.5-5.5-.1-.6 0-1.1.3-1.6Z' stroke='currentColor' stroke-width='1.4' stroke-linejoin='round'/></svg>"
            f"</a>&nbsp;"
            if wa_link else ""
        )
        avatar = _html_escape((r["name"] or "P")[:1].upper())
        color_i = (color_i + 1) % 6
        acolor = f"a{color_i + 1}"
        delete_btn = (
            f"<a class='iconbtn' title='Delete' href='/patients/{r['id']}/delete' onclick=\"return confirm('Delete this patient?');\">"
            f"<svg viewBox='0 0 24 24' fill='none'><path d='M3 6h18' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'/><path d='M8 6V4h8v2' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M10 11v6M14 11v6' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'/></svg>"
            f"</a>"
            if can_delete
            else ""
        )
        trs.append(
            f"<tr>"
            f"<td><div class='cellname'><span class='avatar {acolor}'>{avatar}</span><div><b>{name}</b><br><small>{_html_escape(r['patient_code'])}</small></div></div></td>"
            f"<td>{age}</td>"
            f"<td>{phone}</td>"
            f"<td class='mono'>{last_at}</td>"
            f"<td><div class='treatcell'>{treatment}</div></td>"
            f"<td class='right'>"
            f"<a class='iconbtn' title='View' href='/patients/{r['id']}'>"
            f"<svg viewBox='0 0 24 24' fill='none'><path d='M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z' stroke='currentColor' stroke-width='1.8'/></svg>"
            f"</a>"
            f"&nbsp;"
            f"{wa_btn}"
            f"{delete_btn}"
            f"</td>"
            f"</tr>"
        )
    rows_html = "".join(trs) if trs else "<tr><td colspan='6' class='muted'>No patients yet.</td></tr>"
    body = _render_template(
        "patients_list.html",
        {
            "search": _html_escape(search),
            "patients_rows_html": rows_html,
            "patients_total": str(int(totals["total_patients"] or 0)),
            "patients_added_today": str(int(totals["added_today"] or 0)),
        },
    )
    return _layout("Patients", body, flash=flash)


def page_patient_new(error: str = "") -> str:
    error_html = ""
    if error:
        error_html = f"<div class='flash error'>{_html_escape(error)}</div>"
    body = _render_template("patient_new.html", {"error_html": error_html})
    return _layout("New Patient", body)


def page_patient_detail(patient_id: int, flash: str = "") -> str:
    with _db_lock:
        conn = db_connect()
        try:
            p = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
            if not p:
                return _layout("Not Found", "<div class='card'><h1>Patient not found</h1></div>")
            visits = conn.execute(
                "SELECT * FROM visits WHERE patient_id = ? ORDER BY visit_at DESC, id DESC",
                (patient_id,),
            ).fetchall()
            upcoming_appointments = conn.execute(
                """
                SELECT * FROM appointments
                WHERE patient_id = ? AND status = 'Scheduled'
                ORDER BY appointment_at ASC, id ASC
                LIMIT 10
                """,
                (patient_id,),
            ).fetchall()
            invoices = conn.execute(
                """
                SELECT * FROM billing_invoices
                WHERE patient_id = ?
                ORDER BY invoice_day DESC, id DESC
                LIMIT 30
                """,
                (patient_id,),
            ).fetchall()
            settings = _get_settings(conn)
        finally:
            conn.close()

    vtrs = []
    for v in visits:
        vtrs.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(v['visit_at'])}</td>"
            f"<td>{_html_escape(v['doctor_assigned'] or '')}</td>"
            f"<td>{_html_escape(v['treatment_details'] or '')}<br><small>{_html_escape(v['notes'] or '')}</small></td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(v['cost_paise']))}</td>"
            "</tr>"
        )
    visits_rows_html = "".join(vtrs) if vtrs else "<tr><td colspan='5' class='muted'>No visits yet.</td></tr>"
    if not vtrs:
        visits_rows_html = "<tr><td colspan='4' class='muted'>No visits yet.</td></tr>"
    atrs = []
    for a in upcoming_appointments:
        atrs.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(a['appointment_at'])}</td>"
            f"<td>{_html_escape(a['doctor_name'] or '')}</td>"
            f"<td>{_html_escape(a['note'] or '')}</td>"
            f"<td>{_html_escape(a['status'] or '')}</td>"
            "</tr>"
        )
    appointments_rows_html = "".join(atrs) if atrs else "<tr><td colspan='4' class='muted'>No scheduled appointments.</td></tr>"
    invoice_rows = []
    for invoice in invoices:
        pending_paise = int(invoice["amount_paise"] or 0) - int(invoice["paid_amount_paise"] or 0)
        invoice_rows.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(invoice['invoice_no'])}</td>"
            f"<td class='mono'>{_html_escape(invoice['invoice_day'])}</td>"
            f"<td>{_html_escape(invoice['treatment_details'] or '')}</td>"
            f"<td class='mono right'>{_html_escape(_paise_to_money(int(invoice['amount_paise'] or 0)))}</td>"
            f"<td class='mono right'>{_html_escape(_paise_to_money(pending_paise))}</td>"
            f"<td><span class='pill'>{_html_escape(invoice['status'])}</span></td>"
            f"<td class='right actionrow'><a class='btn secondary btn-sm' href='/invoices/{int(invoice['id'])}'>View</a><a class='btn secondary btn-sm' href='/invoices/{int(invoice['id'])}?print=1' target='_blank' rel='noopener noreferrer'>Print PDF</a></td>"
            "</tr>"
        )
    invoice_rows_html = "".join(invoice_rows) if invoice_rows else "<tr><td colspan='7' class='muted'>No invoices yet.</td></tr>"
    patient_phone = _html_escape(p["phone"] or "")
    whatsapp_href = _whatsapp_link(
        p["phone"] or "",
        f"Hello {p['name']}, your appointment with {settings.get('clinic_name', 'Dental Clinic')} is scheduled. Please contact us if you need any change.",
    )
    body = _render_template(
        "patient_detail.html",
        {
            "patient_id": str(patient_id),
            "patient_code": _html_escape(p["patient_code"]),
            "patient_name": _html_escape(p["name"]),
            "patient_age": _html_escape(p["age"] if p["age"] is not None else ""),
            "patient_gender": _html_escape(p["gender"] or ""),
            "patient_phone": patient_phone,
            "visit_at_default": _html_escape(_iso_now_minutes()),
            "visits_rows_html": visits_rows_html,
            "appointments_rows_html": appointments_rows_html,
            "whatsapp_button_html": (
                f"<a class='btn secondary btn-sm' target='_blank' rel='noopener noreferrer' href='{whatsapp_href}'>WhatsApp Patient</a>"
                if whatsapp_href else ""
            ),
            "appointment_at_default": _html_escape(_iso_now_minutes()),
            "invoice_day_default": _html_escape(_now_local().date().isoformat()),
            "invoice_rows_html": invoice_rows_html,
        },
    )
    return _layout(f"Patient {p['patient_code']}", body, flash=flash)


#
# Visits are edited by adding a new visit entry.
# Keeping the UI simple (no visit edit screen).
#


def page_staff_list(q: dict | None = None, flash: str = "") -> str:
    q = q or {}
    search = (q.get("q") or "").strip()
    with _db_lock:
        conn = db_connect()
        try:
            totals = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_staff,
                  SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS inactive_staff
                FROM staff
                """
            ).fetchone()
            if search:
                staff = conn.execute(
                    """
                    SELECT * FROM staff
                    WHERE name LIKE ? OR phone LIKE ? OR role LIKE ?
                    ORDER BY active DESC, name COLLATE NOCASE ASC
                    LIMIT 200
                    """,
                    (f"%{search}%", f"%{search}%", f"%{search}%"),
                ).fetchall()
            else:
                staff = conn.execute(
                    "SELECT * FROM staff ORDER BY active DESC, name COLLATE NOCASE ASC LIMIT 200"
                ).fetchall()
        finally:
            conn.close()
    trs = []
    color_i = 0
    for s in staff:
        phone = _html_escape(s["phone"] or "")
        status = "Active" if s["active"] else "Inactive"
        role = _html_escape(s["role"] or "Staff")
        phone_html = phone if phone else "<span class='muted'>No phone</span>"
        color_i = (color_i + 1) % 6
        acolor = f"a{color_i + 1}"
        photo_html = (
            _image_html(s["staff_image_path"] or "", f"{s['name']} photo", "staffavatar")
            if (s["staff_image_path"] or "").strip()
            else f"<span class='avatar {acolor}'>{_html_escape((s['name'] or 'S')[:1].upper())}</span>"
        )
        status_class = "pill goodpill" if s["active"] else "pill badpill"
        trs.append(
            f"<tr>"
            f"<td><div class='cellname'>{photo_html}<div><b>{_html_escape(s['name'])}</b></div></div></td>"
            f"<td>{phone_html}</td>"
            f"<td><span class='rolepill'>{role}</span></td>"
            f"<td><span class='{status_class}'>{status}</span></td>"
            f"<td class='right actionrow'><a class='iconbtn' href='/staff/{s['id']}' title='View'>"
            "<svg viewBox='0 0 24 24' fill='none'><path d='M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z' stroke='currentColor' stroke-width='1.8'/><circle cx='12' cy='12' r='3' stroke='currentColor' stroke-width='1.8'/></svg></a>"
            f"<a class='btn secondary btn-sm' href='/staff/{s['id']}/edit'>Edit</a></td>"
            "</tr>"
        )
    staff_rows_html = "".join(trs) if trs else "<tr><td colspan='5' class='muted'>No staff yet.</td></tr>"
    body = _render_template(
        "staff_list.html",
        {
            "staff_rows_html": staff_rows_html,
            "search": _html_escape(search),
            "active_staff_count": str(int(totals["active_staff"] or 0)),
            "inactive_staff_count": str(int(totals["inactive_staff"] or 0)),
        },
    )
    return _layout("Staff", body, flash=flash)


def page_staff_new(error: str = "") -> str:
    error_html = f"<div class='flash error'>{_html_escape(error)}</div>" if error else ""
    body = _render_template("staff_new.html", {"error_html": error_html})
    return _layout("New Staff", body)


def page_appointments(q: dict, flash: str = "") -> str:
    search = (q.get("q") or "").strip()
    status_filter = (q.get("status") or "Scheduled").strip()
    show_new = (q.get("new") or "").strip() == "1"
    with _db_lock:
        conn = db_connect()
        try:
            patients = conn.execute(
                "SELECT id, name, phone FROM patients ORDER BY name COLLATE NOCASE ASC LIMIT 300"
            ).fetchall()
            if search:
                rows = conn.execute(
                    """
                    SELECT a.*, p.name AS patient_name, p.phone AS patient_phone
                    FROM appointments a
                    JOIN patients p ON p.id = a.patient_id
                    WHERE (p.name LIKE ? OR p.phone LIKE ? OR COALESCE(a.doctor_name,'') LIKE ?)
                      AND (? = 'All' OR a.status = ?)
                    ORDER BY a.appointment_at ASC, a.id DESC
                    LIMIT 300
                    """,
                    (f"%{search}%", f"%{search}%", f"%{search}%", status_filter, status_filter),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT a.*, p.name AS patient_name, p.phone AS patient_phone
                    FROM appointments a
                    JOIN patients p ON p.id = a.patient_id
                    WHERE (? = 'All' OR a.status = ?)
                    ORDER BY a.appointment_at ASC, a.id DESC
                    LIMIT 300
                    """,
                    (status_filter, status_filter),
                ).fetchall()
            status_summary = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'Scheduled' THEN 1 ELSE 0 END) AS scheduled_count,
                  SUM(CASE WHEN status = 'Done' THEN 1 ELSE 0 END) AS done_count,
                  SUM(CASE WHEN status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_count
                FROM appointments
                """
            ).fetchone()
            settings = _get_settings(conn)
        finally:
            conn.close()
    patient_options_html = "".join(
        f"<option value='{int(p['id'])}'>{_html_escape(p['name'])} ({_html_escape(p['phone'] or 'No phone')})</option>"
        for p in patients
    )
    rows_html = []
    for a in rows:
        wa_link = _whatsapp_link(
            a["patient_phone"] or "",
            f"Hello {a['patient_name']}, your appointment is on {a['appointment_at']} at {settings.get('clinic_name', 'Dental Clinic')}.",
        )
        wa_btn = (
            f"<a class='iconbtn' target='_blank' rel='noopener noreferrer' title='WhatsApp' href='{wa_link}'>"
            "<svg viewBox='0 0 24 24' fill='none'><path d='M20 11.5A8.5 8.5 0 0 1 7.4 19l-3.4 1 1.1-3.2A8.5 8.5 0 1 1 20 11.5Z' stroke='currentColor' stroke-width='1.8' stroke-linejoin='round'/><path d='M9.5 8.8c.2-.4.4-.4.6-.4h.5c.2 0 .4 0 .5.4l.6 1.5c.1.2.1.4 0 .6l-.5.8c.4.8 1 1.4 1.8 1.8l.8-.5c.2-.1.4-.1.6 0l1.5.6c.4.2.4.3.4.5v.5c0 .2 0 .4-.4.6-.5.3-1 .4-1.6.3-2.7-.7-4.8-2.8-5.5-5.5-.1-.6 0-1.1.3-1.6Z' stroke='currentColor' stroke-width='1.4' stroke-linejoin='round'/></svg></a>"
            if wa_link else ""
        )
        rows_html.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(a['appointment_at'])}</td>"
            f"<td>{_html_escape(a['patient_name'])}</td>"
            f"<td>{_html_escape(a['doctor_name'] or '')}</td>"
            f"<td>{_html_escape(a['note'] or '')}</td>"
            f"<td><span class='pill'>{_html_escape(a['status'])}</span></td>"
            f"<td class='right actionrow'>{wa_btn}<a class='btn secondary btn-sm' href='/appointments/{int(a['id'])}/done'>Done</a></td>"
            "</tr>"
        )
    appointments_rows_html = "".join(rows_html) if rows_html else "<tr><td colspan='6' class='muted'>No appointments found.</td></tr>"
    body = _render_template(
        "appointments.html",
        {
            "search": _html_escape(search),
            "show_new_class": "" if show_new else "is-hidden",
            "hide_list_class": "is-hidden" if show_new else "",
            "status_all": "selected" if status_filter == "All" else "",
            "status_scheduled": "selected" if status_filter == "Scheduled" else "",
            "status_done": "selected" if status_filter == "Done" else "",
            "status_cancelled": "selected" if status_filter == "Cancelled" else "",
            "patient_options_html": patient_options_html,
            "appointment_at_default": _html_escape(_iso_now_minutes()),
            "appointments_rows_html": appointments_rows_html,
            "scheduled_count": str(int(status_summary["scheduled_count"] or 0)),
            "done_count": str(int(status_summary["done_count"] or 0)),
            "cancelled_count": str(int(status_summary["cancelled_count"] or 0)),
        },
    )
    return _layout("Appointments", body, flash=flash)


def page_staff_edit(staff_id: int, error: str = "", flash: str = "") -> str:
    with _db_lock:
        conn = db_connect()
        try:
            settings = _get_settings(conn)
            s = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        finally:
            conn.close()
    if not s:
        return _layout("Not Found", "<div class='card'><h1>Staff not found</h1></div>")
    error_html = f"<div class='flash error'>{_html_escape(error)}</div>" if error else ""
    active_yes = "selected" if int(s["active"]) else ""
    active_no = "selected" if not int(s["active"]) else ""
    body = _render_template(
        "staff_edit.html",
        {
            "error_html": error_html,
            "staff_id": str(staff_id),
            "name": _html_escape(s["name"]),
            "phone": _html_escape(s["phone"] or ""),
            "address": _html_escape(s["address"] or ""),
            "aadhar_no": _html_escape(s["aadhar_no"] or ""),
            "role": _html_escape(s["role"] or "Staff"),
            "staff_image_preview": _image_html(s["staff_image_path"] or "", f"{s['name']} photo"),
            "aadhar_image_preview": _image_html(s["aadhar_image_path"] or "", f"{s['name']} Aadhaar"),
            "active_yes": active_yes,
            "active_no": active_no,
        },
    )
    return _layout("Edit Staff", body, flash=flash)


def page_staff_view(staff_id: int) -> str:
    with _db_lock:
        conn = db_connect()
        try:
            s = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        finally:
            conn.close()
    if not s:
        return _layout("Not Found", "<div class='card'><h1>Staff not found</h1></div>")
    status = "Active" if s["active"] else "Inactive"
    body = _render_template(
        "staff_view.html",
        {
            "name": _html_escape(s["name"]),
            "phone": _html_escape(s["phone"] or ""),
            "address": _html_escape(s["address"] or ""),
            "aadhar_no": _html_escape(s["aadhar_no"] or ""),
            "role": _html_escape(s["role"] or "Staff"),
            "status": _html_escape(status),
            "staff_image_preview": _image_html(s["staff_image_path"] or "", f"{s['name']} photo", "previewimg"),
            "aadhar_image_preview": _image_html(s["aadhar_image_path"] or "", f"{s['name']} Aadhaar", "previewimg"),
            "staff_id": str(staff_id),
        },
    )
    return _layout("View Staff", body)


def _staff_access_rows_html(staff: list[sqlite3.Row], settings: dict[str, str]) -> str:
    role_options = _role_values(settings)
    rows = []
    for member in staff:
        options_html = "".join(
            f"<option value='{_html_escape(role)}'{' selected' if role == (member['role'] or 'Staff') else ''}>{_html_escape(role)}</option>"
            for role in role_options
        )
        rows.append(
            "<tr>"
            f"<td>{_html_escape(member['name'])}</td>"
            f"<td><select name='staff_role_{int(member['id'])}'>{options_html}</select></td>"
            f"<td><input type='password' name='staff_password_{int(member['id'])}' value='{_html_escape(member['password'] or '')}' placeholder='Set password' /></td>"
            "</tr>"
        )
    return "".join(rows) if rows else "<tr><td colspan='3' class='muted'>No staff added yet.</td></tr>"


def page_settings(flash: str = "") -> str:
    with _db_lock:
        conn = db_connect()
        try:
            settings = _get_settings(conn)
            settings["role_options"] = ",".join(_roles_from_staff(conn, active_only=True))
            staff = conn.execute(
                "SELECT id, name, role, password FROM staff WHERE active = 1 ORDER BY name COLLATE NOCASE ASC"
            ).fetchall()
        finally:
            conn.close()
    body = _render_template(
        "settings.html",
        {
            "clinic_name": _html_escape(settings.get("clinic_name", "Dental Clinic")),
            "clinic_tagline": _html_escape(settings.get("clinic_tagline", "Management System")),
            "admin_password": _html_escape(settings.get("admin_password", "admin123")),
            "theme_ocean": "selected" if settings.get("theme", "ocean") == "ocean" else "",
            "theme_sunrise": "selected" if settings.get("theme", "ocean") == "sunrise" else "",
            "theme_forest": "selected" if settings.get("theme", "ocean") == "forest" else "",
            "theme_dark": "selected" if settings.get("theme", "ocean") == "dark" else "",
            "clinic_phone": _html_escape(settings.get("clinic_phone", "")),
            "clinic_email": _html_escape(settings.get("clinic_email", "")),
            "clinic_address": _html_escape(settings.get("clinic_address", "")),
            "currency_symbol": _html_escape(settings.get("currency_symbol", "Rs.")),
            "appointment_minutes": _html_escape(settings.get("appointment_minutes", "30")),
            "role_permissions_table": _permissions_table_html(settings),
            "staff_access_rows_html": _staff_access_rows_html(staff, settings),
        },
    )
    return _layout("Settings", body, flash=flash)


def page_invoice_view(invoice_id: int, flash: str = "", print_mode: bool = False) -> str:
    with _db_lock:
        conn = db_connect()
        try:
            invoice = conn.execute(
                """
                SELECT b.*, p.name AS patient_name, p.patient_code, p.phone AS patient_phone
                FROM billing_invoices b
                JOIN patients p ON p.id = b.patient_id
                WHERE b.id = ?
                """,
                (invoice_id,),
            ).fetchone()
            settings = _get_settings(conn)
        finally:
            conn.close()
    if not invoice:
        return _layout("Not Found", "<div class='card'><h1>Invoice not found</h1></div>")
    amount = int(invoice["amount_paise"] or 0)
    paid = int(invoice["paid_amount_paise"] or 0)
    balance = amount - paid
    clinic_name = _html_escape(settings.get("clinic_name", "Dental Clinic"))
    clinic_phone = _html_escape(settings.get("clinic_phone", ""))
    clinic_email = _html_escape(settings.get("clinic_email", ""))
    clinic_address = _html_escape(settings.get("clinic_address", ""))
    invoice_time = _html_escape(_now_local().strftime("%d/%m/%Y, %I:%M %p"))
    status_class = "goodpill" if (invoice["status"] or "") == "Paid" else "badpill"
    body = f"""
    <div class='card invoice-card{' print-mode' if print_mode else ''}'>
      <div class='invoice-strip'>
        <div class='invoice-strip-item'>
          <div class='invoice-strip-label'>Printed On</div>
          <div>{invoice_time}</div>
        </div>
        <div class='invoice-strip-item invoice-strip-title'>Invoice</div>
        <div class='invoice-strip-item invoice-strip-status'><span class='pill {status_class}'>{_html_escape(invoice['status'])}</span></div>
      </div>
      <div class='headrow'>
        <div class='left'>
          <h1>{clinic_name}</h1>
          <div class='subhead'>{clinic_phone}</div>
          <div class='subhead'>{clinic_email}</div>
          <div class='subhead'>{clinic_address}</div>
        </div>
      </div>
      <div style='height:12px'></div>
      <div class='invoice-toolbar no-print'>
        <a class='btn secondary' href='/patients/{int(invoice["patient_id"])}'>Back To Patient</a>
        <button class='btn' type='button' onclick='window.print()'>Print PDF</button>
      </div>
      <div style='height:12px'></div>
      <div class='invoice-details-grid'>
        <div class='invoice-section'>
          <div class='invoice-section-title'>Clinic Details</div>
          <div class='invoice-detail-row'><span>Clinic Name</span><strong>{clinic_name}</strong></div>
          <div class='invoice-detail-row'><span>Phone</span><strong>{clinic_phone}</strong></div>
          <div class='invoice-detail-row'><span>Email</span><strong>{clinic_email}</strong></div>
          <div class='invoice-detail-row'><span>Address</span><strong>{clinic_address}</strong></div>
        </div>
        <div class='invoice-section'>
          <div class='invoice-section-title'>Patient Details</div>
          <div class='invoice-detail-row'><span>Patient Name</span><strong>{_html_escape(invoice['patient_name'])}</strong></div>
          <div class='invoice-detail-row'><span>Patient ID</span><strong>{_html_escape(invoice['patient_code'])}</strong></div>
          <div class='invoice-detail-row'><span>Phone</span><strong>{_html_escape(invoice['patient_phone'] or '')}</strong></div>
        </div>
      </div>
      <div style='height:12px'></div>
      <div class='invoice-section'>
        <div class='invoice-section-title'>Bill Details</div>
        <table class='invoice-table'>
          <thead>
            <tr>
              <th>Invoice No</th>
              <th>Invoice Date</th>
              <th>Treatment</th>
              <th>Note</th>
              <th class='right'>Total Amount</th>
              <th class='right'>Paid Amount</th>
              <th class='right'>Balance</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class='mono'><b>{_html_escape(invoice['invoice_no'])}</b></td>
              <td class='mono'>{_html_escape(invoice['invoice_day'])}</td>
              <td>{_html_escape(invoice['treatment_details'] or '')}</td>
              <td>{_html_escape(invoice['note'] or '')}</td>
              <td class='right mono'>{_html_escape(_paise_to_money(amount))}</td>
              <td class='right mono'>{_html_escape(_paise_to_money(paid))}</td>
              <td class='right mono'><b>{_html_escape(_paise_to_money(balance))}</b></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div style='height:12px'></div>
      <div class='invoice-footer'>
        <div class='subhead'>Authorized by {clinic_name}</div>
        <div class='subhead'>Thank you for visiting our clinic.</div>
      </div>
    </div>
    """
    return _layout("Invoice", body, flash=flash)


def page_reports(q: dict | None = None, flash: str = "") -> str:
    q = q or {}
    period = (q.get("period") or "monthly").strip().lower()
    source_filter = (q.get("source") or "all").strip().lower()
    anchor_text = (q.get("date") or _now_local().date().isoformat()).strip()
    try:
        anchor_day = _dt.date.fromisoformat(anchor_text)
    except ValueError:
        anchor_day = _now_local().date()
    if period == "today":
        start_day = anchor_day
        end_day = anchor_day + _dt.timedelta(days=1)
        label = "Today"
    elif period == "yesterday":
        start_day = anchor_day - _dt.timedelta(days=1)
        end_day = anchor_day
        label = "Yesterday"
    elif period == "weekly":
        start_day = anchor_day - _dt.timedelta(days=anchor_day.weekday())
        end_day = start_day + _dt.timedelta(days=7)
        label = "Weekly"
    elif period == "yearly":
        start_day = anchor_day.replace(month=1, day=1)
        end_day = _dt.date(anchor_day.year + 1, 1, 1)
        label = "Yearly"
    elif period == "all":
        start_day = _dt.date(2000, 1, 1)
        end_day = anchor_day + _dt.timedelta(days=1)
        label = "All Reports"
    else:
        period = "monthly"
        start_day = anchor_day.replace(day=1)
        if start_day.month == 12:
            end_day = _dt.date(start_day.year + 1, 1, 1)
        else:
            end_day = _dt.date(start_day.year, start_day.month + 1, 1)
        label = "Monthly"
    range_start = start_day.isoformat()
    range_end = end_day.isoformat()
    with _db_lock:
        conn = db_connect()
        try:
            staff_rows = conn.execute(
                """
                SELECT s.name,
                       COUNT(a.id) AS days_marked,
                       SUM(CASE WHEN a.status = 'Present' THEN 1 ELSE 0 END) AS days_present,
                       SUM(CASE WHEN a.status = 'Absent' THEN 1 ELSE 0 END) AS days_absent
                FROM staff s
                LEFT JOIN attendance a
                  ON a.staff_id = s.id AND a.day >= ? AND a.day < ?
                WHERE s.active = 1
                GROUP BY s.id, s.name
                ORDER BY s.name COLLATE NOCASE ASC
                """,
                (range_start, range_end),
            ).fetchall()
            expense_rows = conn.execute(
                """
                SELECT day, category AS label, amount_paise, note, 'Clinic' AS source
                FROM clinic_expenses
                WHERE day >= ? AND day < ?
                UNION ALL
                SELECT sa.day, s.name || ' - ' || sa.kind AS label, sa.amount_paise, sa.note, 'Staff' AS source
                FROM staff_adjustments sa
                JOIN staff s ON s.id = sa.staff_id
                WHERE sa.day >= ? AND sa.day < ?
                ORDER BY day DESC
                LIMIT 150
                """,
                (range_start, range_end, range_start, range_end),
            ).fetchall()
            appointment_rows = conn.execute(
                """
                SELECT a.appointment_at, a.status, a.doctor_name, a.note, p.name AS patient_name
                FROM appointments a
                JOIN patients p ON p.id = a.patient_id
                WHERE a.appointment_at >= ? AND a.appointment_at < ?
                ORDER BY a.appointment_at DESC, a.id DESC
                LIMIT 150
                """,
                (range_start, range_end),
            ).fetchall()
            billing_rows = conn.execute(
                """
                SELECT b.invoice_no, b.invoice_day, b.amount_paise, b.paid_amount_paise, b.status, p.name AS patient_name
                FROM billing_invoices b
                JOIN patients p ON p.id = b.patient_id
                WHERE b.invoice_day >= ? AND b.invoice_day < ?
                ORDER BY b.invoice_day DESC, b.id DESC
                LIMIT 150
                """,
                (range_start, range_end),
            ).fetchall()
            month_visits = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(cost_paise),0) AS total FROM visits WHERE visit_at >= ? AND visit_at < ?",
                (range_start, range_end),
            ).fetchone()
            month_clinic_exp = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS total FROM clinic_expenses WHERE day >= ? AND day < ?",
                (range_start, range_end),
            ).fetchone()
            month_staff_exp = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS total FROM staff_adjustments WHERE day >= ? AND day < ?",
                (range_start, range_end),
            ).fetchone()
            all_summary = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM patients) AS patients,
                  (SELECT COUNT(*) FROM visits) AS visits,
                  (SELECT COUNT(*) FROM staff WHERE active = 1) AS active_staff,
                  (SELECT COALESCE(SUM(cost_paise),0) FROM visits) AS income,
                  (SELECT COALESCE(SUM(amount_paise),0) FROM clinic_expenses) AS clinic_expenses,
                  (SELECT COALESCE(SUM(amount_paise),0) FROM staff_adjustments) AS staff_expenses,
                  (SELECT COALESCE(SUM(amount_paise),0) FROM billing_invoices) AS billed_total
                """
            ).fetchone()
        finally:
            conn.close()
    monthly_income = int(month_visits["total"])
    monthly_expenses = int(month_clinic_exp["total"]) + int(month_staff_exp["total"])
    all_expenses = int(all_summary["clinic_expenses"]) + int(all_summary["staff_expenses"])
    staff_report_rows_html = "".join(
        "<tr>"
        f"<td>{_html_escape(row['name'])}</td>"
        f"<td>{int(row['days_marked'] or 0)}</td>"
        f"<td>{int(row['days_present'] or 0)}</td>"
        f"<td>{int(row['days_absent'] or 0)}</td>"
        "</tr>"
        for row in staff_rows
    ) or "<tr><td colspan='4' class='muted'>No staff report rows for this range.</td></tr>"
    filtered_expense_rows = []
    for row in expense_rows:
        row_source = (row["source"] or "").lower()
        if source_filter != "all" and row_source != source_filter:
            continue
        filtered_expense_rows.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(row['day'])}</td>"
            f"<td>{_html_escape(row['source'])}</td>"
            f"<td>{_html_escape(row['label'])}</td>"
            f"<td>{_html_escape(row['note'] or '')}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(int(row['amount_paise'] or 0)))}</td>"
            "</tr>"
        )
    expense_report_rows_html = "".join(filtered_expense_rows) or "<tr><td colspan='5' class='muted'>No expenses for this filter.</td></tr>"
    appointment_report_rows_html = "".join(
        "<tr>"
        f"<td class='mono'>{_html_escape(row['appointment_at'])}</td>"
        f"<td>{_html_escape(row['patient_name'])}</td>"
        f"<td>{_html_escape(row['doctor_name'] or '')}</td>"
        f"<td>{_html_escape(row['note'] or '')}</td>"
        f"<td><span class='pill'>{_html_escape(row['status'] or '')}</span></td>"
        "</tr>"
        for row in appointment_rows
    ) or "<tr><td colspan='5' class='muted'>No appointments in this range.</td></tr>"
    billing_report_rows = []
    for row in billing_rows:
        balance = int(row["amount_paise"] or 0) - int(row["paid_amount_paise"] or 0)
        billing_report_rows.append(
            "<tr>"
            f"<td class='mono'>{_html_escape(row['invoice_no'])}</td>"
            f"<td class='mono'>{_html_escape(row['invoice_day'])}</td>"
            f"<td>{_html_escape(row['patient_name'])}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(int(row['amount_paise'] or 0)))}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(balance))}</td>"
            f"<td><span class='pill'>{_html_escape(row['status'])}</span></td>"
            "</tr>"
        )
    billing_report_rows_html = "".join(billing_report_rows) or "<tr><td colspan='6' class='muted'>No billing entries in this range.</td></tr>"
    body = f"""
    <div class='card'>
      <div class='headrow'>
        <div class='left'>
          <h1>Reports</h1>
          <div class='subhead'>{_html_escape(label)} business reporting with flexible filters</div>
        </div>
      </div>
      <div style='height:12px'></div>
      <form method='get' action='/reports' class='row'>
        <div style='flex:1 1 180px'>
          <label>Filter type</label>
          <select name='period'>
            <option value='today' {'selected' if period == 'today' else ''}>Today</option>
            <option value='yesterday' {'selected' if period == 'yesterday' else ''}>Yesterday</option>
            <option value='weekly' {'selected' if period == 'weekly' else ''}>Weekly</option>
            <option value='monthly' {'selected' if period == 'monthly' else ''}>Monthly</option>
            <option value='yearly' {'selected' if period == 'yearly' else ''}>Yearly</option>
            <option value='all' {'selected' if period == 'all' else ''}>All</option>
          </select>
        </div>
        <div style='flex:1 1 220px'>
          <label>Reference date</label>
          <input type='date' name='date' value='{_html_escape(anchor_day.isoformat())}' />
        </div>
        <div style='flex:1 1 180px'>
          <label>Expense source</label>
          <select name='source'>
            <option value='all' {'selected' if source_filter == 'all' else ''}>All expenses</option>
            <option value='clinic' {'selected' if source_filter == 'clinic' else ''}>Clinic expense</option>
            <option value='staff' {'selected' if source_filter == 'staff' else ''}>Staff expense</option>
          </select>
        </div>
        <div style='flex:1 1 180px'>
          <label>&nbsp;</label>
          <button class='btn secondary searchbtn' type='submit'>Apply Filter</button>
        </div>
      </form>
    </div>
    <div style='height:12px'></div>
    <div class='statgrid'>
      <div class='stat stat-a'><div><div class='stnum'>{int(month_visits['c'])}</div><div class='stlab'>{_html_escape(label)} Visits</div></div></div>
      <div class='stat stat-b'><div><div class='stnum mono'>{_html_escape(_paise_to_money(monthly_income))}</div><div class='stlab'>{_html_escape(label)} Income</div></div></div>
      <div class='stat stat-c'><div><div class='stnum mono'>{_html_escape(_paise_to_money(monthly_expenses))}</div><div class='stlab'>{_html_escape(label)} Expenses</div></div></div>
      <div class='stat stat-d'><div><div class='stnum mono'>{_html_escape(_paise_to_money(monthly_income - monthly_expenses))}</div><div class='stlab'>{_html_escape(label)} Balance</div></div></div>
    </div>
    <div style='height:12px'></div>
    <div class='grid two'>
      <div class='card'>
        <h1>All-Time Summary</h1>
        <table>
          <tbody>
            <tr><th>Total Patients</th><td>{int(all_summary['patients'])}</td></tr>
            <tr><th>Total Visits</th><td>{int(all_summary['visits'])}</td></tr>
            <tr><th>Active Staff</th><td>{int(all_summary['active_staff'])}</td></tr>
            <tr><th>Total Income</th><td class='mono'>{_html_escape(_paise_to_money(int(all_summary['income'])))}</td></tr>
            <tr><th>Total Billing</th><td class='mono'>{_html_escape(_paise_to_money(int(all_summary['billed_total'])))}</td></tr>
            <tr><th>Total Expenses</th><td class='mono'>{_html_escape(_paise_to_money(all_expenses))}</td></tr>
            <tr><th>Net Balance</th><td class='mono'>{_html_escape(_paise_to_money(int(all_summary['income']) - all_expenses))}</td></tr>
          </tbody>
        </table>
      </div>
      <div class='card'>
        <h1>Staff Attendance Report</h1>
        <div class='tablewrap'>
          <table>
            <thead><tr><th>Staff</th><th>Marked Days</th><th>Present</th><th>Absent</th></tr></thead>
            <tbody>{staff_report_rows_html}</tbody>
          </table>
        </div>
      </div>
    </div>
    <div style='height:12px'></div>
    <div class='card'>
      <h1>Expense Report</h1>
      <div class='tablewrap'>
        <table>
          <thead><tr><th>Date</th><th>Source</th><th>Label</th><th>Note</th><th class='right'>Amount</th></tr></thead>
          <tbody>{expense_report_rows_html}</tbody>
        </table>
      </div>
    </div>
    <div style='height:12px'></div>
    <div class='grid two'>
      <div class='card'>
        <h1>Appointment Report</h1>
        <div class='tablewrap'>
          <table>
            <thead><tr><th>Date &amp; Time</th><th>Patient</th><th>Doctor</th><th>Note</th><th>Status</th></tr></thead>
            <tbody>{appointment_report_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class='card'>
        <h1>Billing Report</h1>
        <div class='tablewrap'>
          <table>
            <thead><tr><th>Invoice</th><th>Date</th><th>Patient</th><th class='right'>Amount</th><th class='right'>Balance</th><th>Status</th></tr></thead>
            <tbody>{billing_report_rows_html}</tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return _layout("Reports", body, flash=flash)


def page_attendance(day: str, session: dict[str, str] | None = None, flash: str = "") -> str:
    day = (day or _now_local().date().isoformat()).strip()
    staff_filter = None
    if session and session.get("is_admin") != "1" and session.get("staff_id"):
        try:
            staff_filter = int((session.get("staff_id") or "0").strip() or "0")
        except ValueError:
            staff_filter = 0
    with _db_lock:
        conn = db_connect()
        try:
            if staff_filter:
                staff = conn.execute(
                    "SELECT * FROM staff WHERE active = 1 AND id = ? ORDER BY name COLLATE NOCASE ASC",
                    (staff_filter,),
                ).fetchall()
            else:
                staff = conn.execute(
                    "SELECT * FROM staff WHERE active = 1 ORDER BY name COLLATE NOCASE ASC"
                ).fetchall()
            existing = conn.execute(
                "SELECT * FROM attendance WHERE day = ?", (day,)
            ).fetchall()
        finally:
            conn.close()
    by_staff = {int(a["staff_id"]): a for a in existing}
    rows = []
    for s in staff:
        a = by_staff.get(int(s["id"]))
        status = (a["status"] if a else "Absent") or "Absent"
        cin = a["check_in"] if a else ""
        cout = a["check_out"] if a else ""
        rows.append(
            "<tr>"
            f"<td>{_html_escape(s['name'])}</td>"
            f"<td><select name='status_{s['id']}'><option {'selected' if status=='Present' else ''}>Present</option><option {'selected' if status=='Absent' else ''}>Absent</option></select></td>"
            f"<td><input type='text' name='check_in_{s['id']}' value='{_html_escape(cin)}' placeholder='HH:MM' /></td>"
            f"<td><input type='text' name='check_out_{s['id']}' value='{_html_escape(cout)}' placeholder='HH:MM' /></td>"
            "</tr>"
        )
    attendance_rows_html = "".join(rows) if rows else "<tr><td colspan='4' class='muted'>No active staff.</td></tr>"
    body = _render_template(
        "attendance.html",
        {"day": _html_escape(day), "attendance_rows_html": attendance_rows_html},
    )
    return _layout("Attendance", body, flash=flash)


def page_staff_adjustments(day: str, flash: str = "") -> str:
    day = (day or _now_local().date().isoformat()).strip()
    with _db_lock:
        conn = db_connect()
        try:
            staff = conn.execute(
                "SELECT * FROM staff ORDER BY active DESC, name COLLATE NOCASE ASC"
            ).fetchall()
            adj = conn.execute(
                """
                SELECT sa.*, s.name AS staff_name
                FROM staff_adjustments sa
                JOIN staff s ON s.id = sa.staff_id
                WHERE sa.day = ?
                ORDER BY sa.id DESC
                """,
                (day,),
            ).fetchall()
        finally:
            conn.close()
    staff_opts = "".join(
        f"<option value='{int(s['id'])}'>{_html_escape(s['name'])}</option>" for s in staff
    )
    trs = []
    for a in adj:
        trs.append(
            "<tr>"
            f"<td>{_html_escape(a['staff_name'])}</td>"
            f"<td>{_html_escape(a['kind'])}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(a['amount_paise']))}</td>"
            f"<td>{_html_escape(a['note'] or '')}</td>"
            f"<td class='right'><a class='btn danger' href='/staff-adjustments/{a['id']}/delete?day={_html_escape(day)}'>Delete</a></td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Staff</th><th>Type</th><th class='right'>Amount</th><th>Note</th><th class='right'>Action</th></tr></thead><tbody>"
        + ("".join(trs) if trs else "<tr><td colspan='5' class='muted'>No records for this day.</td></tr>")
        + "</tbody></table>"
    )
    body = f"""
      <div class="grid two">
        <div class="card">
          <h1>Staff Expenses / Bonus / Deductions</h1>
          <div class="muted">Date: {_html_escape(day)}</div>
        </div>
        <div class="card">
          <form method="get" action="/staff-adjustments">
            <label>Change day</label>
            <div class="row">
              <div><input type="date" name="day" value="{_html_escape(day)}" /></div>
              <div style="flex:1 1 180px"><button class="btn secondary" type="submit">Go</button></div>
            </div>
          </form>
        </div>
      </div>
      <div style="height:12px"></div>
      <div class="grid two">
        <div class="card">
          <h1>Add Entry</h1>
          <form method="post" action="/staff-adjustments/new?day={_html_escape(day)}">
            <div class="row">
              <div>
                <label>Staff Name</label>
                <select name="staff_id" required>{staff_opts}</select>
              </div>
              <div>
                <label>Type</label>
                <select name="kind">
                  <option>Expense</option>
                  <option>Bonus</option>
                  <option>Deduction</option>
                </select>
              </div>
            </div>
            <div class="row">
              <div>
                <label>Amount</label>
                <input type="text" name="amount" placeholder="e.g. 200" required />
              </div>
              <div>
                <label>Note</label>
                <input type="text" name="note" />
              </div>
              <div style="flex:1 1 180px">
                <label>&nbsp;</label>
                <button class="btn good" type="submit">Add</button>
              </div>
            </div>
          </form>
        </div>
        <div class="card">
          <h1>Entries</h1>
          {table}
        </div>
      </div>
    """
    return _layout("Staff Adjustments", body, flash=flash)

def page_expenses(day: str, flash: str = "") -> str:
    day = (day or _now_local().date().isoformat()).strip()
    with _db_lock:
        conn = db_connect()
        try:
            staff = conn.execute(
                "SELECT * FROM staff WHERE active = 1 ORDER BY name COLLATE NOCASE ASC"
            ).fetchall()
            staff_items = conn.execute(
                """
                SELECT sa.*, s.name AS staff_name
                FROM staff_adjustments sa
                JOIN staff s ON s.id = sa.staff_id
                WHERE sa.day = ? AND sa.kind = 'Expense'
                ORDER BY sa.id DESC
                """,
                (day,),
            ).fetchall()
            clinic_items = conn.execute(
                "SELECT * FROM clinic_expenses WHERE day = ? ORDER BY id DESC",
                (day,),
            ).fetchall()
            staff_total_row = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS total FROM staff_adjustments WHERE day = ? AND kind = 'Expense'",
                (day,),
            ).fetchone()
            clinic_total_row = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) AS total FROM clinic_expenses WHERE day = ?",
                (day,),
            ).fetchone()
        finally:
            conn.close()

    staff_opts = "".join(
        f"<option value='{int(s['id'])}'>{_html_escape(s['name'])}</option>" for s in staff
    )
    s_trs = []
    for e in staff_items:
        s_trs.append(
            "<tr>"
            f"<td>{_html_escape(e['staff_name'])}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(e['amount_paise']))}</td>"
            f"<td>{_html_escape(e['note'] or '')}</td>"
            f"<td class='right'><a class='btn danger' href='/staff-adjustments/{e['id']}/delete?day={_html_escape(day)}'>Delete</a></td>"
            "</tr>"
        )
    s_table = (
        "<table><thead><tr><th>Staff</th><th class='right'>Amount</th><th>Note</th><th class='right'>Action</th></tr></thead><tbody>"
        + ("".join(s_trs) if s_trs else "<tr><td colspan='4' class='muted'>No staff expenses for this day.</td></tr>")
        + "</tbody></table>"
    )
    c_trs = []
    for e in clinic_items:
        c_trs.append(
            "<tr>"
            f"<td>{_html_escape(e['category'])}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(e['amount_paise']))}</td>"
            f"<td>{_html_escape(e['note'] or '')}</td>"
            f"<td class='right'><a class='btn danger' href='/clinic-expenses/{e['id']}/delete?day={_html_escape(day)}'>Delete</a></td>"
            "</tr>"
        )
    c_table = (
        "<table><thead><tr><th>Clinic Category</th><th class='right'>Amount</th><th>Note</th><th class='right'>Action</th></tr></thead><tbody>"
        + ("".join(c_trs) if c_trs else "<tr><td colspan='4' class='muted'>No clinic expenses for this day.</td></tr>")
        + "</tbody></table>"
    )
    staff_expense_rows_html = "".join(s_trs) if s_trs else "<tr><td colspan='4' class='muted'>No staff expenses for this day.</td></tr>"
    clinic_expense_rows_html = "".join(c_trs) if c_trs else "<tr><td colspan='4' class='muted'>No clinic expenses for this day.</td></tr>"
    body = _render_template(
        "expenses.html",
        {
            "day": _html_escape(day),
            "staff_options_html": staff_opts,
            "staff_expense_rows_html": staff_expense_rows_html,
            "clinic_expense_rows_html": clinic_expense_rows_html,
            "staff_expense_total": _html_escape(_paise_to_money(int(staff_total_row["total"] or 0))),
            "clinic_expense_total": _html_escape(_paise_to_money(int(clinic_total_row["total"] or 0))),
            "grand_expense_total": _html_escape(_paise_to_money(int(staff_total_row["total"] or 0) + int(clinic_total_row["total"] or 0))),
        },
    )
    return _layout("Expenses", body, flash=flash)


def page_clinic_expenses(day: str, flash: str = "") -> str:
    day = (day or _now_local().date().isoformat()).strip()
    with _db_lock:
        conn = db_connect()
        try:
            items = conn.execute(
                "SELECT * FROM clinic_expenses WHERE day = ? ORDER BY id DESC", (day,)
            ).fetchall()
        finally:
            conn.close()
    trs = []
    for e in items:
        trs.append(
            "<tr>"
            f"<td>{_html_escape(e['category'])}</td>"
            f"<td class='right mono'>{_html_escape(_paise_to_money(e['amount_paise']))}</td>"
            f"<td>{_html_escape(e['note'] or '')}</td>"
            f"<td class='right'><a class='btn danger' href='/clinic-expenses/{e['id']}/delete?day={_html_escape(day)}'>Delete</a></td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Category</th><th class='right'>Amount</th><th>Note</th><th class='right'>Action</th></tr></thead><tbody>"
        + ("".join(trs) if trs else "<tr><td colspan='4' class='muted'>No expenses for this day.</td></tr>")
        + "</tbody></table>"
    )
    body = f"""
      <div class="grid two">
        <div class="card">
          <h1>Clinic Expenses</h1>
          <div class="muted">Date: {_html_escape(day)}</div>
        </div>
        <div class="card">
          <form method="get" action="/clinic-expenses">
            <label>Change day</label>
            <div class="row">
              <div><input type="date" name="day" value="{_html_escape(day)}" /></div>
              <div style="flex:1 1 180px"><button class="btn secondary" type="submit">Go</button></div>
            </div>
          </form>
        </div>
      </div>
      <div style="height:12px"></div>
      <div class="grid two">
        <div class="card">
          <h1>Add Expense</h1>
          <form method="post" action="/clinic-expenses/new?day={_html_escape(day)}">
            <div class="row">
              <div>
                <label>Category</label>
                <select name="category">
                  <option>Equipment</option>
                  <option>Medicines</option>
                  <option>Rent</option>
                  <option>Electricity</option>
                  <option>Other</option>
                </select>
              </div>
              <div>
                <label>Amount</label>
                <input type="text" name="amount" placeholder="e.g. 1000" required />
              </div>
            </div>
            <div class="row">
              <div>
                <label>Note</label>
                <input type="text" name="note" />
              </div>
              <div style="flex:1 1 180px">
                <label>&nbsp;</label>
                <button class="btn good" type="submit">Add</button>
              </div>
            </div>
          </form>
        </div>
        <div class="card">
          <h1>Expenses</h1>
          {table}
        </div>
      </div>
    """
    return _layout("Clinic Expenses", body, flash=flash)


class Handler(BaseHTTPRequestHandler):
    server_version = "DentalOffline/1.0"

    def _send_html(self, html: str, status: int = 200) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect_with_cookie(self, location: str, cookie_header: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    def _require_login(self) -> dict[str, str] | None:
        return _read_session(self)

    def _module_guard(
        self,
        session: dict[str, str],
        settings: dict[str, str],
        module: str,
    ) -> bool:
        if _has_module_access(session, settings, module):
            return True
        self._send_html(_access_denied_page(), status=403)
        return False

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        content = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        if not mime:
            mime = "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        p = _path(self)
        q = _query_params(self)
        try:
            if p.startswith("/static/"):
                rel = p.removeprefix("/static/").lstrip("/").replace("\\", "/")
                target = (STATIC_DIR / rel).resolve()
                # Prevent path traversal
                if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                    self.send_response(HTTPStatus.FORBIDDEN)
                    self.end_headers()
                    return
                self._send_file(target)
                return
            if p.startswith("/uploads/"):
                rel = p.removeprefix("/uploads/").lstrip("/").replace("\\", "/")
                target = (UPLOADS_DIR / rel).resolve()
                if UPLOADS_DIR.resolve() not in target.parents and target != UPLOADS_DIR.resolve():
                    self.send_response(HTTPStatus.FORBIDDEN)
                    self.end_headers()
                    return
                self._send_file(target)
                return
            if p == "/login":
                if self._require_login():
                    session = self._require_login()
                    with _db_lock:
                        conn = db_connect()
                        try:
                            settings = _get_settings(conn)
                        finally:
                            conn.close()
                    _redirect(self, _default_landing_path(session, settings))
                else:
                    self._send_html(page_login())
                return
            if p == "/logout":
                _destroy_session(self)
                _request_ctx.user_name = "Admin"
                self._redirect_with_cookie("/login", "session_id=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                return
            session = self._require_login()
            if not session:
                _redirect(self, "/login")
                return
            with _db_lock:
                conn = db_connect()
                try:
                    settings = _get_settings(conn)
                    _apply_request_context(session, settings, conn)
                finally:
                    conn.close()
            if p == "/":
                if not _has_module_access(session, settings, "dashboard"):
                    _redirect(self, _default_landing_path(session, settings))
                    return
                self._send_html(page_dashboard())
                return
            if p == "/patients":
                if not self._module_guard(session, settings, "patients"):
                    return
                self._send_html(page_patients_list(q))
                return
            if p == "/appointments":
                if not self._module_guard(session, settings, "appointments"):
                    return
                self._send_html(page_appointments(q))
                return
            if p == "/patients/new":
                if not self._module_guard(session, settings, "patients"):
                    return
                self._send_html(page_patient_new())
                return
            if p.startswith("/appointments/") and p.endswith("/done"):
                if not self._module_guard(session, settings, "appointments"):
                    return
                aid = int(p.split("/")[2])
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute("UPDATE appointments SET status = 'Done' WHERE id = ?", (aid,))
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, "/appointments")
                return
            if p.startswith("/patients/") and p.count("/") == 2:
                if not self._module_guard(session, settings, "patients"):
                    return
                pid = int(p.split("/")[-1])
                self._send_html(page_patient_detail(pid))
                return
            if p.startswith("/invoices/") and p.count("/") == 2:
                if not self._module_guard(session, settings, "patients"):
                    return
                invoice_id = int(p.split("/")[-1])
                self._send_html(page_invoice_view(invoice_id, print_mode=(q.get("print") or "").strip() == "1"))
                return
            if p.startswith("/patients/") and p.endswith("/delete"):
                if not self._module_guard(session, settings, "patients"):
                    return
                if not _can_delete_records():
                    self._send_html(_access_denied_page(), status=403)
                    return
                pid = int(p.split("/")[2])
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute("DELETE FROM patients WHERE id = ?", (pid,))
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, "/patients")
                return
            if p == "/staff":
                if not self._module_guard(session, settings, "staff"):
                    return
                self._send_html(page_staff_list(q))
                return
            if p == "/settings":
                if not self._module_guard(session, settings, "settings"):
                    return
                self._send_html(page_settings())
                return
            if p == "/staff/new":
                if not self._module_guard(session, settings, "staff"):
                    return
                self._send_html(page_staff_new())
                return
            if p == "/reports":
                if not self._module_guard(session, settings, "reports"):
                    return
                self._send_html(page_reports(q))
                return
            if p.startswith("/staff/") and p.count("/") == 2:
                if not self._module_guard(session, settings, "staff"):
                    return
                sid = int(p.split("/")[2])
                self._send_html(page_staff_view(sid))
                return
            if p.startswith("/staff/") and p.endswith("/edit"):
                if not self._module_guard(session, settings, "staff"):
                    return
                sid = int(p.split("/")[2])
                self._send_html(page_staff_edit(sid))
                return
            if p == "/attendance":
                if not self._module_guard(session, settings, "attendance"):
                    return
                self._send_html(page_attendance(q.get("day") or "", session))
                return
            if p == "/expenses":
                if not self._module_guard(session, settings, "expenses"):
                    return
                self._send_html(page_expenses(q.get("day") or ""))
                return
            if p == "/staff-adjustments":
                if not self._module_guard(session, settings, "expenses"):
                    return
                self._send_html(page_staff_adjustments(q.get("day") or ""))
                return
            if p.startswith("/staff-adjustments/") and p.endswith("/delete"):
                if not self._module_guard(session, settings, "expenses"):
                    return
                adj_id = int(p.split("/")[2])
                day = q.get("day") or _now_local().date().isoformat()
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute("DELETE FROM staff_adjustments WHERE id = ?", (adj_id,))
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/staff-adjustments?day={day}")
                return
            if p == "/clinic-expenses":
                if not self._module_guard(session, settings, "expenses"):
                    return
                self._send_html(page_clinic_expenses(q.get("day") or ""))
                return
            if p.startswith("/clinic-expenses/") and p.endswith("/delete"):
                if not self._module_guard(session, settings, "expenses"):
                    return
                exp_id = int(p.split("/")[2])
                day = q.get("day") or _now_local().date().isoformat()
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute("DELETE FROM clinic_expenses WHERE id = ?", (exp_id,))
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/expenses?day={day}")
                return
        except Exception as e:
            self._send_html(_layout("Error", f"<div class='card'><h1>Error</h1><div class='muted mono'>{_html_escape(e)}</div></div>"), status=500)
            return

        self._send_html(_layout("Not Found", "<div class='card'><h1>404 Not Found</h1></div>"), status=404)

    def do_POST(self) -> None:
        p = _path(self)
        q = _query_params(self)
        form = _read_form(self)
        try:
            if p == "/login":
                username = (form.get("username") or "").strip()
                password = (form.get("password") or "").strip()
                if not username or not password:
                    self._send_html(page_login("Enter username and password."))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        settings = _get_settings(conn)
                        staff = conn.execute(
                            "SELECT * FROM staff WHERE lower(name) = lower(?) AND active = 1",
                            (username,),
                        ).fetchone()
                    finally:
                        conn.close()
                if username.lower() == "admin":
                    if password != settings.get("admin_password", "admin123"):
                        self._send_html(page_login("Invalid login details."))
                        return
                    session_data = {"user_name": "Admin", "user_role": "Admin", "is_admin": "1"}
                else:
                    if not staff or (staff["password"] or "") != password:
                        self._send_html(page_login("Invalid login details."))
                        return
                    session_data = {
                        "user_name": str(staff["name"]),
                        "user_role": str(staff["role"] or "Staff"),
                        "staff_id": str(staff["id"]),
                        "is_admin": "0",
                    }
                session_id = _create_session(session_data)
                _request_ctx.user_name = session_data["user_name"]
                self._redirect_with_cookie(_default_landing_path(session_data, settings), f"session_id={session_id}; Path=/; HttpOnly; SameSite=Lax")
                return

            session = self._require_login()
            if not session:
                _redirect(self, "/login")
                return
            with _db_lock:
                conn = db_connect()
                try:
                    settings = _get_settings(conn)
                    _apply_request_context(session, settings, conn)
                finally:
                    conn.close()
            if p == "/patients/new":
                if not self._module_guard(session, settings, "patients"):
                    return
                name = (form.get("name") or "").strip()
                age_raw = (form.get("age") or "").strip()
                gender = (form.get("gender") or "").strip()
                phone = (form.get("phone") or "").strip()
                if not name:
                    self._send_html(page_patient_new("Patient name is required."))
                    return
                age = None
                if age_raw:
                    try:
                        age = int(age_raw)
                    except ValueError:
                        self._send_html(page_patient_new("Age must be a number."))
                        return
                with _db_lock:
                    conn = db_connect()
                    try:
                        code = _next_patient_code(conn)
                        conn.execute(
                            "INSERT INTO patients(patient_code, name, age, gender, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (code, name, age, gender, phone, _iso_now_minutes()),
                        )
                        pid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/patients/{pid}")
                return

            if p == "/appointments/new":
                if not self._module_guard(session, settings, "appointments"):
                    return
                patient_id = int((form.get("patient_id") or "0").strip() or "0")
                appointment_at = (form.get("appointment_at") or "").strip()
                doctor_name = (form.get("doctor_name") or "").strip()
                note = (form.get("note") or "").strip()
                status = (form.get("status") or "Scheduled").strip() or "Scheduled"
                if not patient_id or not appointment_at:
                    self._send_html(page_appointments(q, flash="Patient and appointment date are required."))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO appointments(patient_id, appointment_at, doctor_name, note, status, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (patient_id, appointment_at, doctor_name, note, status, _iso_now_minutes()),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                source_patient_id = (form.get("source_patient_id") or "").strip()
                if source_patient_id:
                    _redirect(self, f"/patients/{int(source_patient_id)}")
                else:
                    _redirect(self, "/appointments")
                return

            if p.startswith("/patients/") and p.endswith("/visits/new"):
                if not self._module_guard(session, settings, "patients"):
                    return
                pid = int(p.split("/")[2])
                visit_at = (form.get("visit_at") or "").strip()
                doctor = (form.get("doctor_assigned") or "").strip()
                treatment = (form.get("treatment_details") or "").strip()
                notes = (form.get("notes") or "").strip()
                cost_raw = (form.get("cost") or "").strip()
                if not visit_at:
                    _redirect(self, f"/patients/{pid}")
                    return
                try:
                    cost_paise = _money_to_paise(cost_raw)
                except ValueError:
                    self._send_html(page_patient_detail(pid, flash="Invalid cost value. Use numbers like 500 or 500.00"))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO visits(patient_id, visit_at, doctor_assigned, treatment_details, notes, cost_paise, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (pid, visit_at, doctor, treatment, notes, cost_paise, _iso_now_minutes()),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/patients/{pid}")
                return

            if p.startswith("/patients/") and p.endswith("/billing/new"):
                if not self._module_guard(session, settings, "patients"):
                    return
                pid = int(p.split("/")[2])
                invoice_day = (form.get("invoice_day") or _now_local().date().isoformat()).strip()
                treatment_details = (form.get("treatment_details") or "").strip()
                amount_raw = (form.get("amount") or "").strip()
                paid_raw = (form.get("paid_amount") or "").strip()
                note = (form.get("note") or "").strip()
                try:
                    amount_paise = _money_to_paise(amount_raw)
                    paid_amount_paise = _money_to_paise(paid_raw)
                except ValueError:
                    self._send_html(page_patient_detail(pid, flash="Invalid billing amount."))
                    return
                status = "Paid" if paid_amount_paise >= amount_paise and amount_paise > 0 else "Pending"
                with _db_lock:
                    conn = db_connect()
                    try:
                        invoice_no = _next_invoice_no(conn)
                        conn.execute(
                            """
                            INSERT INTO billing_invoices(patient_id, invoice_no, invoice_day, treatment_details, amount_paise, paid_amount_paise, status, note, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (pid, invoice_no, invoice_day, treatment_details, amount_paise, paid_amount_paise, status, note, _iso_now_minutes()),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/patients/{pid}")
                return

            if p == "/staff/new":
                if not self._module_guard(session, settings, "staff"):
                    return
                name = (form.get("name") or "").strip()
                phone = (form.get("phone") or "").strip()
                address = (form.get("address") or "").strip()
                aadhar_no = (form.get("aadhar_no") or "").strip()
                role = (form.get("role") or "").strip() or "Staff"
                if not name:
                    self._send_html(page_staff_new("Staff name is required."))
                    return
                staff_image_path = _save_uploaded_file(form.get("staff_image"), "staff_photo")
                aadhar_image_path = _save_uploaded_file(form.get("aadhar_image"), "aadhar")
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO staff(name, phone, address, aadhar_no, staff_image_path, aadhar_image_path, role, password, monthly_salary_paise, active, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (name, phone, address, aadhar_no, staff_image_path, aadhar_image_path, role, "", 0, _iso_now_minutes()),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        self._send_html(page_staff_new("Staff name already exists."))
                        return
                    finally:
                        conn.close()
                _redirect(self, "/staff")
                return

            if p.startswith("/staff/") and p.endswith("/edit"):
                if not self._module_guard(session, settings, "staff"):
                    return
                sid = int(p.split("/")[2])
                name = (form.get("name") or "").strip()
                phone = (form.get("phone") or "").strip()
                address = (form.get("address") or "").strip()
                aadhar_no = (form.get("aadhar_no") or "").strip()
                role = (form.get("role") or "").strip() or "Staff"
                active = 1 if (form.get("active") or "1").strip() == "1" else 0
                if not name:
                    self._send_html(page_staff_edit(sid, error="Staff name is required."))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        current = conn.execute("SELECT staff_image_path, aadhar_image_path FROM staff WHERE id = ?", (sid,)).fetchone()
                        staff_image_path = _save_uploaded_file(form.get("staff_image"), "staff_photo") or (current["staff_image_path"] if current else "")
                        aadhar_image_path = _save_uploaded_file(form.get("aadhar_image"), "aadhar") or (current["aadhar_image_path"] if current else "")
                        conn.execute(
                            """
                            UPDATE staff
                            SET name = ?, phone = ?, address = ?, aadhar_no = ?, staff_image_path = ?, aadhar_image_path = ?, role = ?, active = ?
                            WHERE id = ?
                            """,
                            (name, phone, address, aadhar_no, staff_image_path, aadhar_image_path, role, active, sid),
                        )
                        conn.commit()
                    except sqlite3.IntegrityError:
                        self._send_html(page_staff_edit(sid, error="Staff name already exists."))
                        return
                    finally:
                        conn.close()
                _redirect(self, "/staff")
                return

            if p == "/settings":
                if not self._module_guard(session, settings, "settings"):
                    return
                clinic_name = (form.get("clinic_name") or "").strip() or "Dental Clinic"
                clinic_tagline = (form.get("clinic_tagline") or "").strip() or "Management System"
                admin_password = (form.get("admin_password") or "").strip() or "admin123"
                theme = (form.get("theme") or "").strip() or "ocean"
                clinic_phone = (form.get("clinic_phone") or "").strip()
                clinic_email = (form.get("clinic_email") or "").strip()
                clinic_address = (form.get("clinic_address") or "").strip()
                currency_symbol = (form.get("currency_symbol") or "").strip() or "Rs."
                appointment_minutes = (form.get("appointment_minutes") or "").strip() or "30"
                with _db_lock:
                    conn = db_connect()
                    try:
                        role_names = _roles_from_staff(conn, active_only=True)
                        role_permissions = {}
                        for role in role_names:
                            allowed = [module for module in MODULES if (form.get(f"perm__{role}__{module}") or "").strip() == "1"]
                            role_permissions[role] = allowed or list(DEFAULT_ROLE_PERMISSIONS.get(role, ["dashboard"]))
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('clinic_name', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (clinic_name,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('clinic_tagline', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (clinic_tagline,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('admin_password', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (admin_password,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('theme', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (theme,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('role_options', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (",".join(role_names),),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('clinic_phone', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (clinic_phone,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('clinic_email', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (clinic_email,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('clinic_address', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (clinic_address,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('currency_symbol', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (currency_symbol,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('appointment_minutes', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (appointment_minutes,),
                        )
                        conn.execute(
                            "INSERT INTO app_settings(key, value) VALUES ('role_permissions', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (json.dumps(role_permissions),),
                        )
                        staff_rows = conn.execute("SELECT id FROM staff").fetchall()
                        for row in staff_rows:
                            sid = int(row["id"])
                            staff_role = (form.get(f"staff_role_{sid}") or "").strip() or "Staff"
                            if staff_role not in role_names:
                                staff_role = "Staff"
                            staff_password = (form.get(f"staff_password_{sid}") or "").strip()
                            conn.execute(
                                "UPDATE staff SET role = ?, password = ? WHERE id = ?",
                                (staff_role, staff_password, sid),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                _request_ctx.clinic_name = clinic_name
                _request_ctx.clinic_tagline = clinic_tagline
                _request_ctx.theme = theme
                self._send_html(page_settings(flash="Settings saved successfully."))
                return

            if p == "/attendance":
                if not self._module_guard(session, settings, "attendance"):
                    return
                day = (q.get("day") or _now_local().date().isoformat()).strip()
                with _db_lock:
                    conn = db_connect()
                    try:
                        if session.get("is_admin") != "1" and session.get("staff_id"):
                            staff = conn.execute(
                                "SELECT id FROM staff WHERE active = 1 AND id = ?",
                                (int((session.get("staff_id") or "0").strip() or "0"),),
                            ).fetchall()
                        else:
                            staff = conn.execute(
                                "SELECT id FROM staff WHERE active = 1"
                            ).fetchall()
                        for s in staff:
                            sid = int(s["id"])
                            status = (form.get(f"status_{sid}") or "Absent").strip()
                            cin = (form.get(f"check_in_{sid}") or "").strip() or None
                            cout = (form.get(f"check_out_{sid}") or "").strip() or None
                            conn.execute(
                                """
                                INSERT INTO attendance(staff_id, day, status, check_in, check_out, created_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(staff_id, day) DO UPDATE SET
                                  status=excluded.status,
                                  check_in=excluded.check_in,
                                  check_out=excluded.check_out
                                """,
                                (sid, day, status, cin, cout, _iso_now_minutes()),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/attendance?day={day}")
                return

            if p == "/staff-adjustments/new":
                if not self._module_guard(session, settings, "expenses"):
                    return
                day = (q.get("day") or _now_local().date().isoformat()).strip()
                staff_id = int((form.get("staff_id") or "0").strip() or "0")
                kind = (form.get("kind") or "Expense").strip()
                amount_raw = (form.get("amount") or "").strip()
                note = (form.get("note") or "").strip()
                try:
                    amount_paise = _money_to_paise(amount_raw)
                except ValueError:
                    self._send_html(page_staff_adjustments(day, flash="Invalid amount value."))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO staff_adjustments(staff_id, day, kind, amount_paise, note, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (staff_id, day, kind, amount_paise, note, _iso_now_minutes()),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                # If user used the unified Expenses page, send them back there.
                back = q.get("back") or ""
                if back == "expenses":
                    _redirect(self, f"/expenses?day={day}")
                else:
                    _redirect(self, f"/expenses?day={day}")
                return

            if p == "/clinic-expenses/new":
                if not self._module_guard(session, settings, "expenses"):
                    return
                day = (q.get("day") or _now_local().date().isoformat()).strip()
                category = (form.get("category") or "Other").strip()
                amount_raw = (form.get("amount") or "").strip()
                note = (form.get("note") or "").strip()
                try:
                    amount_paise = _money_to_paise(amount_raw)
                except ValueError:
                    self._send_html(page_clinic_expenses(day, flash="Invalid amount value."))
                    return
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO clinic_expenses(day, category, amount_paise, note, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (day, category, amount_paise, note, _iso_now_minutes()),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                _redirect(self, f"/expenses?day={day}")
                return
        except Exception as e:
            self._send_html(_layout("Error", f"<div class='card'><h1>Error</h1><div class='muted mono'>{_html_escape(e)}</div></div>"), status=500)
            return

        self._send_html(_layout("Not Found", "<div class='card'><h1>404 Not Found</h1></div>"), status=404)


def main() -> None:
    db_init()
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("DENTAL_HOST", "127.0.0.1")
    preferred_port = int(os.environ.get("DENTAL_PORT", "9000"))
    httpd, port = _bind_server(host, preferred_port)
    if port != preferred_port:
        print(
            f"Port {preferred_port} is busy, so Dental Clinic Offline started on http://{host}:{port}"
        )
    else:
        print(f"Dental Clinic Offline running on http://{host}:{port}")
    print(f"DB: {DB_PATH}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
