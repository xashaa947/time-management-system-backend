import json
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Mocking the AI client from app.py logic
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def test_parse_email(subject, snippet):
    prompt = f"""
МЭДЭЭЛЭЛ: Дараах и-мэйлээс уулзалт, ажил эсвэл даалгавар байгаа эсэхийг тодорхойлно уу.
Subject: {subject}
Snippet: {snippet}

Хэрэв уулзалт эсвэл ажил байвал "tasks" массивт JSON хэлбэрээр оруулна уу. 
Массив хоосон байж болно. 
Бүтэц (Зөвхөн JSON):
{{
  "tasks": [
    {{
      "type": "ajlaar | choloot",
      "title": "Мэйлээс олдсон гарчиг",
      "content": "Мэйлийн агуулга",
      "date": "YYYY-MM-DD (хэрэв олдвол, үгүй бол өнөөдрийн огноо эсвэл хоосон)",
      "time": "HH:mm",
      "status": "pending"
    }}
  ]
}}
"""
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(completion.choices[0].message.content)

# Test cases
test_emails = [
    {
        "subject": "Уулзалтын тов",
        "snippet": "Маргааш буюу 2026-03-20-ны 10:00 цагт төслийн хурлын талаар ярилцахаар товлолоо."
    },
    {
        "subject": "Спортын заал захиалга",
        "snippet": "Таны 2026-03-21-ний 18:00 цагийн заалны захиалга баталгаажсан байна."
    },
    {
        "subject": "Зүгээр нэг мэндчилгээ",
        "snippet": "Сайн байна уу? Таны ажил төрөл сайн уу? Дараа тухтай уулзъя."
    }
]

for email in test_emails:
    print(f"Testing email: {email['subject']}")
    try:
        result = test_parse_email(email['subject'], email['snippet'])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
    print("-" * 20)
