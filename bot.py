# bot.py  — Memory Forever v0.2 (минимальный рабочий)
# Что умеет сейчас:
# - /start: выбор сюжета и формата
# - принимает ОДНО фото (для простоты старта)
# - генерит 5с/10с через Runway (image_to_video, gen4_turbo)
# - вертикаль 720x1280
# - присылает видео пользователю
#
# Что добавим следующим шагом:
# - 2 фото (раздельно), вырезание фона, простая склейка
# - оверлей водяного знака/лого и финальный титр
# - авто-QC (минимальный)
# - вторая вариация (Luma/PixVerse) по кнопке

import os, io, time, base64, uuid, requests
from datetime import datetime
from PIL import Image
import telebot

# ==== КЛЮЧИ ====
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "PUT_YOUR_TELEGRAM_TOKEN_HERE"
RUNWAY_KEY = os.environ.get("RUNWAY_API_KEY") or "PUT_YOUR_RUNWAY_KEY_HERE"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY") or "PUT_YOUR_OPENAI_KEY_HERE"  # пока не используем

if TG_TOKEN.startswith("PUT_YOUR") or RUNWAY_KEY.startswith("PUT_YOUR"):
    print("⚠️ Задай TELEGRAM_BOT_TOKEN и RUNWAY_API_KEY в Tools → Secrets или прямо в коде.")

bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")

# ==== Папки ====
os.makedirs("uploads", exist_ok=True)
os.makedirs("renders", exist_ok=True)

# ==== Кнопки и сценарии ====
SCENES = {
    "🤝 Обнимашки 5с": {"duration": 5, "kind": "hug"},
    "🤝 Обнимашки 10с": {"duration": 10, "kind": "hug"},
    "😘 Щёчка 10с": {"duration": 10, "kind": "kiss_cheek"},
    "👋 Машет рукой 10с": {"duration": 10, "kind": "wave"},
    "🪜 Уходит по лестнице 10с": {"duration": 10, "kind": "stairs"},
}
FORMATS = ["🧍 В рост", "🧍‍♂️ По пояс", "🫱 По грудь"]

# Простой стейт в памяти процесса
user_state = {}  # user_id -> {"format": ..., "scene_key": ..., "photo_path": ...}

# ==== Промпт-банк (минимум) ====
NEG_TAIL = (
    "no extra fingers, no deformed hands, no melting faces, identity preserved, "
    "consistent faces, no background warping, static camera, clean edges"
)

def build_prompt(kind: str, choose_format: str):
    # Уточняем крупность для подсказки (не режем видео, а подсказываем модели)
    if "В рост" in choose_format:
        framing = "full-body shot"
    elif "По пояс" in choose_format:
        framing = "waist-up shot"
    else:
        framing = "chest-up shot"

    if kind == "hug":
        main = "two people start facing the camera, then gently turn toward each other and hug; subtle sway"
    elif kind == "kiss_cheek":
        main = "two people turn toward each other; the parent gives a small kiss on the child's cheek; hold the moment"
    elif kind == "wave":
        main = "one person stands and waves hand goodbye; gentle, heartfelt gesture; calm breathing"
    elif kind == "stairs":
        main = "one person looks back kindly, then slowly walks upstairs into the heavenly light; fade into light at the end"
    else:
        main = "two people gently approach each other and hug"

    # Важно: модель должна сохранять лица и фон из входного кадра
    return (
        f"{main}; {framing}; keep both faces identical to the reference; realistic skin; "
        f"soft light; camera stays still; {NEG_TAIL}"
    )

# ==== Runway API (image_to_video + поллинг задачи) ====
RUNWAY_API = "https://api.dev.runwayml.com/v1"
HEADERS = {
    "Authorization": f"Bearer {RUNWAY_KEY}",
    "X-Runway-Version": "2024-11-06",
    "Content-Type": "application/json",
}

def encode_image_to_datauri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ["jpg", "jpeg"] else "image/png"
    return f"data:{mime};base64,{b64}"

