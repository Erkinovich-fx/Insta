import json
import os
import re
import time
import threading
import random
from collections import deque
from urllib.parse import unquote

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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path):
    """.env faylidan KALIT=QIYMAT qatorlarini o'qiydi (Termux'da har safar export qilish shart emas)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip().strip('"').strip("'")
            if key and not os.environ.get(key):
                os.environ[key] = value


load_dotenv(os.path.join(BASE_DIR, ".env"))

# 1. GROQ AI SOZLAMASI
# Kalitni main.py yonidagi .env fayliga yozing:  GROQ_API_KEY=gsk_...
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()

ACCOUNTS_FILE = "accounts.json"
DB_FILE = "replied_data.json"

# Katta akkauntlar uchun sozlamalar
# Tezlik: DM lar tez-tez, izohlar va so'rovlar kamroq tekshiriladi.
# DM_POLL_DELAY ni juda kichik qilmang — Instagram ko'p so'rovda cheklov (PleaseWait) qo'yadi.
DM_POLL_DELAY = (18, 26)         # Asosiy/Umumiy DM lar har necha sekundda tekshiriladi
PENDING_INTERVAL = 45            # So'rovlar (Requests) jildi har necha sekundda
COMMENTS_INTERVAL = 90           # Izohlar har necha sekundda tekshiriladi
DEEP_SCAN_INTERVAL = 300         # Har 5 daqiqada inbox chuqurroq (bir necha sahifa) o'qiladi
REQUEST_DELAY = [0.3, 1.0]       # Har bir Instagram so'rovidan oldingi pauza (instagrapi)
DM_THREADS_PER_BOX = 50          # Chuqur tekshiruvda har bir jilddan nechta chat olinadi
PENDING_PAGES = 3                # So'rovlar jildidan nechta sahifa (har biri ~20 chat)
MEDIA_TO_WATCH = 10              # Eng yangi nechta post/reels kuzatiladi
MEDIA_REFRESH_SEC = 600          # Postlar ro'yxati har 10 daqiqada yangilanadi
COMMENT_PAGES = 3                # Har bir postdan nechta izoh sahifasi o'qiladi
MAX_REPLIES_PER_CYCLE = 16       # Bir aylanishda maksimum javoblar (spam-blokdan himoya)
MAX_REPLIES_PER_HOUR = 120       # Bir akkaunt uchun soatiga maksimum javob
ACTION_DELAY = (2, 5)            # Har bir javobdan keyingi pauza
DB_MAX_IDS = 20000               # Bazada saqlanadigan maksimum ID lar

# Eski (bot ishga tushishidan oldingi) xabar/izohlarga ham javob berish.
# 0 = faqat bot ishga tushgandan keyin kelganlariga javob beradi.
DM_MAX_AGE_HOURS = 48            # Javobsiz qolgan DM / so'rovlarga necha soatgacha javob beriladi
COMMENT_MAX_AGE_HOURS = 720      # Izohlarga necha soatgacha (720 = 30 kun) javob beriladi

DEFAULT_BEHAVIOR = (
    "Sen juda xushmuomala yordamchisan. Mijoz qaysi tilda yozsa, o'sha tilda javob qaytar "
    "(asosan O'zbek tilida). Qisqa, lo'nda va kamida 2 ta emoji ishlat."
)

# Instagram cheklov/sessiya xatolari — bular yuqoriga uzatiladi va asosiy siklda qayta ishlanadi
IG_FATAL_ERRORS = (LoginRequired, PleaseWaitFewMinutes, RateLimitError,
                   ChallengeRequired, ClientThrottledError, FeedbackRequired)

FAST_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

ai_status = {"key_invalid": False, "last_warn": 0}
ai_lock = threading.Lock()


def warn_bad_key(detail=""):
    """API kalit xatosini har 5 daqiqada bir marta ko'rsatadi (log to'lib ketmasligi uchun)."""
    with ai_lock:
        ai_status["key_invalid"] = True
        if time.time() - ai_status["last_warn"] < 300:
            return
        ai_status["last_warn"] = time.time()
    print(
        "\n[AI] ❌ GROQ API KALIT NOTO'G'RI yoki YO'Q — hech bir akkaunt AI javob bera olmaydi!"
        f"{(' (' + detail + ')') if detail else ''}\n"
        "     1) https://console.groq.com/keys dan yangi kalit oling\n"
        f"     2) {os.path.join(BASE_DIR, '.env')} fayliga yozing:  GROQ_API_KEY=gsk_...\n"
        "     3) Botni qayta ishga tushiring.\n"
    )


def get_groq_models():
    if not GROQ_API_KEY:
        warn_bad_key("GROQ_API_KEY topilmadi")
        return list(FAST_MODELS)
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 401:
            warn_bad_key("Groq 401 Invalid API Key")
        elif res.status_code == 200:
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
                print(f"[AI] ✅ Groq kalit ishlayapti. Asosiy model: {preferred[0]}")
                return preferred
    except Exception as e:
        print(f"[AI] Modellar ro'yxatini olishda xato: {e}")
    return list(FAST_MODELS)


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
    if not GROQ_API_KEY:
        warn_bad_key("GROQ_API_KEY topilmadi")
        return None

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
                ai_status["key_invalid"] = False
                reply = (res.json()["choices"][0]["message"].get("content") or "").strip()
                if "</think>" in reply:
                    reply = reply.split("</think>")[-1].strip()
                elif "<think>" in reply:
                    continue  # fikrlash tugamay kesilgan — keyingi modelga
                reply = reply.replace("**", "").strip().strip('"').strip()
                if reply:
                    return reply
            elif res.status_code in (401, 403):
                # Kalit noto'g'ri — boshqa modellarni sinash befoyda
                warn_bad_key(f"Groq {res.status_code}: {res.text[:100]}")
                return None
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
def session_user_id(session_id):
    """Session ID ning boshidagi raqam — shu akkauntning Instagram ID si (masalan 24419371704%3A...)."""
    m = re.match(r"^\d+", unquote(session_id or "").strip())
    return m.group() if m else ""


def new_client():
    cl = Client()
    cl.delay_range = REQUEST_DELAY
    return cl


def whoami(cl):
    """Haqiqatda qaysi akkauntga ulanganimizni qaytaradi (username, id)."""
    result = cl.private_request("accounts/current_user/", params={"edit": "true"})
    user = result.get("user") or {}
    return user.get("username", ""), str(user.get("pk") or user.get("pk_id") or cl.user_id)


def login_account(username, session_id):
    """Dashboard'da "password" maydoniga Session ID (cookie) yoziladi.
    accounts.json dagi Session ID har doim asosiy hisoblanadi: agar saqlangan session_*.json
    boshqa Session ID / boshqa akkauntga tegishli bo'lsa, u tashlab yuboriladi."""
    session_id = (session_id or "").strip()
    expected_uid = session_user_id(session_id)
    session_file = f"session_{username}.json"
    cl = None

    if os.path.exists(session_file):
        try:
            cl = new_client()
            cl.load_settings(session_file)
            auth = cl.authorization_data or {}
            saved_uid = str(auth.get("ds_user_id") or "")
            saved_sid = unquote(str(auth.get("sessionid") or ""))
            if expected_uid and saved_uid and saved_uid != expected_uid:
                print(f"[{username}] ⚠️ {session_file} BOSHQA akkauntga tegishli (ID {saved_uid}), "
                      f"accounts.json dagi Session ID esa ID {expected_uid} uchun. Eski fayl tashlab yuborildi.")
                cl = None
            elif saved_sid and session_id and saved_sid != unquote(session_id):
                print(f"[{username}] ℹ️ accounts.json da yangi Session ID — o'shani ishlatamiz.")
                cl = None
            else:
                # Eslatma: cl.login(username, session_id) chaqirilmaydi — session ID parol emas.
                whoami(cl)
        except Exception as e:
            print(f"[{username}] ⚠️ Saqlangan sessiya ishlamadi ({e}), Session ID bilan qayta ulanilmoqda...")
            cl = None

    if cl is None:
        try:
            cl = new_client()
            cl.login_by_sessionid(session_id)
            cl.dump_settings(session_file)
        except Exception as e:
            print(f"[{username}] ❌ Login xatoligi: {e}")
            print(f"[{username}]    Session ID eskirgan bo'lishi mumkin — brauzerdan yangi sessionid oling.")
            return None

    try:
        real_username, real_id = whoami(cl)
    except Exception:
        real_username, real_id = "", str(cl.user_id)
    print(f"[{username}] ✅ Ulandi: @{real_username or '?'} (ID: {real_id}) ⚡")
    if real_username and real_username.lower() != username.lower():
        print(f"[{username}] ⚠️ DIQQAT: Session ID @{real_username} akkauntiga tegishli, @{username} ga emas! "
              f"Bot @{real_username} nomidan ishlaydi. To'g'ri akkauntning Session ID sini kiriting.")
    return cl


