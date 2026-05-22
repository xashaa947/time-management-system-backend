from flask import Flask, request, jsonify, url_for, session, redirect
from flask_cors import CORS
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv(override=True)
import sqlite3
try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
import json
from datetime import datetime, timedelta
import google.oauth2.credentials
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
import requests
import uuid

# Allow insecure transport for local development (OAUTH2 over HTTP)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
# Allow scope changes (e.g. if Google Calendar API is not granted)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

app = Flask(__name__)
app.secret_key = "ai_agent_secret_key_change_this"
CORS(app)

# Database Setup
DB_NAME = "schedule.db"

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgresql") and psycopg2:
        # PostgreSQL (Supabase)
        conn = psycopg2.connect(db_url, cursor_factory=DictCursor)
        return conn, True
    else:
        # Local SQLite
        conn = sqlite3.connect(DB_NAME, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn, False

def init_db():
    conn, is_pg = get_db_connection()
    c = conn.cursor()
    
    # Auto-increment differences
    id_type = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            username TEXT UNIQUE,
            password TEXT,
            role TEXT,
            google_token TEXT,
            code_verifier TEXT,
            email TEXT
        )
    ''')
    
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS tasks (
            id {id_type},
            user_id INTEGER,
            type TEXT,
            content TEXT,
            date TEXT,
            time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'approved',
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            google_event_id TEXT,
            group_id TEXT,
            meet_link TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    ''')
    
    # Existing tables migration (Postgres handles ALTER differently, but this is safe)
    if not is_pg:
        # SQLite migrations
        try: c.execute("ALTER TABLE tasks ADD COLUMN end_time TEXT")
        except: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'approved'")
        except: pass
        try: c.execute("ALTER TABLE users ADD COLUMN google_token TEXT")
        except: pass
        try: c.execute("ALTER TABLE users ADD COLUMN code_verifier TEXT")
        except: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN google_event_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        except: pass
        try: c.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN group_id TEXT")
        except: pass
        try: c.execute("ALTER TABLE tasks ADD COLUMN meet_link TEXT")
        except: pass
    
    # Initial admin setup
    try:
        c.execute("UPDATE users SET role = 'admin'")
    except:
        pass
        
    conn.commit()
    conn.close()

init_db()

def is_time_conflict(user_id, date, start_time, end_time, exclude_group_id=None):
    if not user_id or not date or not start_time:
        return False
        
    # Handle missing end_time by defaulting to 1 hour after start_time
    if not end_time:
        try:
            h, m = map(int, start_time.split(':'))
            end_time = f"{(h+1)%24:02d}:{m:02d}"
        except:
            end_time = start_time

    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()

    sql = f'''
        SELECT time, end_time FROM tasks
        WHERE user_id = {p} AND date = {p} AND status = 'approved'
    '''
    params = [user_id, date]
    
    if exclude_group_id:
        sql += f" AND (group_id IS NULL OR group_id != {p})"
        params.append(exclude_group_id)

    c.execute(sql, tuple(params))

    rows = c.fetchall()
    conn.close()

    try:
        new_start = datetime.strptime(start_time, "%H:%M")
        new_end = datetime.strptime(end_time, "%H:%M")
        
        # If end_time wrapped around (e.g. 23:00 to 00:00), we should handle it, 
        # but for now we assume same-day tasks. 
        if new_end <= new_start:
             new_end += timedelta(days=1)

        for row in rows:
            try:
                existing_time = row['time']
                existing_end_time = row['end_time']
            except:
                existing_time = row[0]
                existing_end_time = row[1]

            if not existing_time: continue
            existing_start = datetime.strptime(existing_time, "%H:%M")
            
            if existing_end_time:
                existing_end = datetime.strptime(existing_end_time, "%H:%M")
                if existing_end <= existing_start:
                    existing_end += timedelta(days=1)
            else:
                existing_end = existing_start + timedelta(hours=1)

            if (new_start < existing_end) and (new_end > existing_start):
                return True
    except Exception as e:
        print(f"Conflict check error: {e}")
        return False

    return False  # Давхцахгүй 

def save_task_to_db(task, user_id=None, created_by=None):
    if not user_id: return None
    
    # Skip conflict check for pending tasks (they are just requests)
    if task.get('status', 'approved') == 'approved':
        if is_time_conflict(user_id, task.get('date'), task.get('time'), task.get('end_time'), exclude_group_id=task.get('group_id')):
            print(f"⚠️ Давхцал илэрлээ! User: {user_id}, Date: {task.get('date')}, Time: {task.get('time')}")
            return None

    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    
    sql = f'''
        INSERT INTO tasks (user_id, type, content, date, time, end_time, status, title, created_by, group_id)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
    '''
    
    if is_pg:
        sql += " RETURNING id"
        c.execute(sql, (user_id, task.get('type'), task.get('content'), task.get('date'), task.get('time'), 
                      task.get('end_time'), task.get('status', 'approved'), task.get('title'), created_by, task.get('group_id')))
        task_id = c.fetchone()[0]
    else:
        c.execute(sql, (user_id, task.get('type'), task.get('content'), task.get('date'), task.get('time'), 
                      task.get('end_time'), task.get('status', 'approved'), task.get('title'), created_by, task.get('group_id')))
        task_id = c.lastrowid
        
    conn.commit()
    conn.close()
    
    # Try to sync to Google Calendar if task is approved
    if task.get('status', 'approved') == 'approved':
        try:
            task['created_by'] = created_by
            create_google_calendar_event(task, user_id, task_id)
        except Exception as e:
            print(f"Calendar Sync failed: {e}")
    return task_id



