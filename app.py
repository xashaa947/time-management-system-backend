from flask import Flask, request, jsonify, url_for, session, redirect
from flask_cors import CORS
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()
import sqlite3
import json
from datetime import datetime, timedelta
import google.oauth2.credentials
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
import requests

# Allow insecure transport for local development (OAUTH2 over HTTP)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
# Allow scope changes (e.g. if Google Calendar API is not granted)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

app = Flask(__name__)
app.secret_key = "ai_agent_secret_key_change_this"
CORS(app)

# Database Setup
DB_NAME = "schedule.db"

def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    c = conn.cursor()   
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    ''')
    # Existing tables migration
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN end_time TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'approved'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN google_token TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN code_verifier TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN google_event_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN email TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN group_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN meet_link TEXT")
    except sqlite3.OperationalError:
        pass
    
    # Update all existing users to admin
    try:
        c.execute("UPDATE users SET role = 'admin'")
    except:
        pass
        
    conn.commit()
    conn.close()

init_db()

def is_time_conflict(user_id, date, start_time, end_time):
    if not user_id or not date or not start_time:
        return False
        
    # Handle missing end_time by defaulting to 1 hour after start_time
    if not end_time:
        try:
            h, m = map(int, start_time.split(':'))
            end_time = f"{(h+1)%24:02d}:{m:02d}"
        except:
            end_time = start_time

    conn = sqlite3.connect(DB_NAME, timeout=30)
    c = conn.cursor()

    c.execute('''
        SELECT time, end_time FROM tasks
        WHERE user_id = ? AND date = ? AND status = 'approved'
    ''', (user_id, date))

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
            if not row[0]: continue
            existing_start = datetime.strptime(row[0], "%H:%M")
            
            if row[1]:
                existing_end = datetime.strptime(row[1], "%H:%M")
                if existing_end <= existing_start:
                    existing_end += timedelta(days=1)
            else:
                # Default existing task to 1 hour if no end_time
                existing_end = existing_start + timedelta(hours=1)

            if (new_start < existing_end) and (new_end > existing_start):
                return True  # Давхцал байна
    except Exception as e:
        print(f"Conflict check error: {e}")
        return False

    return False  # Давхцахгүй 

def save_task_to_db(task, user_id=None, created_by=None):
    if not user_id: return None
    
    # Skip conflict check for pending tasks (they are just requests)
    if task.get('status', 'approved') == 'approved':
        if is_time_conflict(user_id, task.get('date'), task.get('time'), task.get('end_time')):
            print(f"⚠️ Давхцал илэрлээ! User: {user_id}, Date: {task.get('date')}, Time: {task.get('time')}")
            return None

    conn = sqlite3.connect(DB_NAME, timeout=30)
    c = conn.cursor()
    c.execute('''
        INSERT INTO tasks (user_id, type, content, date, time, end_time, status, title, created_by, group_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, task.get('type'), task.get('content'), task.get('date'), task.get('time'), 
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
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, email FROM users")
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(users)

def get_mentioned_user_ids(user_text, db_cursor):
    """
    Хэрэглэгчийн текстээс @username-г олж, тухайн хэрэглэгчийн ID-г буцаана.
    Хоосон зайтай нэрсийг илрүүлэхийн тулд өгөгдлийн сангийн нэрстэй харьцуулна.
    """
    db_cursor.execute("SELECT id, username FROM users")
    all_users = db_cursor.fetchall()
    user_ids = {}
    lower_text = user_text.lower()
    
    for uid, username in all_users:
        mention_tag = f"@{username.lower()}"
        if mention_tag in lower_text:
            user_ids[uid] = username
    return user_ids 

def get_conflicting_tasks(user_id, date, start_time, end_time=None):
    """
    Хэрэглэгчийн тухайн огноо, цагт давхцсан бүх task-уудыг буцаана
    """
    if not user_id or not date or not start_time:
        return []

    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT title, content, time, end_time FROM tasks
        WHERE user_id = ? AND date = ? AND status = 'approved'
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
            if not row['time']: continue
            existing_start = datetime.strptime(row['time'], "%H:%M")
            if row['end_time']:
                existing_end = datetime.strptime(row['end_time'], "%H:%M")
                if existing_end <= existing_start:
                    existing_end += timedelta(days=1)
            else:
                existing_end = existing_start + timedelta(hours=1)
                
            if (new_start < existing_end) and (new_end > existing_start):
                conflicts.append({"title": row['title'], "content": row['content']})
        return conflicts
    except:
        return []
@app.route("/admin/add-task", methods=["POST"])
def admin_add_task():
    data = request.get_json()
    user_id = data.get("user_id")
    # Simple direct add
    save_task_to_db(data, user_id, user_id)
    return jsonify({"success": True, "message": "Task added successfully"})

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_all_tasks_as_string(user_id=None):
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id:
        c.execute('SELECT type, content, date, time, end_time, status, title FROM tasks WHERE user_id = ? ORDER BY date, time', (user_id,))
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
      "for_users": ["username1", "username2"]
    }
  ],
  "available": true
}

