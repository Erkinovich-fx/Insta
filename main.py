import json
import os
import time
import threading
import random
import requests
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    FeedbackRequired,
    ChallengeRequired,
    ClientThrottledError,
)

# 1. GROQ AI SOZLAMASI
# Kalitni .env / muhit o'zgaruvchisi orqali bering: export GROQ_API_KEY=...
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or "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"

ACCOUNTS_FILE = "accounts.json"
DB_FILE = "replied_data.json"

# Katta akkauntlar uchun sozlamalar
# Tezlik: DM lar tez-tez, izohlar va so'rovlar kamroq tekshiriladi.
# DM_POLL_DELAY ni juda kichik qilmang — Instagram ko'p so'rovda cheklov (PleaseWait) qo'yadi.
DM_POLL_DELAY = (8, 14)          # Asosiy/Umumiy DM lar har necha sekundda tekshiriladi
PENDING_INTERVAL = 45            # So'rovlar (Requests) jildi har necha sekundda
COMMENTS_INTERVAL = 90           # Izohlar har necha sekundda tekshiriladi
REQUEST_DELAY = [0.3, 1.0]       # Har bir Instagram so'rovidan oldingi pauza (instagrapi)
DM_THREADS_PER_BOX = 20          # Har bir jilddan nechta chat olinadi
MEDIA_TO_WATCH = 6               # Eng yangi nechta post/reels kuzatiladi
MEDIA_REFRESH_SEC = 600          # Postlar ro'yxati har 10 daqiqada yangilanadi
MAX_REPLIES_PER_CYCLE = 8        # Bir aylanishda maksimum javoblar (spam-blokdan himoya)
ACTION_DELAY = (2, 5)            # Har bir javobdan keyingi pauza
DB_MAX_IDS = 20000               # Bazada saqlanadigan maksimum ID lar

DEFAULT_BEHAVIOR = (
    "Sen juda xushmuomala yordamchisan. Mijoz qaysi tilda yozsa, o'sha tilda javob qaytar "
    "(asosan O'zbek tilida). Qisqa, lo'nda va kamida 2 ta emoji ishlat."
)


FAST_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def get_groq_models():
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            all_models = [m["id"] for m in res.json().get("data", [])]
            chat_models = [
                m for m in all_models
                if not any(x in m.lower() for x in ["whisper", "guard", "vision", "tts", "audio", "embed"])
            ]
            # Tez, "o'ylamaydigan" modellar birinchi. qwen/deepseek kabi reasoning modellar
            # avval <think> yozadi — sekin va max_tokens da javob kesilib qolishi mumkin.
            preferred = [m for m in FAST_MODELS if m in chat_models]
            for keyword in ["llama", "gemma", "mixtral", "qwen", "deepseek"]:
                for m in chat_models:
                    if keyword in m.lower() and m not in preferred:
                        preferred.append(m)
            for m in chat_models:
                if m not in preferred:
                    preferred.append(m)
            if preferred:
                return preferred
    except Exception as e:
        print(f"[AI] Modellar ro'yxatini olishda xato: {e}")
    return ["llama-3.3-70b-versatile"]


groq_session = requests.Session()  # ulanishni qayta ishlatadi (har safar yangi TLS emas)
GROQ_MODELS = get_groq_models()
models_lock = threading.Lock()


# --- BAZA (javob berilgan xabar/izohlar) ---
def load_db():
    data = {"comments": [], "messages": []}
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception as e:
            print(f"[DB] {DB_FILE} o'qilmadi, yangidan boshlanadi: {e}")
    return {
        "comments": list(dict.fromkeys(str(x) for x in data.get("comments", []))),
        "messages": list(dict.fromkeys(str(x) for x in data.get("messages", []))),
    }


db_lock = threading.Lock()
_db_data = load_db()
replied = {
    "comments": set(_db_data["comments"]),
    "messages": set(_db_data["messages"]),
}
_replied_order = {k: list(v) for k, v in _db_data.items()}


def is_replied(kind, item_id):
    with db_lock:
        return str(item_id) in replied[kind]


def mark_replied(kind, item_id):
    item_id = str(item_id)
    with db_lock:
        if item_id in replied[kind]:
            return
        replied[kind].add(item_id)
        order = _replied_order[kind]
        order.append(item_id)
        if len(order) > DB_MAX_IDS:
            for old in order[:-DB_MAX_IDS]:
                replied[kind].discard(old)
            del order[:-DB_MAX_IDS]
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_replied_order, f)
        os.replace(tmp, DB_FILE)