# --- DM YORDAMCHI FUNKSIYALAR ---
# instagrapi'ning direct_threads() funksiyasi har bir xabarni qat'iy (pydantic) model bilan
# tekshiradi. Katta akkauntlarda (reels ulashish, ovozli xabar, story javobi, xma va h.k.)
# bitta "g'alati" xabar butun so'rovni ValidationError bilan yiqitadi va bot hech narsa
# ko'rmaydi. Shuning uchun xom (raw) JSON o'zimiz o'qiymiz.
def fetch_inbox_threads(cl, box=None, pending=False, max_threads=20):
    threads = []
    cursor = None
    while True:
        if pending:
            params = {
                "visual_message_return_type": "unseen",
                "persistentBadging": "true",
                "is_prefetching": "false",
                "request_session_id": cl.request_id,
            }
            if cursor:
                params["cursor"] = cursor
            result = cl.private_request("direct_v2/pending_inbox/", params=params)
        else:
            params = {
                "visual_message_return_type": "unseen",
                "thread_message_limit": "10",
                "persistentBadging": "true",
                "limit": "20",
                "is_prefetching": "false",
                "fetch_reason": "initial_snapshot",
            }
            if box:
                # Biznes/Creator akkauntlarda: 0 = Asosiy (Primary), 1 = Umumiy (General)
                params["folder"] = "1" if box == "general" else "0"
            if cursor:
                params.update({"cursor": cursor, "direction": "older", "fetch_reason": "page_scroll"})
            result = cl.private_request("direct_v2/inbox/", params=params)

        inbox = result.get("inbox") or {}
        threads.extend(inbox.get("threads") or [])
        cursor = inbox.get("oldest_cursor")
        if not cursor or not inbox.get("has_older", True) or len(threads) >= max_threads:
            break
    return threads[:max_threads]