@app.route("/users", methods=["GET"])
def get_users():
    conn, is_pg = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, email FROM users")
    # Postgres DictCursor behaves like dict, SQLite Row needs dict() conversion
    rows = c.fetchall()
    users = [dict(row) for row in rows]
    conn.close()
    return jsonify(users)

def normalize_username(name):
    if not name: return ""
    return name.lower().replace("@", "").replace("-", "").replace("_", "").replace(" ", "")

def parse_time_from_text(text):
    """
    Хэрэглэгчийн текстээс цагийг regex-ээр задлан, 24 цагийн HH:MM форматаар буцаана.
    Зөвхөн 'орой', 'pm', 'үдээс хойш' гэж тодорхой хэлсэн бол PM болгоно,
    эс бол тоог шууд цаг болгоно (5 → 05:00, 13 → 13:00).
    Буцаах утга: (start_time, end_time) эсвэл (None, None)
    """
    import re
    text_lower = text.lower()

    # PM context keywords
    is_pm = any(w in text_lower for w in ['орой', 'pm', 'p.m', 'үдээс хойш', 'afternoon', 'evening'])
    is_am = any(w in text_lower for w in ['өглөө', 'am', 'a.m', 'үүрийн', 'шөнийн'])

    def to_24h(hour, minute=0):
        h = int(hour)
        m = int(minute)
        if is_pm and h < 12:
            h += 12
        elif is_am:
            pass  # keep as-is
        return f"{h:02d}:{m:02d}"

    # Pattern: H:MM-H:MM or H-H:MM or H-H
    range_pattern = re.search(r'(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?', text)
    if range_pattern:
        sh, sm, eh, em = range_pattern.groups()
        return to_24h(sh, sm or 0), to_24h(eh, em or 0)

    # Pattern: single time like "5 цагт"
    single_pattern = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(?:цагт|цаг|:00)?', text)
    if single_pattern:
        sh, sm = single_pattern.groups()
        start = to_24h(sh, sm or 0)
        h = int(sh)
        if is_pm and h < 12: h += 12
        end = f"{(h+1)%24:02d}:{int(sm or 0):02d}"
        return start, end

    return None, None

def get_mentioned_user_ids(user_text, db_cursor):
    """
    Хэрэглэгчийн текстээс @username-г олж, тухайн хэрэглэгчийн ID-г буцаана.
    """
    db_cursor.execute("SELECT id, username FROM users")
    all_users = db_cursor.fetchall()
    user_ids = {}
    normalized_text = normalize_username(user_text)
    
    for row in all_users:
        try:
            uid = row['id']; username = row['username']
        except:
            uid = row[0]; username = row[1]
            
        if normalize_username(username) in normalized_text:
            user_ids[uid] = username
    return user_ids 

def get_conflicting_tasks(user_id, date, start_time, end_time=None):
    """
    Хэрэглэгчийн тухайн огноо, цагт давхцсан бүх task-уудыг буцаана
    """
    if not user_id or not date or not start_time:
        return []

    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    c.execute(f"""
        SELECT title, content, time, end_time FROM tasks
        WHERE user_id = {p} AND date = {p} AND status = 'approved'
    """, (user_id, date))
    rows = c.fetchall()
    conn.close()

    if not end_time:
        try:
            h, m = map(int, start_time.split(":"))
            end_time = f"{(h+1)%24:02d}:{m:02d}"
        except:
            end_time = start_time

    try:
        new_start = datetime.strptime(start_time, "%H:%M")
        new_end = datetime.strptime(end_time, "%H:%M")
        if new_end <= new_start:
            new_end += timedelta(days=1)

        conflicts = []
        for row in rows:
            try:
                existing_time = row['time']
                existing_end_time = row['end_time']
                existing_title = row['title']
                existing_content = row['content']
            except:
                existing_time = row[2]
                existing_end_time = row[3]
                existing_title = row[0]
                existing_content = row[1]

            if not existing_time: continue
            existing_start = datetime.strptime(existing_time, "%H:%M")
            if existing_end_time:
                existing_end = datetime.strptime(existing_end_time, "%H:%M")
                if existing_end <= existing_start:
                    existing_end += timedelta(days=1)
            else:
                existing_end = existing_start + timedelta(hours=1)
                
            if (new_start < existing_end) and (new_end > existing_start):
                conflicts.append({"title": existing_title, "content": existing_content})
        return conflicts
    except Exception as e:
        print(f"DEBUG: get_conflicting_tasks error: {e}")
        return []
