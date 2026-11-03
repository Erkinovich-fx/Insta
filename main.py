import json
import os
import time
import threading
import random
import requests
from datetime import datetime, timezone
from instagrapi import Client

# 1. GROQ AI SOZLAMASI
GROQ_API_KEY = "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"

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
            preferred = []
            for keyword in ["llama", "qwen", "deepseek", "gemma", "mixtral"]:
                for m in chat_models:
                    if keyword in m.lower() and m not in preferred:
                        preferred.append(m)
            for m in chat_models:
                if m not in preferred:
                    preferred.append(m)
            return preferred
    except Exception:
        pass
    return ["llama-3.3-70b-versatile"]

GROQ_MODELS = get_groq_models()
DB_FILE = "replied_data.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"comments": [], "messages": []}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

db_lock = threading.Lock()
replied_db = load_db()

# 2. AVTOMATIK JAVOBLAR (AI)
def call_groq(system_prompt, user_prompt):
    global GROQ_MODELS
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

    for model_name in list(GROQ_MODELS):
        data = {
            "model": model_name,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": 0.6,
            "max_tokens": 250
        }
        try:
            res = requests.post(url, headers=headers, json=data, timeout=10)
            if res.status_code == 200:
                reply = res.json()["choices"][0]["message"]["content"].strip()
                if "</think>" in reply:
                    reply = reply.split("</think>")[-1].strip()
                return reply.replace("**", "").strip('"')
            elif res.status_code in [400, 404]:
                if model_name in GROQ_MODELS:
                    GROQ_MODELS.remove(model_name)
        except Exception:
            pass
    return None

def get_dm_reply(business_info, user_text):
    system_prompt = f"Sen ushbu biznesning shaxsiy menejerisan. Biznes ma'lumotlari:\n{business_info}\nQisqa, lo'nda va 2 ta emoji bilan javob yoz."
    return call_groq(system_prompt, f"Mijoz DM da yozdi: '{user_text}'")

def get_comment_and_dm_reply(business_info, user_text):
    system_prompt = f"Biznes ma'lumotlari:\n{business_info}\nMijoz izohiga 2 ta qisqa javob tayyorla va ularni ||| bilan ajrat (1-izoh uchun, 2-DM uchun)."
    res = call_groq(system_prompt, f"Mijoz Reels ostiga izoh yozdi: '{user_text}'")
    if res and "|||" in res:
        parts = res.split("|||")
        return parts[0].strip(), parts[1].strip()
    elif res:
        return "Rahmat! 😊 Lichkaga yozdik 📩", res
    return None, None

# 3. AKKAUNTGA KIRISH
def login_account(username, password):
    cl = Client()
    cl.delay_range = [1, 2]
    session_file = f"session_{username}.json"
    
    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            cl.login(username, password)
            print(f"[{username}] ✅ Sessiya orqali muvaffaqiyatli kirildi! ⚡")
            return cl
        except Exception:
            pass

    try:
        cl.login_by_sessionid(password)
        cl.dump_settings(session_file)
        print(f"[{username}] ✅ Session ID orqali muvaffaqiyatli ulandi! ⚡")
        return cl
    except Exception as e:
        print(f"[{username}] ❌ Login xatoligi: {e}")
        return None