**ДҮРЭМ:**
1. **ХОУМ (PARSING):** Хэрэглэгчийн хүсэлтээс ажил эсвэл уулзалтын мэдээллийг (нэр, огноо, цаг, төрөл) зөв салгаж авна.
2. **МЕНШИН (@):** Хэрэв текст дотор хүмүүсийн нэр (@username) байвал тэдгээрийг "for_users" жагсаалтад оруулна. 
3. **ТӨЛӨВ (STATUS):** Хэрэв "for_users" дотор хүмүүс байвал "status" нь заавал "pending" байх ёстой. Зөвхөн хэрэглэгч өөртөө ажил нэмж байгаа тохиолдолд "approved" байна.
4. **ХУВААРЬ:** Чи өөрөө хуваарь шалгах шаардлагагүй, зөвхөн хүсэлтийг JSON болгож хувиргахад анхаарна уу. 
5. **ХАРИУ:** Хэрэв мэдээлэл асуусан бол (жишээ нь "Өнөөдөр юу хийх вэ?") "tasks" массив хоосон байна.
6. **Зөвхөн JSON.**
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
    print(f"DEBUG: [login] Generated State: {state}")
    print(f"DEBUG: [login] Generated Code Verifier: {flow.code_verifier}")
    
    try:
        conn = sqlite3.connect(DB_NAME, timeout=30)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS oauth_sessions (state TEXT PRIMARY KEY, code_verifier TEXT)")
        c.execute("INSERT OR REPLACE INTO oauth_sessions (state, code_verifier) VALUES (?, ?)", (state, flow.code_verifier))
        conn.commit()
        
        # Verify it was saved
        c.execute("SELECT code_verifier FROM oauth_sessions WHERE state = ?", (state,))
        check_row = c.fetchone()
        if check_row:
            print(f"DEBUG: [login] Successfully saved state to DB. Verifier in DB: {check_row[0]}")
        else:
            print("DEBUG: [login] ERROR: Failed to save state to DB!")
        conn.close()
    except Exception as e:
        print(f"DEBUG: [login] DB Error: {e}")
    
    return jsonify({"auth_url": authorization_url})

@app.route('/api/google/callback')
def google_callback():
    state = request.args.get('state')
    print(f"DEBUG: [callback] Received state: {state}")
    
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT code_verifier FROM oauth_sessions WHERE state = ?", (state,))
    row = c.fetchone()
    
    code_verifier = row['code_verifier'] if row else None
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
         print("DEBUG: [callback] CRITICAL: Missing code_verifier. State lookup failed.")
         return jsonify({"error": "OAuth state mismatch or expired. Please go back to the login page and try again."}), 400
    try:
        # Explicitly pass code_verifier just in case setdefault fails for some reason
        flow.fetch_token(authorization_response=authorization_response, code_verifier=code_verifier)
        print("DEBUG: [callback] Token fetch successful")
        
        # Delete state ONLY after successful token fetch
        if state:
            c.execute("DELETE FROM oauth_sessions WHERE state = ?", (state,))
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
        c.execute("SELECT * FROM users WHERE email = ?", (user_email,))
        user = c.fetchone()
        if user:
            user_id = user['id']
            c.execute("UPDATE users SET google_token = ?, username = ? WHERE id = ?", (creds_json, user_name, user_id))
            conn.commit()
        else:
            c.execute("SELECT id FROM users WHERE username = ?", (user_name,))
            if c.fetchone():
                import random
                user_name = f"{user_name}_{random.randint(100,999)}"
            c.execute("INSERT INTO users (username, password, role, email, google_token) VALUES (?, ?, ?, ?, ?)",
                      (user_name, '', 'admin', user_email, creds_json))
            user_id = c.lastrowid
            conn.commit()
            
        c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
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
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return redirect(f"{frontend_url}/?auth_data={encoded_user}")
    else:
        return jsonify({"error": "Google login failed, no email found"})