def fetch_inbox_threads_retry(cl, box=None, pending=False, max_threads=20, attempts=3):
    """Vaqtinchalik tarmoq xatolarida (ConnectionError, timeout) bir necha marta qayta urinadi."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch_inbox_threads(cl, box=box, pending=pending, max_threads=max_threads)
        except IG_FATAL_ERRORS:
            raise
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(3 * attempt)


# Matnsiz xabarlar (reels/post ulashish, rasm, ovozli xabar...) — AI ga tushunarli tavsif
MEDIA_ITEM_LABELS = {
    "media_share": "post ulashdi",
    "clip": "reels ulashdi",
    "xma_clip": "reels ulashdi",
    "xma_media_share": "post ulashdi",
    "xma_reel_share": "reels ulashdi",
    "story_share": "story ulashdi",
    "xma_story_share": "story ulashdi",
    "reel_share": "story'ga javob yozdi",
    "media": "rasm/video yubordi",
    "raven_media": "rasm/video yubordi",
    "voice_media": "ovozli xabar yubordi",
    "animated_media": "GIF yubordi",
    "felix_share": "video ulashdi",
    "profile": "profil ulashdi",
}


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


def thread_items(thread):
    items = thread.get("items") or []
    if not items and isinstance(thread.get("last_permanent_item"), dict):
        # So'rovlar (pending) jildida ba'zan faqat oxirgi xabar keladi
        items = [thread["last_permanent_item"]]
    return items


def collect_new_user_text(thread, my_user_id):
    """Chatdagi oxirgi (bizdan keyingi) mijoz xabarlarini yig'adi.
    Qaytaradi: (oxirgi_xabar_id, oxirgi_xabar_vaqti_sekund, sender_id, birlashtirilgan_matn)"""
    items = thread_items(thread)
    if not items:
        return None
    last = items[0]  # eng yangi xabar
    sender_id = str(last.get("user_id", ""))
    if not sender_id or sender_id == my_user_id:
        return None

    texts = []
    media_note = None
    for item in items:
        if str(item.get("user_id", "")) == my_user_id:
            break
        text = extract_item_text(item)
        if text:
            texts.append(text.strip())
        elif media_note is None and item.get("item_type") in MEDIA_ITEM_LABELS:
            media_note = f"(Mijoz {MEDIA_ITEM_LABELS[item['item_type']]})"
    if not texts and media_note:
        texts = [media_note]
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
    bo'ladi — shuning uchun ko'proq olib, sana bo'yicha saralaymiz."""
    result = cl.private_request(
        f"feed/user/{my_user_id}/",
        params={"count": max(15, MEDIA_TO_WATCH + 5), "rank_token": cl.rank_token, "ranked_content": "true"},
    )
    items = result.get("items") or []
    items = [i for i in items if i.get("pk") and not i.get("comments_disabled")]
    items.sort(key=lambda i: i.get("taken_at", 0), reverse=True)
    return [{"pk": str(i["pk"]), "id": str(i.get("id") or i["pk"])} for i in items[:MEDIA_TO_WATCH]]


