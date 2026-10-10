import sqlite3
import os
import json
import base64
import random
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Tuple, List, Dict, Any, Optional

def get_db_path() -> str:
    env_db = os.environ.get("DB_FILE")
    if env_db:
        return env_db
    local_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")
    try:
        # Test if current directory supports standard file locking (overlayfs, ext4, ntfs, etc.)
        test_conn = sqlite3.connect(local_db, timeout=2.0)
        test_conn.execute("CREATE TABLE IF NOT EXISTS _test_lock (id INT)")
        test_conn.execute("INSERT OR REPLACE INTO _test_lock VALUES (1)")
        test_conn.commit()
        test_conn.close()
        return local_db
    except Exception:
        # Fallback to temp directory if filesystem lacks POSIX advisory lock support (e.g. 9p, NFS)
        import tempfile
        return os.path.join(tempfile.gettempdir(), "attendance.db")

DB_FILE = get_db_path()

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=60.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 60000;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default

def set_setting(key: str, value: str):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value.strip()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SETTING ERROR]: {e}")

def generate_messaging_links(phone: str, message: str) -> dict:
    """Generates direct one-click WhatsApp and native mobile SMS links for instant delivery."""
    clean_phone = ''.join(c for c in phone if c.isdigit())
    encoded_msg = urllib.parse.quote(message)
    return {
        "whatsapp_url": f"https://wa.me/{clean_phone}?text={encoded_msg}",
        "sms_url": f"sms:{clean_phone}?body={encoded_msg}",
        "clean_phone": clean_phone
    }

def dispatch_carrier_sms(phone: str, message: str) -> bool:
    """
    Dispatches telecom SMS to mobile phones via Fast2SMS (India)
    or Twilio (Global) if API keys are configured in Settings or Environment.
    Non-blocking with strict 3-second timeout.
    """
    clean_phone = ''.join(c for c in phone if c.isdigit())
    if clean_phone.startswith('91') and len(clean_phone) == 12:
        clean_phone = clean_phone[2:]  # 10-digit format for Indian providers

    # 1. Fast2SMS Provider (Instant Indian Telecom SMS Gateway)
    fast2sms_key = get_setting("FAST2SMS_API_KEY", os.environ.get("FAST2SMS_API_KEY", "")).strip()
    if fast2sms_key:
        try:
            url = "https://www.fast2sms.com/dev/bulkV2"
            payload = json.dumps({
                "route": "q",
                "message": message,
                "language": "english",
                "flash": 0,
                "numbers": clean_phone
            }).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers={
                "authorization": fast2sms_key,
                "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req, timeout=3) as response:
                res_body = response.read().decode('utf-8')
                print(f"[FAST2SMS LIVE DISPATCH] Status: {response.status}, Body: {res_body}")
                return True
        except Exception as e:
            print(f"[FAST2SMS GATEWAY ERROR]: {e}")

    # 2. Twilio Provider (Global Cellular SMS Gateway)
    twilio_sid = get_setting("TWILIO_ACCOUNT_SID", os.environ.get("TWILIO_ACCOUNT_SID", "")).strip()
    twilio_token = get_setting("TWILIO_AUTH_TOKEN", os.environ.get("TWILIO_AUTH_TOKEN", "")).strip()
    twilio_from = get_setting("TWILIO_PHONE_NUMBER", os.environ.get("TWILIO_PHONE_NUMBER", "")).strip()
    if twilio_sid and twilio_token and twilio_from:
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
            to_number = phone if phone.startswith('+') else f"+91{clean_phone}"
            data = urllib.parse.urlencode({
                "To": to_number,
                "From": twilio_from,
                "Body": message
            }).encode('utf-8')
            auth_str = base64.b64encode(f"{twilio_sid}:{twilio_token}".encode('utf-8')).decode('utf-8').strip()
            req = urllib.request.Request(url, data=data, headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/x-www-form-urlencoded"
            })
            with urllib.request.urlopen(req, timeout=3) as response:
                print(f"[TWILIO LIVE DISPATCH] Status: {response.status}")
                return True
        except Exception as e:
            print(f"[TWILIO GATEWAY ERROR]: {e}")

    return False

