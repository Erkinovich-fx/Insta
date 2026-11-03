import json
import os
import time
import threading
import random
from instagrapi import Client
from google import genai

# 1. GEMINI AI SOZLAMASI
GEMINI_API_KEY = "AQ.Ab8RN6JqWa10Q5tCFPE0UxM1_dzehCEOMt1uwvPlIGXa1yEDsQ"
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def get_available_models():
    working_models = []
    preferred_order = [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.0-flash"
    ]
    try:
        all_models = [m.name.replace("models/", "") for m in ai_client.models.list() 
                      if "gemini" in m.name and not any(x in m.name for x in ["vision", "embedding", "imagen", "veo", "audio", "tts", "2.5", "2.0"])]
        
        for pref in preferred_order:
            for m in all_models:
                if pref == m and m not in working_models:
                    working_models.append(m)
        for m in all_models:
            if m not in working_models:
                working_models.append(m)
        if working_models:
            print(f"[AI] Tezkor modellar tayyor: {working_models[:4]}")
            return working_models
    except Exception as e:
        print(f"[AI] Model xatosi: {e}")
    return preferred_order

AVAILABLE_MODELS = get_available_models()
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

# 2. TEZKOR VA QISQA JAVOB BERUVCHI AI FUNKSIYASI
def call_gemini(prompt):
    for model_name in list(AVAILABLE_MODELS):
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str:
                if model_name in AVAILABLE_MODELS:
                    AVAILABLE_MODELS.remove(model_name)
            elif "503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str:
                print(f"[AI] {model_name} band, keyingi modelga o'tildi...")
                if model_name in AVAILABLE_MODELS:
                    AVAILABLE_MODELS.remove(model_name)
                    AVAILABLE_MODELS.append(model_name)
            else:
                print(f"[AI Xato]: {e}")
    return None

def get_dm_reply(business_info, user_text):
    prompt = (
        f"Biznes ma'lumoti: {business_info}\n"
        f"Mijoz DM da yozdi: '{user_text}'\n"
        f"QAT'IY QOIDALAR:\n"
        f"1. Juda qisqa, lo'nda va aniq javob yoz (maksimum 1-2 gap).\n"
        f"2. Mijozning gapiga mos tabiiy javob ber.\n"
        f"3. Javobda kamida 2 ta mos emoji ishlat.\n"
        f"4. Yulduzcha (**) kabi belgilarni umuman ishlatma."
    )
    return call_gemini(prompt)

def get_comment_and_dm_reply(business_info, user_text):
    prompt = (
        f"Biznes ma'lumoti: {business_info}\n"
        f"Mijoz Reels ostiga izoh yozdi: '{user_text}'\n"
        f"Menga 2 ta qisqa javob tayyorla va ularni ||| belgisi bilan ajrat:\n"
        f"1-qism (Izohning o'ziga javob): Juda qisqa (3-6 so'z), izoh ma'nosiga mos javob bo'lsin, kamida 2 ta emoji qatnashsin va Direct (lichka) ga qarab qo'yishini aytsin.\n"
        f"2-qism (Direct/DM ga boradigan javob): Juda qisqa va lo'nda (maksimum 1-2 gap!), izohda so'ragan savoliga aniq javob bersin yoki choyxonaga taklif qilsin, kamida 2 ta emoji ishlatsin. Yulduzcha (**) ishlatma.\n"
        f"Namuna: Rahmat! 😊 Lichkaga qarab qo'ying 📩 ||| Assalomu alaykum! Izohingiz uchun rahmat 😊 Biz Osiyo bozoriga yetmasdan, Barsiz yo'lidamiz, 24/7 ochiqmiz 🍵"
    )
    res = call_gemini(prompt)
    if res and "|||" in res:
        parts = res.split("|||")
        return parts[0].strip(), parts[1].strip()
    elif res:
        return "Rahmat! 😊 Direct (lichka) ga ma'lumot yubordik 📩", res
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
            print(f"[{username}] Tezkor rejimda ulandi! ⚡")
            return cl
        except Exception:
            pass

    try:
        cl.login(username, password)
        cl.dump_settings(session_file)
        print(f"[{username}] Yangi login saqlandi! ⚡")
        return cl
    except Exception as e:
        print(f"[{username}] Login xatoligi: {e}")
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

    while True:
        # --- A. DIRECT (DM) XABARLAR ---
        try:
            threads = cl.direct_threads(amount=10, selected_filter="unread")
            for thread in threads:
                if thread.messages:
                    last_msg = thread.messages[0]
                    msg_id = str(last_msg.id)
                    sender_id = str(last_msg.user_id)

                    if sender_id != my_user_id and msg_id not in replied_db["messages"] and last_msg.text:
                        print(f"[{username}] 📩 Yangi DM: {last_msg.text}")
                        
                        ai_reply = get_dm_reply(business_info, last_msg.text)
                        if ai_reply:
                            cl.direct_answer(thread.id, ai_reply)
                            print(f"[{username}] ✅ DM javob: {ai_reply}")

                            with db_lock:
                                replied_db["messages"].append(msg_id)
                                save_db(replied_db)
                            time.sleep(2)
        except Exception as e:
            print(f"[{username}] DM tekshirishda xato: {e}")

        # --- B. REELS VA POST IZOHLARI (CHEKLOVSIZ: amount=0) ---
        try:
            medias = []
            try:
                medias.extend(cl.user_medias(my_user_id, amount=5))
            except Exception:
                pass
            try:
                clips = cl.user_clips(my_user_id, amount=5)
                existing_ids = {m.id for m in medias}
                for clip in clips:
                    if clip.id not in existing_ids:
                        medias.append(clip)
            except Exception:
                pass

            for media in medias:
                # amount=0 qilindi: videodagi BARCHA izohlarni cheklovsiz o'qiydi!
                comments = cl.media_comments(media.id, amount=0)
                
                for comment in comments:
                    comment_id = str(comment.pk)
                    commenter_id = str(comment.user.pk)

                    if commenter_id != my_user_id and comment_id not in replied_db["comments"]:
                        print(f"[{username}] 💬 Yangi izoh (@{comment.user.username}): {comment.text}")
                        
                        comment_reply, dm_reply = get_comment_and_dm_reply(business_info, comment.text)
                        
                        if comment_reply and dm_reply:
                            # 1. Izohning o'ziga javob yozish
                            try:
                                cl.media_comment(
                                    media.id, 
                                    f"@{comment.user.username} {comment_reply}", 
                                    replied_to_comment_id=comment.pk
                                )
                                print(f"[{username}] ✅ Izohga javob yozildi: {comment_reply}")
                            except Exception as ce:
                                print(f"[{username}] ❌ Izoh yozishda xato: {ce}")

                            # 2. Mijozning Direct (DM) iga yozish
                            try:
                                cl.direct_send(dm_reply, user_ids=[int(commenter_id)])
                                print(f"[{username}] ✅ Izoh egasiga DM yuborildi: {dm_reply}")
                            except Exception as de:
                                print(f"[{username}] ❌ Izoh egasiga DM yuborishda xato: {de}")

                            with db_lock:
                                replied_db["comments"].append(comment_id)
                                save_db(replied_db)
                            
                            time.sleep(3)
        except Exception as e:
            print(f"[{username}] Izohlarni tekshirishda xato: {e}")

        time.sleep(random.randint(10, 15))

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