def create_google_calendar_event(task, user_id, task_id=None, generate_meet=True):
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT google_token FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    if not row or not row['google_token']:
        print(f"No google_token for user {user_id}")
        return None
        
    try:
        creds_info = json.loads(row['google_token'])
        credentials = google.oauth2.credentials.Credentials.from_authorized_user_info(creds_info)
        
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            conn = sqlite3.connect(DB_NAME, timeout=30)
            c = conn.cursor()
            c.execute("UPDATE users SET google_token = ? WHERE id = ?", (credentials.to_json(), user_id))
            conn.commit()
            conn.close()
            
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
        
        # Check if this is a meeting to add Google Meet link & Reminders
        title_lower = task.get('title', '').lower()
        type_lower = task.get('type', '').lower()
        if 'уулзалт' in title_lower or 'uulzalt' in title_lower or 'уулзалт' in type_lower or 'uulzalt' in type_lower:
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

            attendees = []
            target_usernames = task.get('for_users', [])
            
            conn_users = sqlite3.connect(DB_NAME, timeout=30)
            conn_users.row_factory = sqlite3.Row
            c_users = conn_users.cursor()
            
            #huselt avsan hun
            if target_usernames:
                for tu in target_usernames:
                    clean_name = tu.replace("@", "").strip()
                    c_users.execute("SELECT email FROM users WHERE LOWER(username) = LOWER(?)", (clean_name,))
                    u_row = c_users.fetchone()
                    if u_row and u_row['email']:
                        attendees.append({'email': u_row['email']})
            
            # sender_email (хүсэлт явуулсан хүн)
            sender_id = task.get('created_by')
            if sender_id:
                c_users.execute("SELECT email FROM users WHERE id = ?", (sender_id,))
                sender_row = c_users.fetchone()
                if sender_row and sender_row['email']:
                    sender_email = sender_row['email']
                    if not any(a['email'] == sender_email for a in attendees):
                        attendees.append({'email': sender_email})
                        
            conn_users.close()
            
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
            conn = sqlite3.connect(DB_NAME, timeout=30)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            if generate_meet and meet_link:
                c.execute("UPDATE tasks SET google_event_id = ?, meet_link = ? WHERE id = ?", (event_id, meet_link, task_id))
                c.execute("SELECT group_id FROM tasks WHERE id = ?", (task_id,))
                grp = c.fetchone()
                if grp and grp['group_id']:
                    c.execute("UPDATE tasks SET meet_link = ? WHERE group_id = ?", (meet_link, grp['group_id']))
            else:
                c.execute("UPDATE tasks SET google_event_id = ? WHERE id = ?", (event_id, task_id))
                
            conn.commit()
            conn.close()
        
        return event.get('htmlLink')
    except Exception as e:
        error_str = str(e)
        if "invalid_grant" in error_str or "expired" in error_str:
            # Clear invalid/revoked token so user knows they need to re-login
            try:
                conn = sqlite3.connect(DB_NAME, timeout=30)
                c = conn.cursor()
                c.execute("UPDATE users SET google_token = NULL WHERE id = ?", (user_id,))
                conn.commit()
                conn.close()
                print(f"DEBUG: Token for user {user_id} was revoked/expired and has been cleared from DB.")
            except:
                pass
        print(f"Google Calendar Sync Error for user {user_id}: {e}")
        return None

def delete_google_calendar_event(user_id, google_event_id):
    if not google_event_id:
        return
        
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT google_token FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    if not row or not row['google_token']:
        return
        
    try:
        creds_info = json.loads(row['google_token'])
        credentials = google.oauth2.credentials.Credentials.from_authorized_user_info(creds_info)
        
        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            conn = sqlite3.connect(DB_NAME, timeout=30)
            c = conn.cursor()
            c.execute("UPDATE users SET google_token = ? WHERE id = ?", (credentials.to_json(), user_id))
            conn.commit()
            conn.close()
            
        service = build('calendar', 'v3', credentials=credentials)
        service.events().delete(calendarId='primary', eventId=google_event_id).execute()
    except Exception as e:
        print(f"Google Calendar Deletion Error: {e}")