@app.route("/admin/add-task", methods=["POST"])
def admin_add_task():
    data = request.get_json()
    user_id = data.get("user_id")
    task_id = save_task_to_db(data, user_id, user_id)
    if not task_id:
        return jsonify({"success": False, "message": "Цаг давхцаж байна"}), 409
    return jsonify({"success": True, "message": "Task added successfully"})

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_all_tasks_as_string(user_id=None):
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    if user_id:
        c.execute(f'SELECT type, content, date, time, end_time, status, title FROM tasks WHERE user_id = {p} ORDER BY date, time', (user_id,))
    else:
        c.execute('SELECT type, content, date, time, end_time, status, title FROM tasks ORDER BY date, time')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return "Одоогоор ямар нэгэн ажил/хуваарь байхгүй байна."
    
    schedule_text = "Хэрэглэгчийн одоогийн хуваарь:\n"
    for row in rows:
        task = dict(row)
        time_str = f"{task['time']} - {task['end_time']}" if task.get('end_time') else task['time']
        status_str = f" [{task.get('status')}]" if task.get('status') != 'approved' else ""
        schedule_text += f"- {task['date']} {time_str}: {task['title']} ({task['content']}){status_str} [Type: {task['type']}]\n"
    return schedule_text

# System prompt (AI Agent-ийн дүрэм)
SYSTEM_PROMPT = """
Чи бол ухаалаг туслах AI Agent байна. Хэрэглэгчийн хүсэлтийг шинжлээд дараах JSON бүтцээр хариу өгнө үү:

{
  "summary": "Хэрэглэгчийн хүсэлтийн товч тайлбар",
  "tasks": [
    {
      "type": "ajlaar | choloot",
      "content": "Даалгаврын дэлгэрэнгүй",
      "date": "YYYY-MM-DD",
      "time": "HH:mm (эхлэх)",
      "end_time": "HH:mm (дуусах)",
      "title": "Гарчиг",
      "status": "pending | approved",
      "for_users": ["username1", "username2"],
      "is_online_meeting": false,
      "location": ""
    }
  ],
  "available": true,
  "need_location": false
}

**ДҮРЭМ:**
1. **ХОУМ (PARSING):** Хэрэглэгчийн хүсэлтээс ажил эсвэл уулзалтын мэдээллийг (нэр, огноо, цаг, төрөл) зөв салгаж авна.
2. **МЕНШИН (@):** Хэрэв текст дотор хүмүүсийн нэр (@username) байвал тэдгээрийг "for_users" жагсаалтад оруулна. 
3. **ТӨЛӨВ (STATUS):** Хэрэв "for_users" дотор хүмүүс байвал "status" нь заавал "pending" байх ёстой. Зөвхөн хэрэглэгч өөртөө ажил нэмж байгаа тохиолдолд "approved" байна.
4. **ХУВААРЬ:** Чи өөрөө хуваарь шалгах шаардлагагүй, зөвхөн хүсэлтийг JSON болгож хувиргахад анхаарна уу. 
5. **ХАРИУ:** Хэрэв мэдээлэл асуусан бол (жишээ нь "Өнөөдөр юу хийх вэ?") "tasks" массив хоосон байна.
6. **УУЛЗАЛТЫН ТӨРӨЛ:**
   - "цахим уулзалт", "онлайн уулзалт", "видео уулзалт", "zoom", "meet", "online meeting" гэх мэт үгс байвал → "is_online_meeting": true, "location": "" гэж тохируул.
   - "уулзалт" гэх мэт биечлэн уулзах утгатай үгс байвал → "is_online_meeting": false. Хэрэв байршил (хаана уулзах?) хэлэгдээгүй бол "need_location": true гэж тохируул.
   - Ердийн ажлын даалгавар бол "is_online_meeting": false, "need_location": false.
7. **Зөвхөн JSON.**
"""

# GOOGLE OAUTH CONFIG
CLIENT_SECRETS_FILE = "credentials.json"
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'openid'
]

def get_oauth_flow(scopes, redirect_uri=None):
    """Create OAuth flow from credentials.json (local) or env vars (Render)."""
    if os.path.exists(CLIENT_SECRETS_FILE):
        flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, scopes=scopes)
    else:
        client_config = {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs"
            }
        }
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config, scopes=scopes)
    if redirect_uri:
        flow.redirect_uri = redirect_uri
    return flow

def get_redirect_uri():
    backend_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BACKEND_URL")
    if backend_url:
        return f"{backend_url.rstrip('/')}/api/google/callback"
    return url_for('google_callback', _external=True)