def load_account_config(username):
    """Dashboard (app.py) da qilingan o'zgarishlar botni qayta ishga tushirmasdan qo'llanadi."""
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            for acc in json.load(f):
                if acc.get("username") == username:
                    return acc
    except Exception:
        pass
    return None


def get_business_info(acc):
    # app.py "knowledge" kalitini, eski format "business_info" ni ishlatadi
    return acc.get("knowledge") or acc.get("business_info") or ""


# 2. AVTOMATIK JAVOBLAR (AI)
def call_groq(system_prompt, user_prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    with models_lock:
        models = list(GROQ_MODELS)

    for model_name in models:
        data = {
            "model": model_name,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": 0.6,
            "max_tokens": 250,
        }
        try:
            res = groq_session.post(url, headers=headers, json=data, timeout=15)
            if res.status_code == 200:
                reply = (res.json()["choices"][0]["message"].get("content") or "").strip()
                if "</think>" in reply:
                    reply = reply.split("</think>")[-1].strip()
                elif "<think>" in reply:
                    continue  # fikrlash tugamay kesilgan — keyingi modelga
                reply = reply.replace("**", "").strip().strip('"').strip()
                if reply:
                    return reply
            elif res.status_code in (400, 404):
                with models_lock:
                    if model_name in GROQ_MODELS and len(GROQ_MODELS) > 1:
                        GROQ_MODELS.remove(model_name)
            elif res.status_code == 429:
                print(f"[AI] {model_name} limitga yetdi, keyingi model...")
            else:
                print(f"[AI] {model_name} xato {res.status_code}: {res.text[:150]}")
        except Exception as e:
            print(f"[AI] {model_name} so'rovida xato: {e}")
    return None


def get_dm_reply(business_info, behavior, user_text):
    system_prompt = (
        f"{behavior}\nSen ushbu biznesning shaxsiy menejerisan. Biznes ma'lumotlari:\n{business_info}\n"
        "Qisqa, lo'nda (1-2 gap) va 2 ta emoji bilan javob yoz."
    )
    return call_groq(system_prompt, f"Mijoz DM da yozdi: '{user_text}'")


def get_comment_and_dm_reply(business_info, behavior, user_text):
    system_prompt = (
        f"{behavior}\nBiznes ma'lumotlari:\n{business_info}\n"
        "Mijoz izohiga 2 ta qisqa javob tayyorla va ularni ||| bilan ajrat (1-izoh uchun, 2-DM uchun)."
    )
    res = call_groq(system_prompt, f"Mijoz Reels ostiga izoh yozdi: '{user_text}'")
    if res and "|||" in res:
        parts = res.split("|||")
        comment_part, dm_part = parts[0].strip(), parts[1].strip()
        if comment_part and dm_part:
            return comment_part, dm_part
        res = comment_part or dm_part
    if res:
        return "Rahmat! 😊 Lichkaga yozdik 📩", res
    return None, None


# 3. AKKAUNTGA KIRISH
def login_account(username, session_id):
    """Dashboard'da "password" maydoniga Session ID (cookie) yoziladi."""
    cl = Client()
    cl.delay_range = REQUEST_DELAY
    session_file = f"session_{username}.json"

    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            # Eslatma: cl.login(username, session_id) chaqirilmaydi — session ID parol emas,
            # noto'g'ri parol bilan qayta login katta akkauntlarda "challenge"/blok chaqiradi.
            cl.account_info()
            print(f"[{username}] ✅ Sessiya orqali muvaffaqiyatli kirildi! ⚡")
            return cl
        except Exception as e:
            print(f"[{username}] ⚠️ Saqlangan sessiya ishlamadi ({e}), Session ID bilan qayta ulanilmoqda...")
            cl = Client()
            cl.delay_range = REQUEST_DELAY

    try:
        cl.login_by_sessionid(session_id.strip())
        cl.dump_settings(session_file)
        print(f"[{username}] ✅ Session ID orqali muvaffaqiyatli ulandi! ⚡")
        return cl
    except Exception as e:
        print(f"[{username}] ❌ Login xatoligi: {e}")
        return None


# --- DM YORDAMCHI FUNKSIYALAR ---
# instagrapi'ning direct_threads() funksiyasi har bir xabarni qat'iy (pydantic) model bilan
# tekshiradi. Katta akkauntlarda (reels ulashish, ovozli xabar, story javobi, xma va h.k.)
# bitta "g'alati" xabar butun so'rovni ValidationError bilan yiqitadi va bot hech narsa
# ko'rmaydi. Shuning uchun xom (raw) JSON o'zimiz o'qiymiz.
def fetch_inbox_threads(cl, box=None, pending=False):
    if pending:
        params = {
            "visual_message_return_type": "unseen",
            "persistentBadging": "true",
            "is_prefetching": "false",
        }
        result = cl.private_request("direct_v2/pending_inbox/", params=params)
    else:
        params = {
            "visual_message_return_type": "unseen",
            "thread_message_limit": "10",
            "persistentBadging": "true",
            "limit": str(DM_THREADS_PER_BOX),
            "is_prefetching": "false",
            "fetch_reason": "initial_snapshot",
        }
        if box:
            # Biznes/Creator akkauntlarda: 0 = Asosiy (Primary), 1 = Umumiy (General)
            params["folder"] = "1" if box == "general" else "0"
        result = cl.private_request("direct_v2/inbox/", params=params)
    return (result.get("inbox") or {}).get("threads") or []


def fetch_inbox_threads_retry(cl, box=None, pending=False, attempts=3):
    """Vaqtinchalik tarmoq xatolarida (ConnectionError, timeout) bir necha marta qayta urinadi."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch_inbox_threads(cl, box=box, pending=pending)
        except (LoginRequired, PleaseWaitFewMinutes, RateLimitError,
                ChallengeRequired, ClientThrottledError, FeedbackRequired):
            raise
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(3 * attempt)


def extract_item_text(item):
    item_type = item.get("item_type")
    if item_type == "text":
        return item.get("text")
    if item_type == "link":
        return (item.get("link") or {}).get("text")
    # Story'ga javob, reels'ga javob va h.k. — matn alohida maydonda bo'ladi
    for key in ("reel_share", "story_share", "clip_share"):
        data = item.get(key)
        if isinstance(data, dict) and data.get("text"):
            return data.get("text")
    return item.get("text")


def collect_new_user_text(thread, my_user_id):
    """Chatdagi oxirgi (bizdan keyingi) mijoz xabarlarini yig'adi.
    Qaytaradi: (oxirgi_xabar_id, oxirgi_xabar_vaqti_sekund, sender_id, birlashtirilgan_matn)"""
    items = thread.get("items") or []
    if not items:
        return None
    last = items[0]  # eng yangi xabar
    sender_id = str(last.get("user_id", ""))
    if not sender_id or sender_id == my_user_id:
        return None

    texts = []
    for item in items:
        if str(item.get("user_id", "")) == my_user_id:
            break
        text = extract_item_text(item)
        if text:
            texts.append(text.strip())
    if not texts:
        return None
    texts.reverse()

    try:
        ts = int(last.get("timestamp", 0)) / 1_000_000  # mikrosekund -> sekund (UTC epoch)
    except (TypeError, ValueError):
        ts = 0
    return str(last.get("item_id")), ts, sender_id, "\n".join(texts[-5:])


# --- IZOH YORDAMCHI FUNKSIYALAR ---
def fetch_recent_medias(cl, my_user_id):
    """Eng yangi postlar va reels. Katta akkauntlarda tepada 3 ta "pinned" (qadalgan) eski post
    bo'ladi — user_medias(amount=3) faqat o'shalarni qaytarar edi va yangi reels'lar umuman
    tekshirilmas edi. Shuning uchun ko'proq olib, sana bo'yicha saralaymiz."""
    result = cl.private_request(
        f"feed/user/{my_user_id}/",
        params={"count": 15, "rank_token": cl.rank_token, "ranked_content": "true"},
    )
    items = result.get("items") or []
    items = [i for i in items if i.get("pk") and not i.get("comments_disabled")]
    items.sort(key=lambda i: i.get("taken_at", 0), reverse=True)
    return [{"pk": str(i["pk"]), "id": str(i.get("id") or i["pk"])} for i in items[:MEDIA_TO_WATCH]]


def fetch_recent_comments(cl, media_pk):
    """Katta postlarda Instagram izohlarni "mashhurlik" bo'yicha beradi, yangi izoh birinchi 5 ta
    ichida bo'lmaydi. Shuning uchun birinchi sahifani to'liq olib, vaqt bo'yicha saralaymiz."""
    result = cl.private_request(
        f"media/{media_pk}/comments/",
        params={"can_support_threading": "true", "sort_order": "newest"},
    )
    comments = result.get("comments") or []
    comments.sort(key=lambda c: c.get("created_at_utc") or c.get("created_at") or 0, reverse=True)
    return comments


# 4. ASOSIY ISHCHI FUNKSIYA
def run_bot_for_account(acc):
    username = acc["username"]
    session_id = acc["password"]

    cl = login_account(username, session_id)
    if not cl:
        return

    my_user_id = str(cl.user_id)
    # Vaqtni UTC epoch (sekund) da saqlaymiz — timezone muammolari bo'lmaydi.
    # (instagrapi DM vaqtini lokal vaqtda, izoh vaqtini UTC da qaytarar edi — solishtirish noto'g'ri edi)
    bot_start_ts = time.time()

    medias_cache = []
    next_pending_ts = 0
    next_comments_ts = 0
    medias_cache_ts = 0
    error_streak = 0

    print(f"[{username}] 🚀 Bot ishga tushdi! (Asosiy, Umumiy va So'rovlar jildlari kuzatilmoqda...)")

    while True:
        try:
            cfg = load_account_config(username) or acc
            business_info = get_business_info(cfg)
            behavior = cfg.get("behavior") or DEFAULT_BEHAVIOR
            automation = cfg.get("automation") or {}
            auto_dm = automation.get("auto_dm", True)
            auto_comment = automation.get("auto_comment", True)
            replies_this_cycle = 0

            # --- A. BARCHA DIRECT (DM) XABARLARNI TEKSHIRISH ---
            if auto_dm:
                threads = []  # (thread, pending)
                seen_thread_ids = set()
                sources = [("primary", False), ("general", False)]
                if time.time() >= next_pending_ts:
                    sources.append((None, True))
                    next_pending_ts = time.time() + PENDING_INTERVAL
                for box, pending in sources:
                    try:
                        for t in fetch_inbox_threads_retry(cl, box=box, pending=pending):
                            tid = str(t.get("thread_id", ""))
                            if tid and tid not in seen_thread_ids:
                                seen_thread_ids.add(tid)
                                threads.append((t, pending))
                    except (LoginRequired, PleaseWaitFewMinutes, RateLimitError,
                            ChallengeRequired, ClientThrottledError):
                        raise
                    except Exception as e:
                        name = "So'rovlar" if pending else box
                        print(f"[{username}] ⚠️ DM ({name}) jildini o'qishda xato: {e}")

                for thread, pending in threads:
                    if replies_this_cycle >= MAX_REPLIES_PER_CYCLE:
                        break
                    info = collect_new_user_text(thread, my_user_id)
                    if not info:
                        continue
                    msg_id, msg_ts, sender_id, user_text = info
                    if msg_ts < bot_start_ts or is_replied("messages", msg_id):
                        continue

                    thread_id = str(thread.get("thread_id"))
                    print(f"[{username}] 📩 Yangi xabar (ID: {sender_id}): {user_text}")

                    ai_reply = get_dm_reply(business_info, behavior, user_text)
                    if not ai_reply:
                        print(f"[{username}] ⚠️ AI javob bermadi, keyingi aylanishda qayta urinadi.")
                        continue

                    if pending:
                        try:
                            cl.direct_pending_approve(int(thread_id))
                        except Exception as e:
                            print(f"[{username}] ⚠️ So'rovni qabul qilishda xato: {e}")

                    sent = False
                    try:
                        cl.direct_answer(int(thread_id), ai_reply)
                        sent = True
                    except (LoginRequired, PleaseWaitFewMinutes, RateLimitError, FeedbackRequired,
                            ChallengeRequired, ClientThrottledError):
                        raise
                    except Exception:
                        try:
                            cl.direct_send(ai_reply, user_ids=[int(sender_id)])
                            sent = True
                        except Exception as de:
                            print(f"[{username}] ❌ Javob yozishda xato: {de}")

                    if sent:
                        print(f"[{username}] ✅ Javob yuborildi: {ai_reply}")
                        mark_replied("messages", msg_id)
                        replies_this_cycle += 1
                        time.sleep(random.uniform(*ACTION_DELAY))

            # --- B. REELS VA POST IZOHLARI ---
            if (auto_comment and replies_this_cycle < MAX_REPLIES_PER_CYCLE
                    and time.time() >= next_comments_ts):
                next_comments_ts = time.time() + COMMENTS_INTERVAL
                if not medias_cache or time.time() - medias_cache_ts > MEDIA_REFRESH_SEC:
                    try:
                        medias_cache = fetch_recent_medias(cl, my_user_id)
                        medias_cache_ts = time.time()
                    except (LoginRequired, PleaseWaitFewMinutes, RateLimitError,
                            ChallengeRequired, ClientThrottledError):
                        raise
                    except Exception as e:
                        print(f"[{username}] ⚠️ Postlarni olishda xato: {e}")

                for media in medias_cache:
                    if replies_this_cycle >= MAX_REPLIES_PER_CYCLE:
                        break
                    try:
                        comments = fetch_recent_comments(cl, media["pk"])
                    except (LoginRequired, PleaseWaitFewMinutes, RateLimitError,
                            ChallengeRequired, ClientThrottledError):
                        raise
                    except Exception as e:
                        print(f"[{username}] ⚠️ Izohlarni olishda xato ({media['pk']}): {e}")
                        continue

                    for comment in comments:
                        if replies_this_cycle >= MAX_REPLIES_PER_CYCLE:
                            break
                        comment_id = str(comment.get("pk") or comment.get("id") or "")
                        user = comment.get("user") or {}
                        commenter_id = str(user.get("pk") or user.get("id") or comment.get("user_id") or "")
                        commenter_name = user.get("username", "")
                        text = (comment.get("text") or "").strip()
                        created = comment.get("created_at_utc") or comment.get("created_at") or 0

                        if (not comment_id or not text or commenter_id == my_user_id
                                or created < bot_start_ts or is_replied("comments", comment_id)):
                            continue

                        print(f"[{username}] 💬 Yangi izoh (@{commenter_name}): {text}")
                        comment_reply, dm_reply = get_comment_and_dm_reply(business_info, behavior, text)
                        if not (comment_reply and dm_reply):
                            print(f"[{username}] ⚠️ AI javob bermadi, keyingi aylanishda qayta urinadi.")
                            continue

                        try:
                            cl.media_comment(
                                media["id"],
                                f"@{commenter_name} {comment_reply}" if commenter_name else comment_reply,
                                replied_to_comment_id=int(comment_id),
                            )
                            print(f"[{username}] ✅ Izohga javob: {comment_reply}")
                        except (LoginRequired, PleaseWaitFewMinutes, RateLimitError, FeedbackRequired,
                                ChallengeRequired, ClientThrottledError):
                            raise
                        except Exception as ce:
                            print(f"[{username}] ❌ Izoh yozishda xato: {ce}")

                        if commenter_id:
                            try:
                                cl.direct_send(dm_reply, user_ids=[int(commenter_id)])
                                print(f"[{username}] ✅ DM yuborildi: {dm_reply}")
                            except (LoginRequired, PleaseWaitFewMinutes, RateLimitError, FeedbackRequired,
                                    ChallengeRequired, ClientThrottledError):
                                raise
                            except Exception as de:
                                print(f"[{username}] ❌ DM yuborishda xato: {de}")

                        mark_replied("comments", comment_id)
                        replies_this_cycle += 1
                        time.sleep(random.uniform(*ACTION_DELAY))

            error_streak = 0

        except LoginRequired as e:
            print(f"[{username}] 🔑 Sessiya tugadi ({e}). Qayta ulanilmoqda...")
            new_cl = login_account(username, session_id)
            if new_cl:
                cl = new_cl
                my_user_id = str(cl.user_id)
            else:
                print(f"[{username}] ❌ Qayta ulanib bo'lmadi. Dashboard'da yangi Session ID kiriting. 10 daqiqa kutiladi.")
                time.sleep(600)
        except ChallengeRequired as e:
            print(f"[{username}] 🛑 Instagram tasdiqlash (challenge) so'radi: {e}. "
                  f"Telefonda Instagram'ga kirib tasdiqlang. 15 daqiqa kutiladi.")
            time.sleep(900)
        except (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired, ClientThrottledError) as e:
            error_streak += 1
            wait = min(300 * error_streak, 1800)
            print(f"[{username}] ⏳ Instagram cheklov qo'ydi ({type(e).__name__}: {e}). {wait // 60} daqiqa kutiladi.")
            time.sleep(wait)
        except Exception as e:
            # Hech qanday xato bot oqimini o'ldirmasligi kerak
            print(f"[{username}] ❌ Kutilmagan xato: {type(e).__name__}: {e}")
            time.sleep(30)

        # Bloklanmaslik uchun tasodifiy pauza
        time.sleep(random.uniform(*DM_POLL_DELAY))


if __name__ == "__main__":
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"❌ {ACCOUNTS_FILE} topilmadi. accounts.example.json dan nusxa oling yoki app.py orqali akkaunt qo'shing.")
        raise SystemExit(1)
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    threads = []
    for acc in accounts:
        t = threading.Thread(target=run_bot_for_account, args=(acc,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(2)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
