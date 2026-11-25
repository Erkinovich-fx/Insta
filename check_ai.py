"""AI kalitlarini tekshirish: python check_ai.py
main.py dagi har bir AI xizmati va kalitini sinab ko'radi. Kalitlar to'liq ko'rsatilmaydi —
natijani skrinshot qilib yuborish xavfsiz."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402  (import paytida kalitlar va modellar ro'yxati chiqadi)

print()
any_ok = False
for p in main.AI_PROVIDERS:
    if not p["keys"]:
        print(f"— {p['name']}: kalit yozilmagan")
        continue
    for key in p["keys"]:
        model = p["models"][0]
        data = {"model": model, "messages": [{"role": "user", "content": "Salom, qalaysiz?"}]}
        if p.get("max_tokens"):
            data["max_tokens"] = 50
        try:
            res = main.http.post(f"{p['base']}/chat/completions", headers=main._auth(key), json=data, timeout=30)
        except Exception as e:
            print(f"❌ {p['name']} {main.describe_key(key)}: ulanib bo'lmadi — {type(e).__name__}: {e}")
            continue
        if res.status_code == 200:
            any_ok = True
            text = (res.json()["choices"][0]["message"].get("content") or "").strip().replace("\n", " ")
            print(f"✅ {p['name']} {main.describe_key(key)} / {model}: ISHLAYAPTI — \"{text[:80]}\"")
        else:
            print(f"❌ {p['name']} {main.describe_key(key)} / {model}: {res.status_code} — {res.text[:200]}")

print("\n✅ Kamida bitta AI ishlayapti — botni ishga tushirishingiz mumkin." if any_ok
      else "\n❌ Hech bir AI ishlamadi. main.py boshidagi kalitlarni tekshiring.")