@app.route('/api/google/login')
def google_login():
    flow = get_oauth_flow(SCOPES, redirect_uri=get_redirect_uri())
    print(f"DEBUG: [login] Redirect URI set to: {flow.redirect_uri}")
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent')
    
    # Store state and code verifier temporarily
    try:
        conn, is_pg = get_db_connection()
        p = "%s" if is_pg else "?"
        c = conn.cursor()
        if is_pg:
            c.execute("CREATE TABLE IF NOT EXISTS oauth_sessions (state TEXT PRIMARY KEY, code_verifier TEXT)")
            # INSERT OR REPLACE is SQLite specific. PG uses INSERT ... ON CONFLICT
            c.execute("INSERT INTO oauth_sessions (state, code_verifier) VALUES (%s, %s) ON CONFLICT (state) DO UPDATE SET code_verifier = EXCLUDED.code_verifier", (state, flow.code_verifier))
        else:
            c.execute("CREATE TABLE IF NOT EXISTS oauth_sessions (state TEXT PRIMARY KEY, code_verifier TEXT)")
            c.execute("INSERT OR REPLACE INTO oauth_sessions (state, code_verifier) VALUES (?, ?)", (state, flow.code_verifier))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DEBUG: [login] DB Error: {e}")
    
    return jsonify({"auth_url": authorization_url})

@app.route('/api/google/callback')
def google_callback():
    state = request.args.get('state')
    
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    c.execute(f"SELECT code_verifier FROM oauth_sessions WHERE state = {p}", (state,))
    row = c.fetchone()
    
    code_verifier = row[0] if row else None
    print(f"DEBUG: [callback] Retrieved code_verifier from DB: {code_verifier}")
    
    flow = get_oauth_flow(SCOPES, redirect_uri=get_redirect_uri())
    
    if code_verifier:
        flow.code_verifier = code_verifier
        print(f"DEBUG: [callback] Applied code_verifier to flow: {flow.code_verifier}")
    else:
        print("DEBUG: [callback] No code_verifier found for this state in DB!")
    
    authorization_response = request.url
    # Ensure it's http if developing locally, as Google might return https in some envs
    # ONLY replace it at the start of the string to avoid corrupting parameters like 'iss'
    if 'http://' in flow.redirect_uri and authorization_response.startswith('https://'):
        authorization_response = 'http://' + authorization_response[8:]

    print(f"DEBUG: [callback] Fetching token with response URL: {authorization_response}")
    
    if not code_verifier:
         return jsonify({"error": "OAuth state mismatch or expired. Please go back to the login page and try again."}), 400
    try:
        flow.fetch_token(authorization_response=authorization_response, code_verifier=code_verifier)
        if state:
            c.execute(f"DELETE FROM oauth_sessions WHERE state = {p}", (state,))
            conn.commit()
    except Exception as e:
        print(f"DEBUG: [callback] Token fetch failed: {e}")
        conn.close()
        return jsonify({"error": f"Token fetch failed: {str(e)}"}), 500
    
    credentials = flow.credentials
    creds_json = credentials.to_json()
    
    # Get user email
    try:
        user_info_service = build('oauth2', 'v2', credentials=credentials)
        user_info = user_info_service.userinfo().get().execute()
        user_email = user_info.get('email')
        user_name = user_info.get('name') or user_email.split('@')[0]
    except Exception as e:
        print(f"DEBUG: Could not fetch user email: {e}")
        user_email = None
        user_name = "Unknown User"

    if user_email:
        c.execute(f"SELECT * FROM users WHERE email = {p}", (user_email,))
        user = c.fetchone()
        if user:
            # handle dict access
            uid = user['id'] if hasattr(user, '__getitem__') else user[0]
            c.execute(f"UPDATE users SET google_token = {p}, username = {p} WHERE id = {p}", (creds_json, user_name, uid))
            conn.commit()
            user_id = uid
        else:
            c.execute(f"SELECT id FROM users WHERE username = {p}", (user_name,))
            if c.fetchone():
                import random
                user_name = f"{user_name}_{random.randint(100,999)}"
            
            sql_ins = f"INSERT INTO users (username, password, role, email, google_token) VALUES ({p}, {p}, {p}, {p}, {p})"
            if is_pg:
                c.execute(sql_ins + " RETURNING id", (user_name, '', 'admin', user_email, creds_json))
                user_id = c.fetchone()[0]
            else:
                c.execute(sql_ins, (user_name, '', 'admin', user_email, creds_json))
                user_id = c.lastrowid
            conn.commit()
            
        c.execute(f"SELECT * FROM users WHERE id = {p}", (user_id,))
        updated_user = dict(c.fetchone())
    conn.close()
    
    if user_email:
        import urllib.parse
        frontend_user = {
            "id": updated_user["id"],
            "username": updated_user["username"],
            "role": updated_user["role"],
            "email": updated_user["email"]
        }
        user_json = json.dumps(frontend_user)
        encoded_user = urllib.parse.quote(user_json)
        frontend_url = os.getenv("FRONTEND_URL", "https://time-management-system-five.vercel.app")
        return redirect(f"{frontend_url}/?auth_data={encoded_user}")
    else:
        return jsonify({"error": "Google login failed, no email found"})

