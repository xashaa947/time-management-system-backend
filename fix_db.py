import sqlite3
import os

DB_PATH = 'd:/OCR/diplom/backend/schedule.db'

def fix():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Update Oyuka's role
    print("Updating Oyuka's role to admin...")
    c.execute("UPDATE users SET role = 'admin' WHERE username = 'oyuka'")
    conn.commit()
    
    # Verify users
    c.execute("SELECT id, username, role FROM users")
    users = c.fetchall()
    print("\nUsers:")
    for u in users:
        print(u)
        
    # Verify latest tasks
    c.execute("SELECT id, user_id, status, title FROM tasks ORDER BY id DESC LIMIT 10")
    tasks = c.fetchall()
    print("\nLatest Tasks:")
    for t in tasks:
        print(t)
        
    conn.close()

if __name__ == "__main__":
    fix()