@app.route("/agent", methods=["POST"])
def agent():
    data = request.get_json()
    user_text = data.get("message", "")
    user_date = data.get("date", "")
    user_time = data.get("time", "")
    user_id = data.get("user_id")

    if not user_text and not user_date and not user_time:
        return jsonify({"error": "Хүсэлт хоосон байна"}), 400

    current_time_str = f"Өнөөдрийн огноо: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    # Add explicit user hints if provided
    hints = ""
    if user_date: hints += f"\nХэрэглэгчийн сонгосон огноо: {user_date}"
    if user_time: hints += f"\nХэрэглэгчийн сонгосон цаг: {user_time}"

    # Database connection for initial mention and conflict checks
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
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
    c.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    user_row = c.fetchone()
    user_role = user_row['role'] if user_row else 'user'

    c.execute("SELECT username FROM users")
    all_usernames = [f"@{r['username']}" for r in c.fetchall()]
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

        # 3. Backend Conflict Checking Logic
        if "tasks" in result_json and result_json["tasks"]:
            all_conflicts = []
            enriched_tasks = []
            
            for task in result_json["tasks"]:
                target_usernames = task.get("for_users", [])
                if not target_usernames:
                    # Default to current user
                    c.execute("SELECT username FROM users WHERE id = ?", (user_id,))
                    u_row = c.fetchone()
                    if u_row: target_usernames = [u_row['username']]
                
                target_user_ids = []
                for tu in target_usernames:
                    clean_name = tu.replace("@", "").strip()
                    c.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (clean_name,))
                    u_row = c.fetchone()
                    if u_row: target_user_ids.append((u_row['id'], clean_name))

                # Check conflicts for each target user
                for tid, tname in target_user_ids:
                    conflicts = get_conflicting_tasks(tid, task.get('date'), task.get('time'), task.get('end_time'))
                    if conflicts:
                        for conf in conflicts:
                            all_conflicts.append(f"Уучлаарай, @{tname} тухайн цагт '{conf['title']}' ({conf['content']}) ажилтай байгаа тул завгүй байна.")
                
                if not all_conflicts:
                    enriched_tasks.append((task, [uid for uid, uname in target_user_ids]))

            if all_conflicts:
                result_json["available"] = False
                result_json["summary"] = " ".join(all_conflicts)
                result_json["tasks"] = []
                return jsonify(result_json)
            else:
                # No conflicts, proceed to save
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
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if user_id:
        c.execute('''
            SELECT tasks.*, u1.username as created_by_username, u2.username as target_username
            FROM tasks 
            LEFT JOIN users u1 ON tasks.created_by = u1.id 
            LEFT JOIN users u2 ON tasks.user_id = u2.id
            WHERE tasks.user_id = ? OR tasks.created_by = ?
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
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT group_id FROM tasks WHERE id = ?', (task_id,))
    task = c.fetchone()
    
    if not task:
        conn.close()
        return jsonify({"success": False, "error": "Task not found"})
        
    try:
        group_id = task['group_id']
    except IndexError:
        group_id = None
        
    if group_id:
        c.execute('SELECT user_id, status, created_by, google_event_id FROM tasks WHERE group_id = ?', (group_id,))
        tasks_to_delete = c.fetchall()
        
        for t in tasks_to_delete:
            owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
            delete_google_calendar_event(owner_id, t['google_event_id'])
            
        c.execute('DELETE FROM tasks WHERE group_id = ?', (group_id,))
    else:
        c.execute('SELECT user_id, status, created_by, google_event_id FROM tasks WHERE id = ?', (task_id,))
        t = c.fetchone()
        if t:
            owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
            delete_google_calendar_event(owner_id, t['google_event_id'])
            c.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Tasks deleted successfully"})

@app.route("/tasks/<int:task_id>/status", methods=["POST"])
def update_task_status(task_id):
    data = request.get_json()
    new_status = data.get("status")
    
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Fetch task
    c.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
    task = c.fetchone()
    
    if not task:
        conn.close()
        return jsonify({"success": False, "error": "Task not found"}), 404
    
    # 2. Approve or Reject logic
    if new_status == 'rejected':
        try:
            group_id = task['group_id']
        except IndexError:
            group_id = None
            
        if group_id:
            c.execute('SELECT user_id, status, created_by, google_event_id FROM tasks WHERE group_id = ?', (group_id,))
            tasks_to_delete = c.fetchall()
            for t in tasks_to_delete:
                owner_id = t['created_by'] if t['status'] == 'pending' and t['created_by'] else t['user_id']
                delete_google_calendar_event(owner_id, t['google_event_id'])
            c.execute('DELETE FROM tasks WHERE group_id = ?', (group_id,))
        else:
            owner_id = task['created_by'] if task['status'] == 'pending' and task['created_by'] else task['user_id']
            delete_google_calendar_event(owner_id, task['google_event_id'])
            c.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
    elif new_status == 'approved':
        # Check conflicts
        if is_time_conflict(task['user_id'], task['date'], task['time'], task['end_time']):
            conn.close()
            return jsonify({"success": False, "error": "Давхцал илэрлээ. Хэрэглэгч завгүй."}), 409
        
        # Update status
        c.execute('UPDATE tasks SET status = ? WHERE id = ?', (new_status, task_id))
        conn.commit()
        
        # Google Calendar sync for receiver (doesn't generate duplicate meet link)
        try:
            task_copy = dict(task)
            task_copy['created_by'] = task['created_by']
            # Fetch the updated meet_link directly in case it wasn't pre-populated in the `task` variable initially
            c.execute('SELECT meet_link FROM tasks WHERE id = ?', (task_id,))
            updated_task = c.fetchone()
            if updated_task and updated_task['meet_link']:
                task_copy['meet_link'] = updated_task['meet_link']
            
            create_google_calendar_event(task_copy, task['user_id'], task_id, generate_meet=False)
        except Exception as e:
            print(f"Calendar sync failed: {e}")
    else:
        # For any other custom status
        c.execute('UPDATE tasks SET status = ? WHERE id = ?', (new_status, task_id))
    
    conn.commit()
    conn.close()
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