def runway_start(prompt_image_datauri: str, prompt_text: str, duration: int):
    # Вертикаль 720x1280 для Reels/Stories
    payload = {
        "model": "gen4_turbo",
        "promptImage": prompt_image_datauri,
        "promptText": prompt_text,
        "ratio": "720:1280",
        "duration": duration,  # только 5 или 10
    }
    r = requests.post(f"{RUNWAY_API}/image_to_video", headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()  # вернёт id задачи

def runway_poll(task_id: str, timeout_sec=600, every=5):
    # Ждём результата
    started = time.time()
    while True:
        rr = requests.get(f"{RUNWAY_API}/tasks/{task_id}", headers=HEADERS, timeout=60)
        rr.raise_for_status()
        data = rr.json()
        status = data.get("status")
        if status in ("SUCCEEDED", "FAILED", "CANCELED", "ERROR"):
            return data
        if time.time() - started > timeout_sec:
            return {"status": "TIMEOUT", "raw": data}
        time.sleep(every)

def download_file(url: str, save_path: str):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return save_path

# ==== Телеграм-бот ====
@bot.message_handler(commands=["start"])
def cmd_start(m: telebot.types.Message):
    uid = m.from_user.id
    user_state[uid] = {"format": None, "scene_key": None, "photo_path": None}
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    for k in SCENES.keys():
        kb.add(telebot.types.KeyboardButton(k))
    bot.send_message(
        uid,
        "Выберите сюжет (для старта — одно фото):",
        reply_markup=kb
    )
    # формат выбираем отдельно
    kb2 = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3, one_time_keyboard=True)
    for f in FORMATS:
        kb2.add(telebot.types.KeyboardButton(f))
    bot.send_message(uid, "Выберите формат кадра:", reply_markup=kb2)
    bot.send_message(uid, "Теперь пришлите фото (один кадр, анфас).")

@bot.message_handler(content_types=["text"])
def on_text(m: telebot.types.Message):
    uid = m.from_user.id
    st = user_state.setdefault(uid, {"format": None, "scene_key": None, "photo_path": None})

    if m.text in SCENES:
        st["scene_key"] = m.text
        bot.send_message(uid, f"Сюжет: <b>{m.text}</b>. Пришлите фото, если ещё не отправили.")
        return

    if m.text in FORMATS:
        st["format"] = m.text
        bot.send_message(uid, f"Формат: <b>{m.text}</b>. Пришлите фото, если ещё не отправили.")
        return

    bot.send_message(uid, "Не понял. Нажмите кнопку сюжета или формата, либо пришлите фото.")

@bot.message_handler(content_types=["photo"])
def on_photo(m: telebot.types.Message):
    uid = m.from_user.id
    st = user_state.setdefault(uid, {"format": None, "scene_key": None, "photo_path": None})
    try:
        # берём самую большую версию
        file_id = m.photo[-1].file_id
        file_info = bot.get_file(file_id)
        content = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_info.file_path}", timeout=120).content
        fname = f"uploads/{uid}_{int(time.time())}.jpg"
        with open(fname, "wb") as f:
            f.write(content)
        st["photo_path"] = fname
        bot.reply_to(m, "Фото получено ✅")

        if not st.get("scene_key"):
            bot.send_message(uid, "Выберите сюжет кнопкой ниже.")
            return
        if not st.get("format"):
            bot.send_message(uid, "Выберите формат кнопкой ниже.")
            return

        # Запуск генерации
        bot.send_message(uid, "Генерирую видео… это займёт пару минут.")
        run_generation_and_send(uid, st)

    except Exception as e:
        print("photo error:", e)
        bot.reply_to(m, "Не получилось принять фото. Пришлите ещё раз.")

def run_generation_and_send(uid: int, st: dict):
    scene_key = st["scene_key"]
    scene = SCENES[scene_key]
    choose_format = st["format"]
    photo_path = st["photo_path"]

    prompt_text = build_prompt(scene["kind"], choose_format)
    datauri = encode_image_to_datauri(photo_path)

    try:
        start_resp = runway_start(datauri, prompt_text, scene["duration"])
    except Exception as e:
        bot.send_message(uid, f"Runway старт не удался: {e}")
        return

    task_id = start_resp.get("id") or start_resp.get("task", {}).get("id")
    if not task_id:
        bot.send_message(uid, f"Не получил id задачи от Runway. Ответ: {start_resp}")
        return

    poll = runway_poll(task_id)
    status = poll.get("status")
    if status != "SUCCEEDED":
        bot.send_message(uid, f"Генерация завершилась со статусом: {status}")
        return

    # В Runway обычно видео-URL лежит в output[0]
    out = poll.get("output") or []
    if not out:
        bot.send_message(uid, "Runway не вернул ссылку на видео.")
        return

    url = out[0] if isinstance(out[0], str) else out[0].get("url")
    if not url:
        bot.send_message(uid, "Не нашёл ссылку на видео в ответе Runway.")
        return

    save_as = f"renders/{uid}_{uuid.uuid4().hex}.mp4"
    try:
        download_file(url, save_as)
    except Exception as e:
        bot.send_message(uid, f"Не удалось скачать видео: {e}")
        return

    # Пока шлём как есть (водяной знак добавим следующим апдейтом)
    with open(save_as, "rb") as f:
        bot.send_video(uid, f, caption=f"{scene_key} · {choose_format}")

if __name__ == "__main__":
    print("Memory Forever bot started.")
    bot.infinity_polling(skip_pending=True, timeout=60)