def create_google_calendar_event(task, user_id, task_id=None, generate_meet=True):
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    c.execute(f"SELECT google_token FROM users WHERE id = {p}", (user_id,))
    row = c.fetchone()
    # Close for now, will reopen if refresh needed or at end
    
    if not row or not row['google_token']:
        conn.close()
        print(f"No google_token for user {user_id}")
        return None
        
    try:
        creds_info = json.loads(row['google_token'])
        credentials = google.oauth2.credentials.Credentials.from_authorized_user_info(creds_info)
        
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            c.execute(f"UPDATE users SET google_token = {p} WHERE id = {p}", (credentials.to_json(), user_id))
            conn.commit()
            
        service = build('calendar', 'v3', credentials=credentials)
        
        start_datetime = f"{task['date']}T{task['time']}:00"
        
        if not task.get('end_time') or not task['end_time']:
            try:
                h, m = map(int, task['time'].split(':'))
                end_time = f"{h+1:02d}:{m:02d}"
                end_datetime = f"{task['date']}T{end_time}:00"
            except:
                end_datetime = f"{task['date']}T{task['time']}:00"
        else:
            end_datetime = f"{task['date']}T{task['end_time']}:00"
            
        event = {
          'summary': task.get('title', 'AI Agent Task'),
          'description': task.get('content', ''),
          'start': {
            'dateTime': start_datetime,
            'timeZone': 'Asia/Ulaanbaatar',
          },
          'end': {
            'dateTime': end_datetime,
            'timeZone': 'Asia/Ulaanbaatar',
          },
        }
        
        # Check if this is an online meeting → add Google Meet link
        # Check if it's an in-person meeting → add location to calendar event
        is_online = task.get('is_online_meeting', False)
        location = task.get('location', '')
        title_lower = task.get('title', '').lower()
        type_lower = task.get('type', '').lower()
        is_any_meeting = is_online or 'уулзалт' in title_lower or 'уулзалт' in type_lower

        if is_online or (is_any_meeting and generate_meet):
            if generate_meet:
                import time
                event['conferenceData'] = {
                    'createRequest': {
                        'requestId': f"meet_req_{int(time.time())}_{task_id or 'new'}",
                        'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                    }
                }
            else:
                meet_link = task.get('meet_link')
                if meet_link:
                    desc = event.get('description', '')
                    event['description'] = f"{desc}\n\nGoogle Meet: {meet_link}" if desc else f"Google Meet: {meet_link}"
                    event['location'] = meet_link
        elif is_any_meeting and location:
            # In-person meeting with known location
            event['location'] = location

        # Add attendees & reminders for ALL meeting types
        if is_any_meeting:
            attendees = []
            target_usernames = task.get('for_users', [])
            
            conn_att, is_pg_att = get_db_connection()
            p_att = "%s" if is_pg_att else "?"
            c_att = conn_att.cursor()
            
            if target_usernames:
                for tu in target_usernames:
                    clean_name = tu.replace("@", "").strip()
                    c_att.execute(f"SELECT email FROM users WHERE LOWER(username) = LOWER({p_att})", (clean_name,))
                    u_row = c_att.fetchone()
                    if u_row:
                        u_mail = u_row['email'] if hasattr(u_row, 'keys') else u_row[0]
                        if u_mail: attendees.append({'email': u_mail})
            
            sender_id = task.get('created_by')
            if sender_id:
                c_att.execute(f"SELECT email FROM users WHERE id = {p_att}", (sender_id,))
                sender_row = c_att.fetchone()
                if sender_row:
                    s_mail = sender_row['email'] if hasattr(sender_row, 'keys') else sender_row[0]
                    if s_mail and not any(a['email'] == s_mail for a in attendees):
                        attendees.append({'email': s_mail})
                        
            conn_att.close()
            
            if attendees:
                event['attendees'] = attendees
            
            event['reminders'] = {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 10},
                    {'method': 'popup', 'minutes': 10},
                ]
            }

        # sendUpdates='all' ensures that attendees receive an email invite and the event is added to their calendar.
        event = service.events().insert(
            calendarId='primary', 
            body=event, 
            conferenceDataVersion=1,
            sendUpdates='all'
        ).execute()
        event_id = event.get('id')
        meet_link = event.get('hangoutLink')
        
        # Store the event ID in the database for this specific task
        if task_id:
            # Reopen connection for update
            conn, is_pg = get_db_connection()
            p = "%s" if is_pg else "?"
            c = conn.cursor()
            
            if generate_meet and meet_link:
                c.execute(f"UPDATE tasks SET google_event_id = {p}, meet_link = {p} WHERE id = {p}", (event_id, meet_link, task_id))
                c.execute(f"SELECT group_id FROM tasks WHERE id = {p}", (task_id,))
                grp_row = c.fetchone()
                # Handle DictCursor vs Row
                grp = grp_row[0] if grp_row and not hasattr(grp_row, '__getitem__') else (grp_row['group_id'] if grp_row else None)
                if grp:
                    c.execute(f"UPDATE tasks SET meet_link = {p} WHERE group_id = {p}", (meet_link, grp))
            else:
                c.execute(f"UPDATE tasks SET google_event_id = {p} WHERE id = {p}", (event_id, task_id))
                
            conn.commit()
            conn.close()
        
        return event.get('htmlLink')
    except Exception as e:
        error_str = str(e)
        if "invalid_grant" in error_str or "expired" in error_str:
            # Clear invalid/revoked token so user knows they need to re-login
            conn, is_pg = get_db_connection()
            p = "%s" if is_pg else "?"
            c = conn.cursor()
            c.execute(f"UPDATE users SET google_token = NULL WHERE id = {p}", (user_id,))
            conn.commit()
            conn.close()
        print(f"Google Calendar Sync Error for user {user_id}: {e}")
        return None

