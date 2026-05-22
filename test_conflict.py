"""
Цагийн давхцал шалгах тест
"""
import requests
import json

BASE = "http://127.0.0.1:5000"

def test():
    # 1. Одоо байгаа task-уудыг хар
    print("=== 1. Одоогийн approved tasks (user_id=2) ===")
    try:
        resp = requests.get(f"{BASE}/tasks?user_id=2", timeout=5)
        tasks = resp.json()
        approved = [t for t in tasks if t.get("status") == "approved" and t.get("date")]
        for t in approved[:10]:
            print(f"  {t['date']} {t.get('time','?')}-{t.get('end_time','?')} : {t.get('title','?')}")
        if not approved:
            print("  (approved task олдсонгүй)")
    except Exception as e:
        print(f"  Алдаа: {e}")
        return

    # 2. Давхцалтай цагт шинэ task нэмэхийг оролд
    if approved:
        test_date = approved[0]["date"]
        test_time = approved[0].get("time", "14:00")
        print(f"\n=== 2. Давхцал тест: {test_date} {test_time} ===")
        try:
            resp = requests.post(f"{BASE}/agent", json={
                "message": f"{test_date}-нд {test_time} цагт уулзалт товлоно уу",
                "user_id": 2
            }, timeout=15)
            data = resp.json()
            print(f"  available: {data.get('available')}")
            print(f"  summary: {data.get('summary', '')[:200]}")
            if data.get("available") == False:
                print("  ✅ ДАВХЦАЛ ЗӨВ ИЛЭРСЭН!")
            else:
                print("  ⚠️ Давхцал илрээгүй (bug байж магадгүй)")
        except Exception as e:
            print(f"  Алдаа: {e}")

    # 3. Давхцалгүй цагт шинэ task
    print(f"\n=== 3. Давхцалгүй тест: 2026-05-30 03:00 ===")
    try:
        resp = requests.post(f"{BASE}/agent", json={
            "message": "5 сарын 30-нд 3 цагт ном унших",
            "user_id": 2
        }, timeout=15)
        data = resp.json()
        print(f"  available: {data.get('available')}")
        print(f"  summary: {data.get('summary', '')[:200]}")
        if data.get("tasks"):
            for t in data["tasks"]:
                print(f"  Task time: {t.get('time')}-{t.get('end_time')}")
    except Exception as e:
        print(f"  Алдаа: {e}")

if __name__ == "__main__":
    test()
