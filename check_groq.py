"""Groq API kalitini tekshirish: python check_groq.py
Natijani (skrinshot) yuboring — kalit to'liq ko'rsatilmaydi."""
import os
import sys

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
env_path = os.path.join(BASE_DIR, ".env")

print("Python:", sys.version.split()[0])
print("Papka:", BASE_DIR)
print(".env bormi:", os.path.exists(env_path))
if os.path.exists(env_path):
    with open(env_path, "rb") as f:
        raw = f.read()
    lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    print(f".env: {len(lines)} qator, {len(raw)} bayt")
    for i, line in enumerate(lines, 1):
        name = line.split("=", 1)[0].strip() if "=" in line else "(= yo'q)"
        print(f"   {i}-qator nomi: {name!r}")
print("Shell'dagi export GROQ_API_KEY:", "bor" if os.environ.get("GROQ_API_KEY") else "yo'q")

# main.py bilan bir xil yo'l bilan kalitni o'qiymiz (main.py ni ishga tushirmasdan)
from main import GROQ_API_KEY, KEY_SOURCE, describe_key  # noqa: E402

print("\nIshlatiladigan kalit:", describe_key(GROQ_API_KEY), "| manba:", KEY_SOURCE["value"])
if not GROQ_API_KEY:
    print("❌ Kalit topilmadi. .env fayliga: GROQ_API_KEY=gsk_... deb yozing.")
    sys.exit(1)

headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
try:
    r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=20)
    print("\n/models javobi:", r.status_code, r.text[:300])
    r2 = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": "Salom"}], "max_tokens": 20},
        timeout=20,
    )
    print("\nchat javobi:", r2.status_code, r2.text[:300])
except Exception as e:
    print("\n❌ Groq serveriga ulanib bo'lmadi:", type(e).__name__, e)
    print("   Internet / VPN ni tekshiring.")
    sys.exit(1)

if r2.status_code == 200:
    print("\n✅ KALIT ISHLAYAPTI. main.py ni qayta ishga tushiring.")
elif r2.status_code == 401:
    print("\n❌ Groq kalitni rad etdi (401). Kalit o'chirilgan yoki xato. console.groq.com/keys dan yangisini oling.")
elif r2.status_code == 403:
    print("\n❌ 403: Groq bu tarmoq/mamlakatdan kirishni bloklagan bo'lishi mumkin. VPN bilan sinab ko'ring.")
elif r2.status_code == 429:
    print("\n⚠️ 429: limit tugagan. Biroz kuting yoki boshqa akkauntdan kalit oling.")