def delete_google_calendar_event(user_id, google_event_id):
    if not google_event_id:
        return
        
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    c.execute(f"SELECT google_token FROM users WHERE id = {p}", (user_id,))
    row = c.fetchone()
    
    if not row or not row['google_token']:
        conn.close()
        return
        
    try:
        creds_info = json.loads(row['google_token'])
        credentials = google.oauth2.credentials.Credentials.from_authorized_user_info(creds_info)
        
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            c.execute(f"UPDATE users SET google_token = {p} WHERE id = {p}", (credentials.to_json(), user_id))
            conn.commit()
            
        service = build('calendar', 'v3', credentials=credentials)
        service.events().delete(calendarId='primary', eventId=google_event_id).execute()
        conn.close()
    except Exception as e:
        print(f"Google Calendar Deletion Error: {e}")

@app.route("/agent", methods=["POST"])
def agent():
    data = request.get_json()
    user_text = data.get("message", "")
    user_date = data.get("date", "")
    user_time = data.get("time", "")
    user_id = data.get("user_id")

    # ── PENDING TASK MODE (байршил хүлээж авах) ──────────────────────────────
    if data.get("pending_task_mode") and data.get("pending_task"):
        pending = data["pending_task"]
        location = user_text.strip()   # хэрэглэгч байршлыг мессежээр явуулна
        pending["location"] = location
        pending["is_online_meeting"] = False
        # Байршлыг content-д нэмэх — хүсэлт авсан хүн хаана болохыг харна
        existing_content = pending.get("content", "") or pending.get("title", "")
        pending["content"] = f"{existing_content} ({location})".strip()

        conn, is_pg = get_db_connection()
        p = "%s" if is_pg else "?"
        c = conn.cursor()

        # for_users-аас target user_id-уудыг олох
        target_usernames = pending.get("for_users", [])
        if not target_usernames:
            c.execute(f"SELECT username FROM users WHERE id = {p}", (user_id,))
            u_row = c.fetchone()
            if u_row:
                uname = u_row['username'] if hasattr(u_row, '__getitem__') else u_row[0]
                target_usernames = [uname]

        target_user_ids = []
        for tu in target_usernames:
            clean = tu.replace("@", "").strip()
            c.execute(f"SELECT id FROM users WHERE LOWER(username) = LOWER({p})", (clean,))
            row = c.fetchone()
            if row:
                uid = row['id'] if hasattr(row, '__getitem__') else row[0]
                target_user_ids.append(uid)

        if user_id not in target_user_ids:
            target_user_ids.append(user_id)

        import uuid
        group_id = str(uuid.uuid4())
        saved_any = False
        for uid in target_user_ids:
            task_to_save = pending.copy()
            task_to_save["group_id"] = group_id
            task_to_save["status"] = "approved" if uid == user_id else "pending"
            task_id = save_task_to_db(task_to_save, uid, user_id)
            if task_id:
                saved_any = True

        conn.close()
        summary = f"✅ '{pending.get('title', 'Уулзалт')}' — {location}-д амжилттай товлогдлоо." if saved_any else "Давхцал байгаа тул хадгалагдсангүй."
        return jsonify({"summary": summary, "tasks": [pending], "available": saved_any})
    # ─────────────────────────────────────────────────────────────────────────

    if not user_text and not user_date and not user_time:
        return jsonify({"error": "Хүсэлт хоосон байна"}), 400

    current_time_str = f"Өнөөдрийн огноо: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    # Add explicit user hints if provided
    hints = ""
    if user_date: hints += f"\nХэрэглэгчийн сонгосон огноо: {user_date}"
    if user_time: hints += f"\nХэрэглэгчийн сонгосон цаг: {user_time}"

    # Database connection for initial mention and conflict checks
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()

    # 1. Mention detection
    mentioned_users = get_mentioned_user_ids(user_text, c)
    mentions = list(mentioned_users.values())
    
    # 2. Immediate conflict check if date/time provided from UI
    if user_date and user_time:
        all_conflicts = {}
        for uid, uname in mentioned_users.items():
            conflicts = get_conflicting_tasks(uid, user_date, user_time)
            if conflicts:
                all_conflicts[uname] = conflicts
        
        # If there's a conflict for any mentioned user, return busy response
        if all_conflicts:
            conflict_messages = []
            for uname, tasks in all_conflicts.items():
                for t in tasks:
                    status_text = f"Уучлаарай, @{uname} тухайн цагт '{t['title']}' ({t['content']}) ажилтай байгаа тул завгүй байна."
                    conflict_messages.append(status_text)
            conn.close()
            return jsonify({
                "summary": " ".join(conflict_messages),
                "tasks": [],
                "available": False
            })

    # Get current user role and all usernames for AI context
    c.execute(f"SELECT role FROM users WHERE id = {p}", (user_id,))
    user_row = c.fetchone()
    user_role = (user_row['role'] if user_row else 'user') if is_pg else (user_row[0] if user_row else 'user')

    c.execute("SELECT username FROM users")
    rows = c.fetchall()
    all_usernames = [f"@{r['username'] if is_pg else r[1]}" for r in rows]
    usernames_context = f"\nБоломжит хэрэглэгчид: {', '.join(all_usernames)}"

    schedule_context = ""
    # If the user asks "What am I doing today?", we still need context.
    # But for creating tasks, we don't want AI to "decide" based on text.
    if "хийх" in user_text or "хуваарь" in user_text or "юу байна" in user_text:
        schedule_context = "\n\n--- Одоогийн хуваарь ---\n" + get_all_tasks_as_string(user_id)

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{current_time_str}{usernames_context}{hints}{schedule_context}"},
                {"role": "user", "content": user_text}
            ],
            temperature=0
        )

        result_text = completion.choices[0].message.content
        result_json = json.loads(result_text)

        # 3a. Override task times with backend regex parsing (more reliable than GPT)
        if result_json.get("tasks"):
            backend_start, backend_end = parse_time_from_text(user_text)
            if backend_start:
                for task in result_json["tasks"]:
                    task["time"] = backend_start
                    if backend_end and backend_end != backend_start:
                        task["end_time"] = backend_end

        # 3b. Run conflict check BEFORE location prompt
        if result_json.get("tasks"):
            all_conflicts = []
            for task in result_json["tasks"]:
                target_usernames = task.get("for_users", [])
                if not target_usernames:
                    c.execute(f"SELECT username FROM users WHERE id = {p}", (user_id,))
                    u_row = c.fetchone()
                    if u_row:
                        uname = u_row['username'] if is_pg else u_row[0]
                        target_usernames = [uname]

                for tu in target_usernames:
                    clean_name = tu.replace("@", "").strip()
                    c.execute(f"SELECT id FROM users WHERE LOWER(username) = LOWER({p})", (clean_name,))
                    u_row = c.fetchone()
                    if u_row:
                        uid = u_row['id'] if is_pg else u_row[0]
                        conflicts = get_conflicting_tasks(uid, task.get('date'), task.get('time'), task.get('end_time'))
                        if conflicts:
                            conflict_msgs = [f"Уучлаарай, @{clean_name} тухайн цагт '{c['title']}' ажилтай байна." for c in conflicts]
                            conn.close()
                            return jsonify({
                                "summary": " ".join(conflict_msgs),
                                "tasks": [],
                                "available": False
                            })

        # 3c. Check if location is needed (only if no conflicts)
        if result_json.get("need_location") and result_json.get("tasks"):
            # Return a question asking for the location instead of saving
            return jsonify({
                "summary": "Уулзалт хийх байршлыг хэлнэ үү? (жишээ нь: оффис, кофе шоп, гэх мэт)",
                "tasks": [],
                "available": True,
                "need_location": True,
                "pending_task": result_json["tasks"][0] if result_json["tasks"] else None
            })

        # 4. Save Logic (Already cleared conflicts in 3b)
        if "tasks" in result_json and result_json["tasks"]:
            enriched_tasks = []
            for task in result_json["tasks"]:
                target_usernames = task.get("for_users", [])
                if not target_usernames:
                    c.execute(f"SELECT username FROM users WHERE id = {p}", (user_id,))
                    u_row = c.fetchone()
                    if u_row:
                        uname = u_row['username'] if is_pg else u_row[0]
                        target_usernames = [uname]
                
                target_user_ids = []
                for tu in target_usernames:
                    clean_name = tu.replace("@", "").strip()
                    c.execute(f"SELECT id FROM users WHERE LOWER(username) = LOWER({p})", (clean_name,))
                    u_row = c.fetchone()
                    if u_row:
                        uid = u_row['id'] if is_pg else u_row[0]
                        target_user_ids.append((uid, clean_name))
                
                enriched_tasks.append((task, [uid for uid, uname in target_user_ids]))

            # proceed to save
            result_json["available"] = True
            for task, uids in enriched_tasks:
                import uuid
                group_id = str(uuid.uuid4())
                
                if user_id not in uids:
                    uids.append(user_id)
                    
                # If multiple users, save for each
                for uid in uids:
                    task_to_save = task.copy()
                    task_to_save['group_id'] = group_id
                    if uid == user_id:
                        task_to_save['status'] = 'approved'
                    else:
                        # Бусад хүмүүст заавал pending байх ёстой
                        task_to_save['status'] = 'pending'
                        
                        # save_task_to_db natively creates the event if status=='approved' (which it is for the Sender)
                        task_id = save_task_to_db(task_to_save, uid, user_id)
                
                # If it was a scheduling request and success
                if not result_json.get("summary") or result_json["summary"] == "Хүсэлтийн товч тайлбар":
                    result_json["summary"] = "Хүсэлтийг амжилттай товлолоо."
        
        return jsonify(result_json)

    except Exception as e:
        error_msg = str(e)
        if "insufficient_quota" in error_msg:
            return jsonify({"error": "OpenAI-ийн төлбөр/quota дууссан байна."}), 429
        return jsonify({"error": f"Алдаа гарлаа: {error_msg}"}), 500
    finally:
        conn.close()