# 4. ASOSIY ISHCHI FUNKSIYA
def run_bot_for_account(acc):
    username = acc["username"]
    password = acc["password"]
    business_info = acc["business_info"]

    cl = login_account(username, password)
    if not cl:
        return

    my_user_id = str(cl.user_id)
    
    # MUAMMO HAL QILINDI: Vaqt mintaqalarini mutlaq olib tashlaymiz
    bot_start_time = datetime.now(timezone.utc).replace(tzinfo=None)
    
    print(f"[{username}] 🚀 Bot ishga tushdi! (Asosiy, Umumiy va So'rovlar jildlari kuzatilmoqda...)")

    while True:
        # --- A. BARCHA DIRECT (DM) XABARLARNI TEKSHIRISH ---
        all_threads = []
        
        try:
            all_threads.extend(cl.direct_threads(amount=10)) 
        except Exception:
            pass
            
        try:
            all_threads.extend(cl.direct_pending_inbox(amount=10))
        except Exception:
            pass

        for thread in all_threads:
            if thread.messages:
                last_msg = thread.messages[0]
                msg_id = str(last_msg.id)
                sender_id = str(last_msg.user_id)
                
                # Xabar vaqtini xavfsiz olish va undan ham timezone'ni olib tashlash
                msg_time = getattr(last_msg, 'timestamp', bot_start_time)
                if isinstance(msg_time, datetime) and msg_time.tzinfo is not None:
                    msg_time = msg_time.replace(tzinfo=None)

                # Endi solishtirish 100% bexato ishlaydi
                if msg_time >= bot_start_time and sender_id != my_user_id and msg_id not in replied_db["messages"] and last_msg.text:
                    print(f"[{username}] 📩 Yangi xabar (ID: {sender_id}): {last_msg.text}")
                    
                    ai_reply = get_dm_reply(business_info, last_msg.text)
                    if ai_reply:
                        try:
                            cl.direct_answer(thread.id, ai_reply)
                        except Exception:
                            try:
                                cl.direct_send(ai_reply, user_ids=[int(sender_id)])
                            except Exception as de:
                                print(f"[{username}] ❌ Javob yozishda xato: {de}")

                        print(f"[{username}] ✅ Javob yuborildi: {ai_reply}")

                        with db_lock:
                            replied_db["messages"].append(msg_id)
                            save_db(replied_db)
                        time.sleep(2)

        # --- B. REELS VA POST IZOHLARI ---
        try:
            medias = []
            try:
                medias.extend(cl.user_medias(my_user_id, amount=3))
            except Exception:
                pass
            try:
                clips = cl.user_clips(my_user_id, amount=3)
                existing_ids = {m.id for m in medias}
                for clip in clips:
                    if clip.id not in existing_ids:
                        medias.append(clip)
            except Exception:
                pass

            for media in medias:
                comments = cl.media_comments(media.id, amount=5)
                
                for comment in comments:
                    comment_id = str(comment.pk)
                    commenter_id = str(comment.user.pk)
                    
                    # Izoh vaqtidan ham timezone'ni tozalaymiz
                    comment_time = getattr(comment, 'created_at_utc', bot_start_time)
                    if isinstance(comment_time, datetime) and comment_time.tzinfo is not None:
                        comment_time = comment_time.replace(tzinfo=None)

                    if comment_time >= bot_start_time and commenter_id != my_user_id and comment_id not in replied_db["comments"]:
                        print(f"[{username}] 💬 Yangi izoh (@{comment.user.username}): {comment.text}")
                        
                        comment_reply, dm_reply = get_comment_and_dm_reply(business_info, comment.text)
                        
                        if comment_reply and dm_reply:
                            try:
                                cl.media_comment(
                                    media.id, 
                                    f"@{comment.user.username} {comment_reply}", 
                                    replied_to_comment_id=comment.pk
                                )
                                print(f"[{username}] ✅ Izohga javob: {comment_reply}")
                            except Exception:
                                pass

                            try:
                                cl.direct_send(dm_reply, user_ids=[int(commenter_id)])
                                print(f"[{username}] ✅ DM yuborildi: {dm_reply}")
                            except Exception:
                                pass

                            with db_lock:
                                replied_db["comments"].append(comment_id)
                                save_db(replied_db)
                            
                            time.sleep(3)
        except Exception as e:
            print(f"[Xato - Izohlar]: {e}")

        # 50k akkaunt bloklanmasligi uchun 30 sekundlik pauza
        time.sleep(30)

if __name__ == "__main__":
    with open("accounts.json", "r") as f:
        accounts = json.load(f)

    threads = []
    for acc in accounts:
        t = threading.Thread(target=run_bot_for_account, args=(acc,))
        t.start()
        threads.append(t)
        time.sleep(2)

    for t in threads:
        t.join()