def send_sms(recipient_phone: str, recipient_name: str, recipient_role: str, message_type: str, message_body: str):
    """
    Orchestrates cellular SMS transmission:
    1. Attempts live telecom carrier delivery (Fast2SMS / Twilio) if configured
    2. Logs record in database for audit trail and 1-click native SMS / WhatsApp dispatch
    """
    carrier_dispatched = False
    try:
        carrier_dispatched = dispatch_carrier_sms(recipient_phone, message_body)
    except Exception as e:
        print(f"[SMS DISPATCH EXCEPTION]: {e}")

    status_label = "DELIVERED_CARRIER" if carrier_dispatched else "LOGGED_READY_TO_SEND"

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''INSERT INTO sms_logs 
                          (recipient_phone, recipient_name, recipient_role, message_type, message_body, dispatched_at, status)
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (recipient_phone, recipient_name, recipient_role, message_type, message_body, now_str, status_label))
        conn.commit()
        print(f"[SMS LOG] {recipient_phone} ({recipient_name}): {message_body} [Status: {status_label}]")
    except Exception as e:
        print(f"[SMS LOG ERROR]: {e}")
    finally:
        if conn:
            try: conn.close()
            except Exception: pass

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Users Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL,
                        role TEXT NOT NULL,
                        name TEXT NOT NULL,
                        email TEXT,
                        phone TEXT,
                        parent_phone TEXT,
                        reg_no TEXT UNIQUE,
                        department TEXT,
                        year TEXT,
                        class_name TEXT,
                        assigned_teacher_id INTEGER,
                        student_id INTEGER,
                        is_face_enrolled INTEGER DEFAULT 0,
                        face_embedding TEXT,
                        face_photo TEXT,
                        created_at TEXT)''')
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN face_embedding TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN face_photo TEXT")
    except Exception:
        pass

    # 2. Venues / Classrooms Table (Room-Level Geofences: 20-30m per room)
    cursor.execute('''CREATE TABLE IF NOT EXISTS venues (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        building TEXT NOT NULL,
                        room_no TEXT NOT NULL,
                        latitude REAL NOT NULL,
                        longitude REAL NOT NULL,
                        radius_meters REAL NOT NULL DEFAULT 30.0)''')

    # 3. Subject-Wise Class Scheduled Sessions Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_code TEXT UNIQUE NOT NULL,
                        teacher_id INTEGER NOT NULL,
                        teacher_name TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        department TEXT NOT NULL,
                        year TEXT NOT NULL,
                        class_name TEXT NOT NULL,
                        venue_name TEXT NOT NULL,
                        latitude REAL NOT NULL,
                        longitude REAL NOT NULL,
                        radius_meters REAL NOT NULL DEFAULT 30.0,
                        scheduled_start TEXT NOT NULL,
                        scheduled_end TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        geofence_status TEXT NOT NULL DEFAULT 'APPROVED')''')

    # 4. Geofence Location Change Requests (Requires Admin Approval)
    cursor.execute('''CREATE TABLE IF NOT EXISTS geofence_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        teacher_id INTEGER NOT NULL,
                        teacher_name TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        class_name TEXT NOT NULL,
                        old_venue_name TEXT,
                        new_venue_name TEXT NOT NULL,
                        new_latitude REAL NOT NULL,
                        new_longitude REAL NOT NULL,
                        new_radius_meters REAL NOT NULL DEFAULT 30.0,
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'PENDING',
                        requested_at TEXT NOT NULL,
                        reviewed_by TEXT,
                        reviewed_at TEXT)''')

    # 5. Dual-Factor Attendance Transaction Log Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS attendance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL,
                        student_id INTEGER NOT NULL,
                        student_name TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        date TEXT NOT NULL,
                        time TEXT NOT NULL,
                        distance_meters REAL,
                        face_distance REAL,
                        device_fingerprint TEXT,
                        status TEXT NOT NULL,
                        is_manual_override INTEGER DEFAULT 0,
                        override_by TEXT,
                        remarks TEXT)''')

    # 6. SMS Logs Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS sms_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipient_phone TEXT NOT NULL,
                        recipient_name TEXT NOT NULL,
                        recipient_role TEXT NOT NULL,
                        message_type TEXT NOT NULL,
                        message_body TEXT NOT NULL,
                        dispatched_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'SENT')''')

    # 7. Departments / Branches Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS departments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        code TEXT NOT NULL)''')

    # 8. Classes Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        department TEXT NOT NULL,
                        year TEXT NOT NULL)''')

    # 9. System & Gateway Settings Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT)''')

    conn.commit()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Seed Individual Classroom Geofences
    cursor.execute("SELECT COUNT(*) FROM venues")
    if cursor.fetchone()[0] == 0:
        default_venues = [
            ("Classroom 101 - CS Block", "CS Department Building", "Room 101", 17.385044, 78.486671, 25.0),
            ("Classroom 102 - CS Block", "CS Department Building", "Room 102", 17.385150, 78.486750, 25.0),
            ("Classroom 204 - EEE Wing", "Engineering Block B", "Room 204", 17.384210, 78.485300, 25.0),
            ("Advanced AI & Networks Lab 3", "Research Complex", "Lab 3", 17.386120, 78.487950, 30.0),
            ("Electronics Embedded Systems Lab", "Block C", "Lab 12", 17.384500, 78.485600, 25.0),
            ("Mechanical Workshop & CAD Lab", "Heavy Machinery Block", "Room 105", 17.383800, 78.484900, 30.0),
            ("Main University Seminar Hall", "Central Administrative Building", "Hall A", 17.385600, 78.486200, 35.0)
        ]
        cursor.executemany('''INSERT INTO venues (name, building, room_no, latitude, longitude, radius_meters) 
                              VALUES (?, ?, ?, ?, ?, ?)''', default_venues)
        conn.commit()

    # Seed Expanded Academic Branches / Departments (8 Major Disciplines)
    cursor.execute("SELECT COUNT(*) FROM departments")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT OR IGNORE INTO departments (name, code) VALUES (?, ?)", [
            ("Computer Science & Engineering", "CSE"),
            ("Electronics & Communication Engineering", "ECE"),
            ("Electrical & Electronics Engineering", "EEE"),
            ("Mechanical Engineering", "MECH"),
            ("Civil Engineering", "CIVIL"),
            ("Information Technology", "IT"),
            ("Artificial Intelligence & Data Science", "AI&DS"),
            ("Master of Business Administration", "MBA")
        ])
        conn.commit()

    # Seed Classes
    cursor.execute("SELECT COUNT(*) FROM classes")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT INTO classes (name, department, year) VALUES (?, ?, ?)", [
            ("CS-A", "Computer Science & Engineering", "3rd Year"),
            ("CS-B", "Computer Science & Engineering", "3rd Year"),
            ("ECE-A", "Electronics & Communication Engineering", "3rd Year"),
            ("EE-A", "Electrical & Electronics Engineering", "2nd Year"),
            ("MECH-A", "Mechanical Engineering", "3rd Year"),
            ("CIVIL-A", "Civil Engineering", "2nd Year"),
            ("AIDS-A", "Artificial Intelligence & Data Science", "1st Year"),
            ("MBA-A", "Master of Business Administration", "1st Year")
        ])
        conn.commit()

    # Seed baseline users
    def insert_user_if_missing(username, password, role, name, email, phone, parent_phone=None,
                               reg_no=None, department=None, year=None, class_name=None,
                               assigned_teacher_id=None, student_id=None):
        cursor.execute("SELECT id FROM users WHERE username=?", (username,))
        if not cursor.fetchone():
            cursor.execute('''INSERT INTO users 
                              (username, password, role, name, email, phone, parent_phone, reg_no, department, year, class_name, assigned_teacher_id, student_id, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (username, password, role, name, email, phone, parent_phone, reg_no, department, year, class_name, assigned_teacher_id, student_id, now_str))
            conn.commit()

    # Admin user: STRICTLY for website and institutional administration (NO class, NO year, NO mentor)
    insert_user_if_missing('admin', 'admin123', 'management', 'Dr. Arthur Vance', 'vance@institution.edu', '+91-9800000001',
                           department='Central Administration', year=None, class_name=None, assigned_teacher_id=None)

    insert_user_if_missing('teacher1', 'teacher123', 'teacher', 'Prof. Sarah Connor', 'connor@institution.edu', '+91-9800000010',
                           department='Computer Science & Engineering', year='3rd Year', class_name='CS-A')
    cursor.execute("SELECT id FROM users WHERE username='teacher1'")
    teacher1_id = cursor.fetchone()[0]

    insert_user_if_missing('teacher2', 'teacher123', 'teacher', 'Dr. Alan Turing', 'turing@institution.edu', '+91-9800000020',
                           department='Electrical & Electronics Engineering', year='2nd Year', class_name='EE-A')

    insert_user_if_missing('student1', 'student123', 'student', 'Alex Mercer', 'alex.mercer@student.edu', '+91-9876543210',
                           parent_phone='+91-9876543299', reg_no='REG2026-CS101',
                           department='Computer Science & Engineering', year='3rd Year', class_name='CS-A',
                           assigned_teacher_id=teacher1_id)
    cursor.execute("SELECT id FROM users WHERE username='student1'")
    alex_id = cursor.fetchone()[0]

    insert_user_if_missing('student2', 'student123', 'student', 'Emily Blunt', 'emily.blunt@student.edu', '+91-9876543211',
                           parent_phone='+91-9876543298', reg_no='REG2026-CS102',
                           department='Computer Science & Engineering', year='3rd Year', class_name='CS-A',
                           assigned_teacher_id=teacher1_id)

    insert_user_if_missing('parent1', 'parent123', 'parent', 'Robert Mercer', 'robert.mercer@gmail.com', '+91-9876543299',
                           student_id=alex_id)

    conn.close()

    # Auto-rehydrate from attendance_backup.json if present in repository
    backup_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance_backup.json")
    if os.path.exists(backup_file):
        try:
            with open(backup_file, "r") as f:
                json_content = f.read()
            if json_content.strip():
                import_data_json(json_content)
                print("[AUTO-RESTORE] Successfully synchronized database from attendance_backup.json.")
        except Exception as e:
            print(f"[AUTO-RESTORE NOTICE]: {e}")



def sync_backup_file():
    """Silently saves a local JSON snapshot so git commits always retain all registered data."""
    try:
        backup_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance_backup.json")
        json_str = export_data_json()
        with open(backup_file, "w") as f:
            f.write(json_str)
    except Exception as e:
        print(f"[SYNC NOTICE]: {e}")

def export_data_json() -> str:
    """Exports all database tables to a JSON payload for cloud backup and multi-device portability."""
    conn = get_db()
    cursor = conn.cursor()
    tables = ["users", "venues", "sessions", "geofence_requests", "attendance", "sms_logs", "departments", "classes", "settings"]
    data = {}
    for t in tables:
        try:
            cursor.execute(f"SELECT * FROM {t}")
            data[t] = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            data[t] = []
    conn.close()
    return json.dumps(data, indent=2)


def import_data_json(json_str: str) -> Tuple[bool, str]:
    """Restores institutional database from a JSON backup payload."""
    try:
        data = json.loads(json_str)
        conn = get_db()
        cursor = conn.cursor()
        for t, rows in data.items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join(["?"] * len(cols))
            col_names = ",".join(cols)
            for r in rows:
                vals = [r[c] for c in cols]
                cursor.execute(f"INSERT OR REPLACE INTO {t} ({col_names}) VALUES ({placeholders})", vals)
        conn.commit()
        conn.close()
        return True, "Database restored successfully."
    except Exception as e:
        return False, f"Restore failed: {str(e)}"

if __name__ == '__main__':
    init_db()