@app.route("/tasks", methods=["GET"])
def get_tasks():
    user_id = request.args.get("user_id")
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    
    if user_id:
        c.execute(f'''
            SELECT tasks.*, u1.username as created_by_username, u2.username as target_username
            FROM tasks 
            LEFT JOIN users u1 ON tasks.created_by = u1.id 
            LEFT JOIN users u2 ON tasks.user_id = u2.id
            WHERE tasks.user_id = {p} OR tasks.created_by = {p}
            ORDER BY tasks.created_at DESC
        ''', (user_id, user_id))
    else:
        return jsonify([])
        
    rows = c.fetchall()
    conn.close()
    
    # Use a dictionary to avoid duplicates (e.g. if a pending task is assigned to the admin itself)
    tasks_dict = {}
    for row in rows:
        t = dict(row)
        tasks_dict[t['id']] = t
    
    tasks = list(tasks_dict.values())
    tasks.sort(key=lambda x: x['created_at'], reverse=True)
    return jsonify(tasks)

@app.route("/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    c.execute(f'SELECT group_id FROM tasks WHERE id = {p}', (task_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "Task not found"})
        
    group_id = row['group_id'] if is_pg else row[0]
        
    if group_id:
        c.execute(f'SELECT user_id, status, created_by, google_event_id FROM tasks WHERE group_id = {p}', (group_id,))
        rows = c.fetchall()
        
        for r in rows:
            t = dict(r)
            owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
            delete_google_calendar_event(owner_id, t['google_event_id'])
            
        c.execute(f'DELETE FROM tasks WHERE group_id = {p}', (group_id,))
    else:
        c.execute(f'SELECT user_id, status, created_by, google_event_id FROM tasks WHERE id = {p}', (task_id,))
        r = c.fetchone()
        if r:
            t = dict(r)
            owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
            delete_google_calendar_event(owner_id, t['google_event_id'])
            c.execute(f'DELETE FROM tasks WHERE id = {p}', (task_id,))
            
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Tasks deleted successfully"})

@app.route("/tasks/<int:task_id>/status", methods=["POST"])
def update_task_status(task_id):
    data = request.get_json()
    new_status = data.get("status")
    
    conn, is_pg = get_db_connection()
    p = "%s" if is_pg else "?"
    c = conn.cursor()
    
    # 1. Fetch task
    c.execute(f'SELECT * FROM tasks WHERE id = {p}', (task_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "Task not found"}), 404
        
    task = dict(row)
    
    # 2. Approve or Reject logic
    if new_status == 'rejected':
        group_id = task.get('group_id')
        if group_id:
            c.execute(f'SELECT user_id, status, created_by, google_event_id FROM tasks WHERE group_id = {p}', (group_id,))
            tasks_to_delete = c.fetchall()
            for r in tasks_to_delete:
                t = dict(r)
                owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
                delete_google_calendar_event(owner_id, t['google_event_id'])
            c.execute(f'DELETE FROM tasks WHERE group_id = {p}', (group_id,))
        else:
            owner_id = task['created_by'] if task['status'] == 'pending' and task['created_by'] else task['user_id']
            delete_google_calendar_event(owner_id, task['google_event_id'])
            c.execute(f'DELETE FROM tasks WHERE id = {p}', (task_id,))
    elif new_status == 'approved':
        # Check conflicts - Exclude current group_id to allow accepting shared tasks
        if is_time_conflict(task['user_id'], task['date'], task['time'], task['end_time'], exclude_group_id=task.get('group_id')):
            conn.close()
            return jsonify({"success": False, "error": "Давхцал илэрлээ. Хэрэглэгч завгүй."}), 409
        
        # Update status
        c.execute(f'UPDATE tasks SET status = {p} WHERE id = {p}', (new_status, task_id))
        conn.commit()
        
        # Google Calendar sync for receiver (doesn't generate duplicate meet link)
        try:
            task_copy = dict(task)
            # Fetch the updated meet_link directly
            c.execute(f'SELECT meet_link FROM tasks WHERE id = {p}', (task_id,))
            updated_task = c.fetchone()
            if updated_task:
                task_copy['meet_link'] = updated_task['meet_link'] if is_pg else updated_task[0]
            
            create_google_calendar_event(task_copy, task['user_id'], task_id, generate_meet=False)
        except Exception as e:
            print(f"Calendar sync failed: {e}")
    else:
        # For any other custom status
        c.execute(f'UPDATE tasks SET status = {p} WHERE id = {p}', (new_status, task_id))
    
    conn.commit()
    conn.close()
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Run DB init with updated PostgreSQL support
    try:
        init_db()
    except Exception as e:
        print(f"DB Init Error: {e}")
    app.run(host="0.0.0.0", port=port, debug=False)