def fetch_comments(cl, media_pk):
    """Postdagi izohlar (bir necha sahifa). Katta postlarda Instagram izohlarni "mashhurlik"
    bo'yicha beradi, shuning uchun ko'proq olib, keyin vaqt bo'yicha saralaymiz."""
    comments = []
    params = {"can_support_threading": "true", "sort_order": "newest"}
    for _ in range(COMMENT_PAGES):
        result = cl.private_request(f"media/{media_pk}/comments/", params=params)
        comments.extend(result.get("comments") or [])
        if result.get("has_more_comments") and result.get("next_max_id"):
            params = {"can_support_threading": "true", "max_id": result["next_max_id"]}
        elif result.get("has_more_headload_comments") and result.get("next_min_id"):
            params = {"can_support_threading": "true", "min_id": result["next_min_id"]}
        else:
            break
    return comments


def _c_user_id(c):
    user = c.get("user") or {}
    return str(user.get("pk") or user.get("id") or c.get("user_id") or "")


def _c_time(c):
    return c.get("created_at_utc") or c.get("created_at") or 0


def build_comment_candidates(comments, my_user_id):
    """Javob berilishi kerak bo'lgan izohlar: asosiy izohlar + ularga yozilgan javoblar (child).
    Biz allaqachon javob yozgan izohlar o'tkazib yuboriladi.
    Qaytaradi: [(izoh, javob_yoziladigan_asosiy_izoh_pk), ...] — eng yangisi birinchi."""
    seen = set()
    out = []
    for c in comments:
        top_pk = str(c.get("pk") or c.get("id") or "")
        if not top_pk or top_pk in seen:
            continue
        seen.add(top_pk)
        children = c.get("preview_child_comments") or []
        my_reply_times = [_c_time(ch) for ch in children if _c_user_id(ch) == my_user_id]

        if _c_user_id(c) != my_user_id and not my_reply_times:
            out.append((c, top_pk))
        for ch in children:
            ch_pk = str(ch.get("pk") or ch.get("id") or "")
            if not ch_pk or ch_pk in seen or _c_user_id(ch) == my_user_id:
                continue
            seen.add(ch_pk)
            if any(t >= _c_time(ch) for t in my_reply_times):
                continue  # bu javobdan keyin biz allaqachon yozganmiz
            out.append((ch, top_pk))  # Instagram'da javob har doim asosiy izoh ostiga yoziladi
    out.sort(key=lambda pair: _c_time(pair[0]), reverse=True)
    return out


