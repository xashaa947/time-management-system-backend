import os
from dotenv import load_dotenv
load_dotenv(override=True)
import psycopg2
from psycopg2.extras import DictCursor
from datetime import datetime, timedelta

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    try:
        conn = psycopg2.connect(db_url, cursor_factory=DictCursor)
        return conn, True
    except Exception as e:
        print(f"Connection Error: {e}")
        return None, False

def is_time_conflict(user_id, date, start_time, end_time):
    print(f"\nChecking conflict for User: {user_id}, Date: {date}, Time: {start_time} - {end_time}")
    conn, is_pg = get_db_connection()
    if not conn:
        return False
        
    c = conn.cursor()
    # Updated SQL to check both approved and pending
    sql = "SELECT title, time, end_time, status FROM tasks WHERE user_id = %s AND date = %s AND (status = 'approved' OR status = 'pending')"
    c.execute(sql, (user_id, date))
    rows = c.fetchall()
    conn.close()
    
    print(f"Found {len(rows)} relevant tasks on this date:")
    for r in rows:
        print(f"  - {r['title']} [{r['status']}]: {r['time']} to {r['end_time']}")

    try:
        new_start = datetime.strptime(start_time, "%H:%M")
        new_end = datetime.strptime(end_time, "%H:%M")
        if new_end <= new_start:
             new_end += timedelta(days=1)

        for row in rows:
            # Replicating the logic in app.py
            r_time = row['time']
            r_end = row['end_time']
            
            if not r_time: continue
            existing_start = datetime.strptime(r_time, "%H:%M")
            if r_end:
                existing_end = datetime.strptime(r_end, "%H:%M")
                if existing_end <= existing_start:
                    existing_end += timedelta(days=1)
            else:
                existing_end = existing_start + timedelta(hours=1)

            if (new_start < existing_end) and (new_end > existing_start):
                print(f"CONFLICT FOUND with: {row['title']} ({row['status']})")
                return True
    except Exception as e:
        print(f"Logic Error: {e}")
        return False
    
    print("No conflict found.")
    return False

if __name__ == "__main__":
    is_time_conflict(2, "2026-05-23", "15:00", "17:00")
    is_time_conflict(2, "2026-05-23", "02:00", "03:00") # Sample check