# 4. ASOSIY ISHCHI FUNKSIYA
def run_bot_for_account(acc):
    username = acc["username"]
    session_id = acc["password"]

    cl = login_account(username, session_id)
    if not cl:
        return

    my_user_id = str(cl.user_id)
    # Vaqtni UTC epoch (sekund) da saqlaymiz — timezone muammolari bo'lmaydi.
    bot_start_ts = time.time()

    def dm_cutoff():
        return time.time() - DM_MAX_AGE_HOURS * 3600 if DM_MAX_AGE_HOURS > 0 else bot_start_ts

    def comment_cutoff():
        return time.time() - COMMENT_MAX_AGE_HOURS * 3600 if COMMENT_MAX_AGE_HOURS > 0 else bot_start_ts

    sent_times = deque()  # soatlik limit uchun

    def can_reply(replies_this_cycle):
        now = time.time()
        while sent_times and now - sent_times[0] > 3600:
            sent_times.popleft()
        return replies_this_cycle < MAX_REPLIES_PER_CYCLE and len(sent_times) < MAX_REPLIES_PER_HOUR

    medias_cache = []
    next_pending_ts = 0
    next_comments_ts = 0
    next_deep_scan_ts = 0
    medias_cache_ts = 0
    error_streak = 0
    last_pending_count = None
    hour_limit_warned = 0

    print(f"[{username}] 🚀 Bot ishga tushdi! (Asosiy, Umumiy va So'rovlar jildlari kuzatilmoqda...)")

    while True:
        try:
            cfg = load_account_config(username) or acc
            if cfg.get("password") and cfg["password"] != session_id:
                # Dashboard'da Session ID yangilangan — qayta ulanamiz
                print(f"[{username}] 🔄 accounts.json da yangi Session ID, qayta ulanilmoqda...")
                new_cl = login_account(username, cfg["password"])
                if new_cl:
                    cl, session_id = new_cl, cfg["password"]
                    my_user_id = str(cl.user_id)
            business_info = get_business_info(cfg)
            behavior = cfg.get("behavior") or DEFAULT_BEHAVIOR
            automation = cfg.get("automation") or {}
            auto_dm = automation.get("auto_dm", True)
            auto_comment = automation.get("auto_comment", True)
            replies_this_cycle = 0

            if ai_status["key_invalid"]:
                warn_bad_key()

            if not can_reply(0) and time.time() - hour_limit_warned > 600:
                hour_limit_warned = time.time()
                print(f"[{username}] ⏸ Soatlik limit ({MAX_REPLIES_PER_HOUR} ta javob) tugadi, biroz kutiladi.")

            # --- A. BARCHA DIRECT (DM) XABARLARNI TEKSHIRISH ---
            if auto_dm and can_reply(0):
                deep = time.time() >= next_deep_scan_ts
                if deep:
                    next_deep_scan_ts = time.time() + DEEP_SCAN_INTERVAL
                box_limit = DM_THREADS_PER_BOX if deep else 20

                threads = []  # (thread, pending)
                seen_thread_ids = set()
                sources = [("primary", False), ("general", False)]
                if time.time() >= next_pending_ts:
                    sources.append((None, True))
                    next_pending_ts = time.time() + PENDING_INTERVAL
                for box, pending in sources:
                    limit = PENDING_PAGES * 20 if pending else box_limit
                    try:
                        found = fetch_inbox_threads_retry(cl, box=box, pending=pending, max_threads=limit)
                    except IG_FATAL_ERRORS:
                        raise
                    except Exception as e:
                        name = "So'rovlar" if pending else box
                        print(f"[{username}] ⚠️ DM ({name}) jildini o'qishda xato: {type(e).__name__}: {e}")
                        continue
                    if pending and len(found) != last_pending_count:
                        last_pending_count = len(found)
                        print(f"[{username}] 📥 So'rovlar (Requests) jildida {len(found)} ta chat.")
                    for t in found:
                        tid = str(t.get("thread_id", ""))
                        if tid and tid not in seen_thread_ids:
                            seen_thread_ids.add(tid)
                            threads.append((t, pending))

                # Eng yangi xabarlar birinchi
                candidates = []
                cutoff = dm_cutoff()
                for thread, pending in threads:
                    info = collect_new_user_text(thread, my_user_id)
                    if not info:
                        continue
                    msg_id, msg_ts, sender_id, user_text = info
                    if msg_ts < cutoff or is_replied("messages", msg_id):
                        continue
                    candidates.append((msg_ts, thread, pending, info))
                candidates.sort(key=lambda x: x[0], reverse=True)

                for _, thread, pending, (msg_id, msg_ts, sender_id, user_text) in candidates:
                    if not can_reply(replies_this_cycle):
                        break
                    thread_id = str(thread.get("thread_id"))
                    where = " [So'rov]" if pending else ""
                    print(f"[{username}] 📩 Yangi xabar{where} (ID: {sender_id}): {user_text}")

                    ai_reply = get_dm_reply(business_info, behavior, user_text)
                    if not ai_reply:
                        print(f"[{username}] ⚠️ AI javob bermadi, keyingi aylanishda qayta urinadi.")
                        if ai_status["key_invalid"]:
                            break  # kalit noto'g'ri — boshqa xabarlarni ham sinash befoyda
                        continue

                    if pending:
                        try:
                            cl.direct_pending_approve(int(thread_id))
                        except IG_FATAL_ERRORS:
                            raise
                        except Exception as e:
                            print(f"[{username}] ⚠️ So'rovni qabul qilishda xato: {e}")

                    sent = False
                    try:
                        cl.direct_answer(int(thread_id), ai_reply)
                        sent = True
                    except IG_FATAL_ERRORS:
                        raise
                    except Exception:
                        try:
                            cl.direct_send(ai_reply, user_ids=[int(sender_id)])
                            sent = True
                        except IG_FATAL_ERRORS:
                            raise
                        except Exception as de:
                            print(f"[{username}] ❌ Javob yozishda xato: {de}")

                    if sent:
                        print(f"[{username}] ✅ Javob yuborildi: {ai_reply}")
                        mark_replied("messages", msg_id)
                        replies_this_cycle += 1
                        sent_times.append(time.time())
                        time.sleep(random.uniform(*ACTION_DELAY))

            # --- B. REELS VA POST IZOHLARI ---
            if (auto_comment and can_reply(replies_this_cycle)
                    and not ai_status["key_invalid"]
                    and time.time() >= next_comments_ts):
                next_comments_ts = time.time() + COMMENTS_INTERVAL
                if not medias_cache or time.time() - medias_cache_ts > MEDIA_REFRESH_SEC:
                    try:
                        medias_cache = fetch_recent_medias(cl, my_user_id)
                        medias_cache_ts = time.time()
                    except IG_FATAL_ERRORS:
                        raise
                    except Exception as e:
                        print(f"[{username}] ⚠️ Postlarni olishda xato: {e}")

                cutoff = comment_cutoff()
                for media in medias_cache:
                    if not can_reply(replies_this_cycle):
                        break
                    try:
                        comments = fetch_comments(cl, media["pk"])
                    except IG_FATAL_ERRORS:
                        raise
                    except Exception as e:
                        print(f"[{username}] ⚠️ Izohlarni olishda xato ({media['pk']}): {e}")
                        continue

                    for comment, reply_to_pk in build_comment_candidates(comments, my_user_id):
                        if not can_reply(replies_this_cycle):
                            break
                        comment_id = str(comment.get("pk") or comment.get("id") or "")
                        commenter_id = _c_user_id(comment)
                        commenter_name = (comment.get("user") or {}).get("username", "")
                        text = (comment.get("text") or "").strip()

                        if not text or _c_time(comment) < cutoff or is_replied("comments", comment_id):
                            continue

                        print(f"[{username}] 💬 Yangi izoh (@{commenter_name}): {text}")
                        comment_reply, dm_reply = get_comment_and_dm_reply(business_info, behavior, text)
                        if not (comment_reply and dm_reply):
                            print(f"[{username}] ⚠️ AI javob bermadi, keyingi aylanishda qayta urinadi.")
                            if ai_status["key_invalid"]:
                                break
                            continue

                        try:
                            cl.media_comment(
                                media["id"],
                                f"@{commenter_name} {comment_reply}" if commenter_name else comment_reply,
                                replied_to_comment_id=int(reply_to_pk),
                            )
                            print(f"[{username}] ✅ Izohga javob: {comment_reply}")
                        except IG_FATAL_ERRORS:
                            raise
                        except Exception as ce:
                            print(f"[{username}] ❌ Izoh yozishda xato: {ce}")

                        if commenter_id:
                            try:
                                cl.direct_send(dm_reply, user_ids=[int(commenter_id)])
                                print(f"[{username}] ✅ DM yuborildi: {dm_reply}")
                            except IG_FATAL_ERRORS:
                                raise
                            except Exception as de:
                                print(f"[{username}] ❌ DM yuborishda xato: {de}")

                        mark_replied("comments", comment_id)
                        replies_this_cycle += 1
                        sent_times.append(time.time())
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
