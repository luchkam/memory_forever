# bot.py — Memory Forever v0.3
# Шаги: Сюжет(ы) → Формат → Фон → Музыка → Фото(1/2) → Runway → постобработка (wm+audio+титр) → отправка
import os, io, time, uuid, base64, requests, subprocess, shutil, json
from datetime import datetime
from typing import List
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from PIL import ImageFilter
os.environ.setdefault("U2NET_HOME", os.path.join(os.getcwd(), "models"))
from rembg import remove, new_session
RMBG_SESSION = new_session("u2net")
# Дополнительные модели для портретов
RMBG_HUMAN = new_session("u2net_human_seg")
RMBG_ISNET  = new_session("isnet-general-use")
import telebot

# ---------- КЛЮЧИ ----------
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RUNWAY_KEY = os.environ.get("RUNWAY_API_KEY", "")
if not TG_TOKEN or not RUNWAY_KEY:
    print("⚠️ Задай TELEGRAM_BOT_TOKEN и RUNWAY_API_KEY в Secrets.")
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
OAI_ASSISTANT_ID = os.environ.get("OAI_ASSISTANT_ID", "")  # id из интерфейса ассистента (asst_...)
OAI_BASE = "https://api.openai.com/v1"
OAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
    "OpenAI-Beta": "assistants=v2",
}
OAI_DEBUG = os.environ.get("OAI_DEBUG", "0") == "1"  # включить расширенный лог, если надо
# Визуальное превью старт-кадра и промпта (перед Runway)
PREVIEW_START_FRAME = os.environ.get("PREVIEW_START_FRAME", "0") == "1"  # 1 — отправлять пользователю
DEBUG_TO_ADMIN      = os.environ.get("DEBUG_TO_ADMIN", "1") == "1"       # 1 — слать превью админу (если ADMIN_CHAT_ID задан)
ASSISTANT_GATE_ENABLED = os.environ.get("ASSISTANT_GATE_ENABLED", "1") == "1"
RUNWAY_SEND_JPEG     = os.environ.get("RUNWAY_SEND_JPEG", "1") == "1"  # конвертировать старт-кадр в JPEG перед отправкой
START_OVERLAY_DEBUG  = os.environ.get("START_OVERLAY_DEBUG", "0") == "1"  # рисовать диагностические рамки на старте
MF_DEBUG = OAI_DEBUG or (os.environ.get("MF_DEBUG", "0") == "1")

if not OPENAI_API_KEY or not OAI_ASSISTANT_ID:
    print("ℹ️ Для ассистента укажите OPENAI_API_KEY и OAI_ASSISTANT_ID (иначе будет фолбэк без проверки).")

def _log_oai(kind: str, url: str, status: int, body: str, payload_preview: str = ""):
    head = f"[OAI {kind}] {status} {url}"
    if payload_preview:
        print(head + f"\npayload: {payload_preview}\nresp: {body[:1000]}")
    else:
        print(head + f"\nresp: {body[:1000]}")

def _json_preview(d: dict, clip_keys=("image_url", "image_file")) -> str:
    try:
        j = dict(d)
        # не печатаем длинные строки целиком
        def shorten(v):
            s = str(v)
            return (s[:180] + "…") if len(s) > 200 else s
        def scrub(obj):
            if isinstance(obj, dict):
                return {k: ("<omitted>" if k in clip_keys else scrub(v)) for k, v in obj.items()}
            if isinstance(obj, list):
                return [scrub(x) for x in obj]
            if isinstance(obj, (str, bytes)):
                return shorten(obj)
            return obj
        return json.dumps(scrub(j), ensure_ascii=False)
    except Exception:
        return "<n/a>"

def _safe_send_photo(chat_id: int, path: str, caption: str = ""):
    try:
        with open(path, "rb") as ph:
            bot.send_photo(chat_id, ph, caption=caption[:1024])
    except Exception as e:
        print(f"[DBG] send_photo error: {e}")

def _short_gate(g: dict | None) -> str:
    if not g:
        return "gate: n/a"
    v = g.get("verdict")
    rs = g.get("reasons") or []
    return f"gate: {v} | reasons: {', '.join(rs[:4])}"

def _send_debug_preview(uid: int, scene_key: str, start_path: str, prompt: str, gate: dict | None):
    cap = (f"🎯 PREVIEW → {scene_key}\n"
           f"prompt[{len(prompt)}]: {prompt[:500]}{'…' if len(prompt)>500 else ''}\n"
           f"{_short_gate(gate)}")
    if PREVIEW_START_FRAME:
        _safe_send_photo(uid, start_path, cap)
    if DEBUG_TO_ADMIN and ADMIN_CHAT_ID:
        try:
            _safe_send_photo(int(ADMIN_CHAT_ID), start_path, f"[uid {uid}] {cap}")
        except Exception as e:
            print(f"[DBG] admin preview err: {e}")

def _is_admin(uid: int) -> bool:
    try:
        return ADMIN_CHAT_ID and str(uid) == str(int(ADMIN_CHAT_ID))
    except Exception:
        return False

# --- Админ для техподдержки (ID чата/ пользователя/группы) ---
# Заполни ADMIN_CHAT_ID в Secrets или впиши число здесь:
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()  # пример: "123456789"

# --- Тексты кнопок главного меню ---
BTN_MENU_MAIN   = "📋 Главное меню"
BTN_MENU_START  = "🎬 Сделать видео"
BTN_MENU_PRICE  = "💲 Стоимость"
BTN_MENU_SUPPORT= "🛟 Техподдержка"
BTN_MENU_GUIDE  = "📘 Инструкция по созданию видео"
BTN_MENU_DEMO   = "🎞 Пример работ"

# Кнопка «домой» для всех шагов мастера
BTN_GO_HOME = "🏠 В главное меню"

def kb_main_menu():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(telebot.types.KeyboardButton(BTN_MENU_MAIN),  telebot.types.KeyboardButton(BTN_MENU_START))
    kb.add(telebot.types.KeyboardButton(BTN_MENU_PRICE), telebot.types.KeyboardButton(BTN_MENU_SUPPORT))
    kb.add(telebot.types.KeyboardButton(BTN_MENU_GUIDE), telebot.types.KeyboardButton(BTN_MENU_DEMO))
    return kb

def show_main_menu(uid: int, text: str = None):
    text = text or 'Выберите пункт меню или перейдите к созданию видео, нажав «Сделать видео».'
    bot.send_message(uid, text, reply_markup=kb_main_menu())

# ---------- ПАПКИ ----------
os.makedirs("uploads",  exist_ok=True)
os.makedirs("renders",  exist_ok=True)
os.makedirs("assets",   exist_ok=True)
os.makedirs("audio",    exist_ok=True)
WATERMARK_PATH = "assets/watermark_black.jpg"

# ---------- СЦЕНЫ / ФОРМАТЫ / ФОНЫ / МУЗЫКА ----------
SCENES = {
    "🫂 Объятия 5с - БЕСПЛАТНО":    {"duration": 5,  "kind": "hug",        "people": 2},
    "🫂 Объятия 10с - 100 рублей":   {"duration": 10, "kind": "hug",        "people": 2},
    "💏 Поцелуй 10с - 100 рублей":       {"duration": 10, "kind": "kiss_cheek", "people": 2},
    "👋 Прощание 10с - 100 рублей": {"duration": 10, "kind": "wave",       "people": 1},
    "🪜 Уходит в небеса 10с - 100 рублей": {"duration": 10, "kind": "stairs", "people": 2},
}

FORMATS = {
    "🧍 В рост":  "full-body shot",
    "👨‍💼 По пояс": "waist-up shot",
    "👨‍💼 По грудь":  "chest-up shot",
}

BACKGROUNDS = {
    "☁️ Лестница среди облаков": "assets/backgrounds/bg_stairs.png",
    "🔆 Врата света":            "assets/backgrounds/bg_gates.png",
    "🪽 Ангелы и крылья":        "assets/backgrounds/bg_angels.png",
}

BG_IMAGE_PATHS = {
    "☁️ Лестница среди облаков": "assets/backgrounds/bg_stairs.jpg",
    "🔆 Врата света":            "assets/backgrounds/bg_gates.jpg",
    "🪽 Ангелы и крылья":        "assets/backgrounds/bg_angels.jpg",
}

MUSIC = {
    "🎵 Спокойная":   "audio/soft_pad.mp3",
    "🎵 Церковная":   "audio/gentle_arpeggio.mp3",
    "🎵 Лиричная":    "audio/strings_hymn.mp3",
}

# Картинки фонов (как ты и положил)
BG_FILES = {
    "☁️ Лестница среди облаков": "assets/backgrounds/bg_stairs.jpg",
    "🔆 Врата света":            "assets/backgrounds/bg_gates.jpg",
    "🪽 Ангелы и крылья":        "assets/backgrounds/bg_angels.jpg",
}

# Короткие «консервативные» подсказки к каждому фону
BG_TEXT = {
    "☁️ Лестница среди облаков":
        "must animate only: very gentle cloud drift left-to-right (~2 px/s) and faint light breathing (±3% brightness); "
        "stairs and architecture must remain fixed; do not add or remove any objects",
    "🔆 Врата света":
        "must animate only: subtle light pulsing (±3% brightness) and tiny cloud drift (~1–2 px/s); "
        "gates and columns must remain fixed; do not add ornaments or statues",
    "🪽 Ангелы и крылья":
        "must animate only: faint feather shimmer or tiny wing flicker (very rare) and minimal cloud drift (~1–2 px/s); "
        "angel figures must remain fixed; do not add or move elements",
}

# Ресэмплер под Pillow 10+
RESAMPLE = getattr(Image, "Resampling", Image)
MIN_GAP_PX = 20  # минимальный горизонтальный зазор между персонами
IDEAL_GAP_FRAC = 0.05      # целевой зазор между людьми ~5% ширины кадра
CENTER_BIAS_FRAC = 0.42    # стартовые центры людей: 42% и 58% ширины
# Максимальный допустимый апскейл (ограничение «на всякий»)
MAX_UPSCALE = float(os.environ.get("MAX_UPSCALE", "1.45"))

# Минимальные «видимые» высоты силуэтов (доля от H), чтобы не выглядели «карликами»
# Ключ: (формат, count_people) -> минимальная видимая высота bbox по альфе / H
MIN_VISIBLE_FRAC = {
    ("🧍 В рост", 1): 0.86,
    ("🧍 В рост", 2): 0.80,
    ("👨‍💼 По пояс", 1): 0.66,
    ("👨‍💼 По пояс", 2): 0.60,
    ("👨‍💼 По грудь", 1): 0.56,
    ("👨‍💼 По грудь", 2): 0.50,
}

def _min_frac_for(format_key: str, count_people: int) -> float:
    # Базовый дефолт на случай новых форматов
    return MIN_VISIBLE_FRAC.get((format_key, count_people), 0.60)
# --- Целевые высоты силуэтов (доля от высоты кадра) ---
TH_FULL_SINGLE   = 0.73
TH_FULL_DOUBLE   = 0.68
TH_WAIST_SINGLE  = 0.68
TH_WAIST_DOUBLE  = 0.64
TH_CHEST_SINGLE  = 0.62
TH_CHEST_DOUBLE  = 0.58

# Audio files are now real MP3 files provided by user
# No need to create placeholder sounds anymore

# Минимальная доля высоты кадра, которую должна занимать фигура/группа (anti-micro-people)
MIN_SINGLE_FRAC = {
    "В рост": 0.86,
    "По пояс": 0.76,
    "По грудь": 0.68,
}
MIN_PAIR_FRAC = {
    "В рост": 0.72,
    "По пояс": 0.65,
    "По грудь": 0.60,
}
# Мягкий предел апскейла для финального «подрастания» (от текущего target_h)
PAIR_UPSCALE_CAP = 1.22   # не увеличиваем целевую высоту более чем на 22% за один пасс
SINGLE_UPSCALE_CAP = 1.25 # для одиночной фигуры

def _bg_layout_presets(bg_path: str):
    """
    Возвращает словарь с мягкими ограничителями компоновки под конкретный фон.
    center_frac — центр «полосы» в долях ширины,
    band_frac   — ширина «полосы» (в долях ширины),
    top_headroom_min/max — допустимый зазор сверху (доли высоты).
    """
    name = os.path.basename(str(bg_path)).lower()
    # по умолчанию — широкая полоса и умеренный потолок
    presets = dict(center_frac=0.50, band_frac=0.46, top_headroom_min=0.05, top_headroom_max=0.13)

    if "stairs" in name:
        # держим людей ближе к центру, сверху допустимо чуть больше воздуха (блик)
        presets = dict(center_frac=0.50, band_frac=0.40, top_headroom_min=0.06, top_headroom_max=0.18)
    elif "gates" in name:
        presets = dict(center_frac=0.50, band_frac=0.44, top_headroom_min=0.05, top_headroom_max=0.15)
    # angels — оставим дефолт
    return presets

NEG_TAIL = (
    "no face morphing, no identity change, no makeup change, no aging, "
    "do not add new accessories, keep existing accessories, "
    "no extra fingers, no deformed hands, no melting faces, "
    "no background geometry changes, no new background objects, "
    "camera locked, very low creativity"
)

KISS_FALLBACKS = [
    "two people embrace cheek-to-cheek; tender cheek touch; no lip contact; hold briefly",
    "one person gives a gentle kiss on the forehead; respectful, brief; other person softly leans"
]

# Добавки к промпту по умолчанию, когда ассистент недоступен/таймаут/ошибка
BACKUP_PROMPT_ADDITIONS = (
    "stabilize subject scale; prevent stretching or shrinking; "
    "keep feet grounded and fixed to floor plane; "
    "no zoom, no dolly; lock camera; "
    "refine edges softly (1-2px feather); avoid hallucinated limbs"
)

# ---------- СТЕЙТ ----------
def new_state():
    return {
        "scenes": [],
        "format": None,
        "bg": None,
        "music": None,
        "photos": [],
        "ready": False,
        "support": False,   # ждём текст для техподдержки
    }

users = {}  # uid -> state
# Буфер для альбомов (несколько фото, пришедших одним медиа-группой)
PENDING_ALBUMS = {}  # media_group_id -> {"uid": int, "need": int, "paths": list[str]}

# ---------- КЛАВИАТУРЫ ----------
def available_scene_keys(format_key: str | None) -> list[str]:
    # если формат не "В рост" — убираем все сцены с kind == "stairs"
    keys = []
    for name, meta in SCENES.items():
        if format_key and "В рост" not in format_key and meta.get("kind") == "stairs":
            continue
        keys.append(name)
    return keys

def kb_scenes(format_key: str | None = None):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    # Список доступных сцен с учётом формата
    scene_keys = available_scene_keys(format_key)
    scene_buttons = [telebot.types.KeyboardButton(k) for k in scene_keys]
    if scene_buttons:
        kb.add(*scene_buttons)

    # служебные — отдельными рядами
    kb.add(
        telebot.types.KeyboardButton("✅ Выбрано, дальше"),
        telebot.types.KeyboardButton("🔁 Сбросить выбор сюжетов"),
    )
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

def kb_formats():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add(*[telebot.types.KeyboardButton(k) for k in FORMATS.keys()])
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

def kb_backgrounds():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for k in BACKGROUNDS.keys():
        kb.add(telebot.types.KeyboardButton(k))
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

def kb_music():
    """Inline-клавиатура для выбора музыки с возможностью прослушивания"""
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)

    for name, path in MUSIC.items():
        # Убираем эмодзи 🎵 для callback-данных
        clean_name = name.replace("🎵 ", "")
        listen_btn = telebot.types.InlineKeyboardButton(
            f"🎧 {clean_name}", callback_data=f"listen_{clean_name}"
        )
        select_btn = telebot.types.InlineKeyboardButton(
            f"✅ {clean_name}", callback_data=f"select_music_{clean_name}"
        )
        kb.add(listen_btn, select_btn)

    # Кнопка "Без музыки"
    no_music_btn = telebot.types.InlineKeyboardButton(
        "🔇 Без музыки", callback_data="select_music_none"
    )
    kb.add(no_music_btn)

    # Кнопка "В главное меню"
    home_btn = telebot.types.InlineKeyboardButton(
        "🏠 В главное меню", callback_data="go_home"
    )
    kb.add(home_btn)

    return kb

def kb_music_old():
    """Старое меню для совместимости"""
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for k in MUSIC.keys():
        kb.add(telebot.types.KeyboardButton(k))
    kb.add(telebot.types.KeyboardButton("🔇 Без музыки"))
    kb.add(telebot.types.KeyboardButton(BTN_GO_HOME))
    return kb

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def find_music_by_name(name: str) -> str|None:
    """Находит путь к музыкальному файлу по имени"""
    for key, path in MUSIC.items():
        if key.replace("🎵 ", "") == name:
            return path
    return None
def alpha_metrics(img: Image.Image, thr: int = 20):
    """
    Возвращает (bbox, y_bottom) по непрозрачным пикселям альфа-канала.
    bbox: (x0, y0, x1, y1) в координатах изображения
    y_bottom: индекс нижней строки содержимого (int)
    """
    a = img.split()[-1]
    arr = np.asarray(a, dtype=np.uint8)
    ys, xs = np.where(arr >= thr)
    if ys.size == 0:
        b = img.getbbox() or (0, 0, img.width, img.height)
        return b, b[3] - 1
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max()) + 1, int(ys.max()) + 1
    return (x0, y0, x1, y1), (y1 - 1)

def _save_layout_debug(canvas_rgba: Image.Image, metrics: dict, base_id: str):
    """
    Сохраняет:
      - renders/temp/metrics_<base_id>.json — метрики компоновки
      - renders/temp/annot_<base_id>.png    — аннотированное превью с рамками
    """
    try:
        os.makedirs("renders/temp", exist_ok=True)
    except Exception:
        pass

    # 1) JSON
    try:
        mpath = f"renders/temp/metrics_{base_id}.json"
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] metrics -> {mpath}")
    except Exception as e:
        print(f"[DEBUG] metrics save error: {e}")

    # 2) Аннотированная картинка
    try:
        im = canvas_rgba.convert("RGB")
        draw = ImageDraw.Draw(im)
        font = None
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            font = ImageFont.load_default()

        # Рамки и подписи
        colors = {"L": (46, 204, 113), "R": (52, 152, 219)}  # зелёный/синий
        for side in ("L", "R"):
            if side not in metrics: 
                continue
            r = metrics[side]["rect_abs"]  # [x0,y0,x1,y1]
            c = colors[side]
            # рамка
            draw.rectangle(r, outline=c, width=3)
            # подпись
            label = (f"{side}: h={metrics[side]['height_px']} "
                     f"({int(round(metrics[side]['height_frac']*100))}% H), "
                     f"w={metrics[side]['width_px']}, "
                     f"cx={int(round(metrics[side]['center_x_frac']*100))}%, "
                     f"scale≈{metrics[side]['scale']:.2f}")
            tx, ty = r[0] + 4, max(4, r[1] - 18)
            draw.rectangle([tx-2, ty-2, tx+draw.textlength(label, font=font)+6, ty+18], fill=(0,0,0,128))
            draw.text((tx, ty), label, fill=(255,255,255), font=font)

            # отметка «пол»
            fy = metrics[side].get("floor_y")
            if isinstance(fy, int):
                draw.line([(r[0], fy), (r[2], fy)], fill=c, width=2)

        # Зазор между людьми
        gap = metrics.get("gap_px")
        if gap is not None:
            text = f"gap={gap}px ({int(round(metrics.get('gap_frac',0)*100))}% W)"
            draw.rectangle([10, 10, 10+draw.textlength(text, font=font)+12, 10+22], fill=(0,0,0,128))
            draw.text((16, 12), text, fill=(255,255,255), font=font)

        apath = f"renders/temp/annot_{base_id}.png"
        im.save(apath, "PNG")
        print(f"[DEBUG] annot -> {apath}")
    except Exception as e:
        print(f"[DEBUG] annot save error: {e}")

def compact_prompt(s: str, max_len: int = 900) -> str:
    """Убираем повторы пробелов, переводов строк и режем очень длинные промпты."""
    s = " ".join(str(s).split())
    if len(s) > max_len:
        s = s[:max_len-3] + "..."
    return s

# --- Heuristic: treat some reasons as "minor", suitable for warn instead of block
MINOR_REASON_MARKERS = (
    # ru
    "обрезан палец", "обрезаны пальцы", "мелкие артефакты", "легкие артефакты", "артефакт вырезки",
    "тонкий ореол", "ореол по контуру", "незначительное", "чуть обрезано", "слегка обрезано",
    "немного шум", "слегка смещено", "мелкие дефекты маски",
    # en (на всякий случай)
    "minor", "slight", "tiny", "halo", "soft edge", "edge halo", "mask artifact",
    "partial finger", "fingers cut", "small artifact", "slight misalignment"
)
MAJOR_BLOCK_MARKERS = (
    # ru
    "перекрытие фигур", "перекрывают друг друга", "вылезает за кадр", "вне кадра", "нет ног",
    "нет конечности", "сильная деформация", "слишком далеко друг от друга", "масштаб сильно отличается",
    "голова обрезана", "критично обрезано",
    # en
    "overlap", "outside frame", "out of frame", "missing limb", "severe deformation",
    "too far apart", "cropped head", "wild scaling"
)

def _is_minor_only(reasons: list[str] | None) -> bool:
    """Возвращает True, если причины — только «мелкие» (и нет серьёзных)."""
    if not reasons:
        return False
    text = " | ".join(str(r).lower() for r in reasons)
    if any(m in text for m in MAJOR_BLOCK_MARKERS):
        return False
    return any(m in text for m in MINOR_REASON_MARKERS)

def validate_photo(path: str) -> tuple[bool, list[str]]:
    """
    Мягкая валидация фото.
    Возвращает (ok, warnings). ok=False — очень маленькое фото, но мы всё равно не блокируем пайплайн.
    """
    warns = []
    ok = True
    try:
        im = Image.open(path)
    except Exception as e:
        return False, [f"не удалось открыть файл ({e})"]

    w, h = im.size
    min_dim = min(w, h)

    # 1) Размер/разрешение
    if min_dim < 300:
        ok = False
        warns.append(f"очень маленькое разрешение ({w}×{h}) — результат может исказиться")
    elif min_dim < 600:
        warns.append(f"низкое разрешение ({w}×{h}) — желательно ≥ 800px по меньшей стороне")

    # 2) Ориентация (для портретов лучше вертикальная)
    ratio = w / h if h else 1.0
    if ratio > 0.9:
        warns.append("фото не вертикальное — портрет обычно лучше выглядит в вертикали")

    # 3) Темнота/экспозиция
    gray = im.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    mean = float(arr.mean())
    if mean < 55:
        warns.append("фото тёмное — попробуйте более светлое/контрастное")

    # 4) Размытость (приблизительная оценка через «края»)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    earr = np.asarray(edges, dtype=np.float32)
    sharpness = float(earr.std())
    if sharpness < 8:
        warns.append("возможная размытость/шум — контуры слабые")

    return ok, warns

def _visible_bbox_height(img: Image.Image) -> int:
    b = img.getbbox() or (0, 0, img.width, img.height)
    return max(1, b[3] - b[1])

def smart_cutout(img_rgba: Image.Image) -> Image.Image:
    """
    1) Портретная модель -> запасная универсальная.
    2) Мягкая «подрезка ореола» + легкое перо краёв.
    """
    def _run(session):
        out = remove(img_rgba, session=session, post_process_mask=True)
        if isinstance(out, (bytes, bytearray)):
            out = Image.open(io.BytesIO(out)).convert("RGBA")
        return out

    # 1. Пытаемся human_seg
    try:
        cut = _run(RMBG_HUMAN)
    except Exception:
        cut = _run(RMBG_SESSION)

    # 2. Если силуэт подозрительно маленький — пробуем ISNet
    try:
        bb = cut.getbbox() or (0, 0, cut.width, cut.height)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if area < 0.12 * cut.width * cut.height:
            alt = _run(RMBG_ISNET)
            bb2 = alt.getbbox() or (0, 0, alt.width, alt.height)
            area2 = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])
            if area2 > area:
                cut = alt
    except Exception:
        pass

    # 3. Рафинирование маски: чуть «поджать» и дать перо
    a = cut.split()[-1]
    # слегка убрать ореол
    a = a.filter(ImageFilter.MinFilter(3))       # 1px эрозия
    # мягкое перо
    a = a.filter(ImageFilter.GaussianBlur(1.2))  # ~1–2px
    cut.putalpha(a)
    return cut

# ---------- PROMPT BUILDER ----------
def build_prompt(kind: str, framing: str, bg_prompt: str, duration_sec: int):
    # Плавность/темп
    if duration_sec <= 5:
        pace = ("very slow, subtle motion; limit head yaw to ~10-15 degrees; "
                "avoid quick turns; ease-in, ease-out; hold pose at the end")
        turn_portion = "first 50% of the duration"
    else:
        pace = ("slow, smooth motion; limit head yaw to ~15-20 degrees; "
                "no quick snaps; ease-in, ease-out; hold pose at the end")
        turn_portion = "first 60% of the duration"

    # Сцены
    if kind == "hug":
        main = (
            "two people are already close to each other (small inner gap ~4–6% of frame width); "
            f"over the {turn_portion} they gently lean toward each other and embrace; "
            "do not drift them apart; keep their apparent size constant; hold the embrace at the end; "
            "maintain safe margins from frame edges; keep full silhouettes visible; "
            "ensure no cropping of heads, hands, or feet"
        )
    elif kind == "kiss_cheek":
        main = (f"two people slowly turn toward each other over the {turn_portion}; "
                "a tender cheek or forehead touch; hold the moment, no lip contact")
    elif kind == "wave":
        main = ("one person faces camera and gives a slow farewell wave (2–3 cycles) with wrist only; "
                "body stays mostly still; end with a soft pause")
    elif kind == "stairs":
        main = ("one person slowly walks upstairs into the heavenly light while the other person stays below watching; "
                "the walking person may look back kindly; end with a soft fade into light")
    else:
        main = "two people gently approach each other and hug"

    # Фон — сохраняем «как есть»
    bg_anim = (
        "preserve the provided background plate exactly as in the image (geometry and composition fixed); "
        "do not add or remove objects; "
        f"{bg_prompt}"
    )

    # Запреты на камеру и масштаб
    camera_lock = (
        "camera locked; no zoom; no pan; no tilt; no roll; no dolly; "
        "keep field of view constant; no lens breathing"
    )
    scale_lock_pair = (
        "subjects must keep constant apparent size relative to the frame; "
        "strictly no scaling or stretching of people; no growth or shrink; "
        "keep their pixel-to-pixel silhouette alignment within ±2px tolerance"
    )
    scale_lock_stairs = (
        "subject size may change only minimally and smoothly due to going up the stairs (<=5% over the whole clip); "
        "no sudden jumps; no stretching"
    )

    # Фиксация опоры/позиций
    if kind == "stairs":
        ground_lock = (
            "respect the stair plane; feet step naturally upward; "
            "no lateral sliding; apparent size change must be minimal and smooth; "
            + scale_lock_stairs
        )
    else:
        ground_lock = (
            "subjects remain on the ground plane; no forward/backward translation; "
            "if approach is needed, allow only a tiny horizontal slide toward each other (<=6% of frame width per subject); "
            + scale_lock_pair
        )

    identity = (
        "faces remain consistent with the photo; preserve hairstyle and hairline; "
        "do not alter facial proportions; mouth closed, natural blinking, realistic skin; "
        "preserve all clothing and accessories exactly as in the photo; do not add new accessories"
    )

    return (
        f"{main}; {framing}; {bg_anim}; "
        f"{pace}; {camera_lock}; {ground_lock}; {identity}; {NEG_TAIL}"
    )

# ---------- RUNWAY ----------
RUNWAY_API = "https://api.dev.runwayml.com/v1"
HEADERS = {
    "Authorization": f"Bearer {RUNWAY_KEY}",
    "X-Runway-Version": "2024-11-06",
    "Content-Type": "application/json",
}

def encode_image_datauri(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = path.lower().split(".")[-1]
    mime = "image/jpeg" if ext in ["jpg","jpeg"] else "image/png"
    return f"data:{mime};base64,{b64}"

def ensure_jpeg_copy(path: str, quality: int = 88) -> str:
    """
    Делает JPEG-копию файла (оптимизированную) и возвращает путь к .jpg.
    """
    im = Image.open(path).convert("RGB")
    out = os.path.splitext(path)[0] + ".jpg"
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    try:
        os.sync()  # не у всех ОС есть, ок если свалится
    except Exception:
        pass
    return out

def encode_image_as_jpeg_datauri(path: str, quality: int = 88) -> str:
    """
    Принудительно кодирует изображение в JPEG (RGB) и возвращает dataURI.
    Это уменьшает размер по сравнению с PNG и стабильнее проходит в Runway.
    """
    im = Image.open(path).convert("RGB")
    bio = io.BytesIO()
    im.save(bio, format="JPEG", quality=quality, optimize=True, progressive=True)
    b64 = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

def cut_foreground_to_png(in_path: str) -> str:
    """Вырезает фон из JPG/PNG и сохраняет PNG с альфой."""
    with open(in_path, "rb") as f:
        raw = f.read()
    out = remove(raw, session=RMBG_SESSION)
    out_path = os.path.splitext(in_path)[0] + "_cut.png"
    with open(out_path, "wb") as f:
        f.write(out)
    return out_path

def _to_jpeg_copy(src_path: str, quality: int = 88) -> str:
    im = Image.open(src_path).convert("RGB")
    out_path = os.path.join("uploads", f"startframe_{uuid.uuid4().hex}.jpg")
    im.save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)
    return out_path

def ensure_runway_datauri_under_limit(path: str, limit: int = 5_000_000) -> tuple[str, str]:
    data = encode_image_datauri(path)
    if len(data) <= limit:
        return data, path

    last_path = path
    for q in (88, 80, 72):
        try:
            jpg = _to_jpeg_copy(path, quality=q)
            last_path = jpg
            data = encode_image_datauri(jpg)
            if len(data) <= limit:
                print(f"[Runway] using JPEG q={q}, data_uri={len(data)} bytes")
                return data, jpg
        except Exception as e:
            print(f"[Runway] jpeg fallback q={q} failed: {e}")

    print(f"[Runway] still heavy after JPEG attempts, length={len(data)}")
    return data, last_path

def _post_runway(payload: dict) -> dict | None:
    try:
        if "promptText" in payload or "prompt" in payload:
            _pl = payload.get("promptText") or payload.get("prompt") or ""
            print(f"[Runway] model={payload.get('model')} dur={payload.get('duration')} "
                  f"ratio={payload.get('ratio') or payload.get('aspect_ratio')} "
                  f"prompt[:200]={_pl[:200].replace(chr(10),' ')}...")
        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                # делаем санитайз превью, чтобы не писать гигантский data URI
                preview = {
                    "model": payload.get("model"),
                    "duration": payload.get("duration"),
                    "ratio": payload.get("ratio") or payload.get("aspect_ratio"),
                    "prompt_len": len((_pl or "")),
                    "image_data_uri_len": len(payload.get("promptImage") or payload.get("image") or ""),
                }
                with open(os.path.join("renders/temp", f"runway_payload_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                    json.dump(preview, f, ensure_ascii=False, indent=2)
                print("[Runway] payload preview saved")
            except Exception as _e:
                print(f"[Runway] payload preview save err: {_e}")
        r = requests.post(f"{RUNWAY_API}/image_to_video", headers=HEADERS, json=payload, timeout=60)
        if r.status_code == 200:
            return r.json()
        # полезно видеть ответ сервера при 4xx
        print(f"[Runway {r.status_code}] {r.text}")
        return None
    except requests.RequestException as e:
        print(f"[Runway transport error] {e}")
        return None

def runway_start(prompt_image_datauri: str, prompt_text: str, duration: int):
    """
    Порядок попыток:
    1) gen4_turbo + promptImage/promptText + ratio (текущая схема этого API)
    2) gen4_turbo + image/prompt + aspect_ratio (альтернативная)
    3) gen3a_turbo + image/prompt + aspect_ratio (запасной)
    """
    variants = [
        {
            "model": "gen4_turbo",
            "promptImage": prompt_image_datauri,   # <-- ОБЯЗАТЕЛЬНО
            "promptText":  prompt_text,
            "ratio": "720:1280",
            "duration": int(duration),
        },
        {
            "model": "gen4_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
        {
            "model": "gen3a_turbo",
            "image": prompt_image_datauri,
            "prompt": prompt_text,
            "aspect_ratio": "9:16",
            "duration": int(duration),
        },
    ]

    last_keys = ""
    for payload in variants:
        resp = _post_runway(payload)
        if resp:
            return resp
        last_keys = f"{list(payload.keys())}"

    raise RuntimeError(f"Runway returned 400/4xx for all variants (payload={last_keys}). Check logs above.")

def runway_poll(task_id: str, timeout_sec=900, every=5):
    start = time.time()
    while True:
        rr = requests.get(f"{RUNWAY_API}/tasks/{task_id}", headers=HEADERS, timeout=60)
        rr.raise_for_status()
        data = rr.json()
        st = data.get("status")
        if st in ("SUCCEEDED","FAILED","ERROR","CANCELED"):
            return data
        if time.time() - start > timeout_sec:
            return {"status":"TIMEOUT","raw":data}
        time.sleep(every)

def download(url: str, save_path: str):
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk: f.write(chunk)
    return save_path

def _log_fail(uid: int, reason: str, payload: dict | None = None, response: dict | None = None):
    try:
        os.makedirs("renders/temp", exist_ok=True)
        path = os.path.join("renders/temp", f"fail_{uid}_{int(time.time())}_{uuid.uuid4().hex}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "ts": datetime.utcnow().isoformat() + "Z",
                "uid": uid,
                "reason": reason,
                "payload": payload or {},
                "response": response or {}
            }, f, ensure_ascii=False, indent=2)
        print(f"[FAILLOG] {reason} -> {path}")
        # если задан ADMIN_CHAT_ID — шлём короткое уведомление
        if ADMIN_CHAT_ID:
            try:
                bot.send_message(int(ADMIN_CHAT_ID), f"⚠️ FAIL {reason} (uid={uid})\n{os.path.basename(path)} сохранён.")
            except Exception:
                pass
    except Exception as e:
        print(f"[FAILLOG] write error: {e}")

MODERATION_SCHEMA = {
    "name": "visual_editor_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["reject_user_photo", "accept", "accept_with_backend_fixes"],
                "description": "Decision status: reject if source photo has defects, accept if good, accept_with_backend_fixes if technical positioning issues need backend correction."
            },
            "user_notes": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["missing_head", "missing_hands", "prohibited_content", "low_resolution", "too_dark", "blurred", "profile_view", "sitting_pose", "occluded_face", "cutout_artifacts"]
                },
                "description": "Issues with source photos that user needs to fix - only about photo quality, not positioning."
            },
            "backend_fixes": {
                "type": "object",
                "properties": {
                    "recompose": {"type": "boolean", "description": "Whether backend should recompose the frame automatically."},
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["overlap", "out_of_frame", "scale_mismatch", "far_apart", "z_order_wrong", "uneven_floor", "spacing_issue"]
                        },
                        "description": "Technical positioning issues for backend to fix automatically."
                    },
                    "target_height_frac": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Target height fractions for [left, right] figures (0.6-0.9)."
                    },
                    "target_centers": {
                        "type": "array", 
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "Target center X positions for [left, right] figures (0.2-0.8)."
                    },
                    "gap_frac": {"type": "number", "description": "Target gap between figures as fraction of frame width (0.01-0.15)."},
                    "align_feet": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "floor_y": {"type": "integer"}
                        },
                        "required": ["enabled", "floor_y"],
                        "additionalProperties": False
                    }
                },
                "required": ["recompose", "issues", "target_height_frac", "target_centers", "gap_frac", "align_feet"],
                "additionalProperties": False
            },
            "runway_prompt_additions": {
                "type": "string",
                "description": "Optional motion/tempo/constraint details for video generation prompt."
            }
        },
        "required": ["status", "user_notes", "backend_fixes", "runway_prompt_additions"],
        "additionalProperties": False
    }
}

def _normalize_gate(g: dict | None) -> dict | None:
    if not isinstance(g, dict):
        return None
    out = dict(g)

    # status
    st = (out.get("status") or "").strip().lower()
    if st not in ("reject_user_photo", "accept", "accept_with_backend_fixes"):
        st = "accept"
    out["status"] = st

    # user_notes
    un = out.get("user_notes")
    if not isinstance(un, list):
        un = []
    valid_notes = ["missing_head", "missing_hands", "prohibited_content", "low_resolution", "too_dark", "blurred", "profile_view", "sitting_pose", "occluded_face", "cutout_artifacts"]
    un = [note for note in un if note in valid_notes][:10]  # максимум 10 заметок
    out["user_notes"] = un

    # backend_fixes
    bf = out.get("backend_fixes")
    if not isinstance(bf, dict):
        bf = {}
    bf.setdefault("recompose", False)
    bf.setdefault("issues", [])
    bf.setdefault("target_height_frac", [0.77, 0.77])
    bf.setdefault("target_centers", [0.35, 0.65])
    bf.setdefault("gap_frac", 0.05)
    bf.setdefault("align_feet", {"enabled": True, "floor_y": 1180})

    # Валидация backend_fixes
    if not isinstance(bf["issues"], list):
        bf["issues"] = []
    valid_issues = ["overlap", "out_of_frame", "scale_mismatch", "far_apart", "z_order_wrong", "uneven_floor", "spacing_issue"]
    bf["issues"] = [issue for issue in bf["issues"] if issue in valid_issues][:10]

    if not isinstance(bf["target_height_frac"], list) or len(bf["target_height_frac"]) != 2:
        bf["target_height_frac"] = [0.77, 0.77]
    if not isinstance(bf["target_centers"], list) or len(bf["target_centers"]) != 2:
        bf["target_centers"] = [0.35, 0.65]
    if not isinstance(bf["gap_frac"], (int, float)):
        bf["gap_frac"] = 0.05
    if not isinstance(bf["align_feet"], dict):
        bf["align_feet"] = {"enabled": True, "floor_y": 1180}

    out["backend_fixes"] = bf

    # runway_prompt_additions
    rpa = out.get("runway_prompt_additions") or ""
    rpa = " ".join(str(rpa).split())
    if len(rpa) > 600:
        rpa = rpa[:597] + "..."
    out["runway_prompt_additions"] = rpa

    return out

MODERATION_INSTRUCTIONS = """
Ты — визуальный редактор. Получаешь старт-кадр (1–2 вырезанные персоны на фоне) и базовый промпт.
Задача:
1. Семантически проверить кадр (не пиксельно): естественность композиции, видимость лиц, контакт с «полом/ступенью», отсутствие сильных артефактов.
2. Если кадр годится — дополни базовый промпт только деталями движения/темпа/ограничений (камера статична, амплитуда головы/плеч, фиксация стоп и бёдер и т. п.). Нельзя менять внешность, одежду, фон, геометрию.
3. Классифицируй строго:
   • ok — годится; допустимы мелкие дефекты, которые генератор обычно исправляет: слегка срезанные пальцы/локоны, тонкий ореол, лёгкая размытость краёв, небольшой перевес/несимметрия, недостающие 5–10% от носка/пятки при явном контакте с опорой.
   • warn — годится, но дай 1–3 коротких рекомендации («подвинуть на 20px», «чуть уменьшить правого на 5%», «оставить больше места над головой»).
   • block — нельзя: лицо закрыто/нечитабельно; отсутствует значимая часть конечности (≈≥30%); фигуры перекрываются так, что одна нечитабельна; явно «висят в воздухе» (нет контакта с опорой), сильная вырезка: «дырки», «пол лица», крупные посторонние объекты перекрывают корпус.

Отвечай строго JSON по схеме.

- "ok": кадр годится. Небольшие артефакты допустимы.
- "warn": есть некритичные моменты (перечисли кратко в reasons), но генерировать можно.
- "block": только если серьёзные проблемы, из-за которых видео почти наверняка будет плохим:
  • сильное перекрытие людей (>25% площади торса/головы),
  • отсутствует значимая часть головы/торса,
  • обрезана нижняя опора так, что человек “висит” и это не правится слайдом,
  • катастрофические ошибки вырезки (сквозные дырки, половина тела прозрачна),
  • человек почти весь вне кадра,
  • масштаб тела экстремально мал (<35% высоты кадра) или экстремально велик (>98%).

Что НЕ считается причиной для "block" (можно "ok" или "warn"):
- слегка обрезанные пальцы/кисти, мелкие артефакты по контуру, лёгкая щербатость маски,
- разные ступени на лестнице, небольшая разница по высоте опоры,
- небольшое расхождение поз/поворота головы,
- слабая размытость, небольшая тень/ореол от вырезки.

layout_feedback:
- Верни небольшие подсказки компоновки (целые числа):
  shift_left_px, shift_right_px ∈ [-80; 80]  — сдвиг по X (влево/вправо).
  scale_left_pct, scale_right_pct ∈ [-20; 20] — масштаб в процентах.
- Эти подсказки мягкие и опциональные. Если не требуется — ставь 0.
- НЕ предлагай экстремальные значения. Цель — слегка подтянуть к центру и/или сблизить.

prompt_additions:
- КОРОТКАЯ строка (до ~300 символов) только про движение/темп/ограничения. Примеры:
  "allow tiny horizontal slide toward each other; keep size constant; ease-in/out"
  "very subtle head yaw only; no zoom; camera locked"
- Не дублируй базовый промпт, не комментируй внешний вид, не давай советы по пересъёмке здесь.

Важно:
- Ответ только JSON по выданной схеме.
- reasons — 1–4 кратких пункта (если есть).
- Не требуй «встать на одну ступень». Это не критично.
"""

def oai_upload_image(path: str) -> str | None:
    """
    Загружает файл для Assistants и возвращает file_id.
    """
    try:
        with open(path, "rb") as f:
            files = {"file": (os.path.basename(path), f, "image/png")}
            data = {"purpose": "assistants"}
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "assistants=v2"}
            r = requests.post(f"{OAI_BASE}/files", headers=headers, files=files, data=data, timeout=60)
        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                meta = {"file": os.path.basename(path), "size": os.path.getsize(path)}
                with open(os.path.join("renders/temp", f"oai_upload_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                print("[OAI files] upload meta saved")
            except Exception as _e:
                print(f"[OAI files] upload meta err: {_e}")

        if r.status_code != 200:
            _log_oai("files", f"{OAI_BASE}/files", r.status_code, r.text)
            return None
        fid = r.json().get("id")
        if OAI_DEBUG:
            print(f"[OAI files] uploaded {path} -> {fid}")
        return fid
    except Exception as e:
        print(f"[OAI files] upload error: {e}")
        return None

def oai_create_thread_with_image(user_text: str, file_id: str) -> str | None:
    payload_thread = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_file", "image_file": {"file_id": file_id}},
                ],
            }
        ]
    }
    try:
        r = requests.post(f"{OAI_BASE}/threads", headers=OAI_HEADERS, data=json.dumps(payload_thread), timeout=60)
        if r.status_code != 200:
            _log_oai("threads", f"{OAI_BASE}/threads", r.status_code, r.text, _json_preview(payload_thread))
            return None
        return r.json().get("id")
    except Exception as e:
        print(f"[OAI threads] create error: {e}")
        return None

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    # Ручной тумблер: если выключен — пропускаем проверку.
    if not ASSISTANT_GATE_ENABLED:
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }
    """
    Проверка кадра ассистентом:
    1) upload файла -> file_id
    2) threads.create (content: text + image_file)
    3) runs.create (response_format = JSON Schema)
    4) poll -> messages.list -> parse JSON
    """
    if not OPENAI_API_KEY or not OAI_ASSISTANT_ID:
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }

    # 1) Upload
    file_id = oai_upload_image(start_frame_path)
    if not file_id:
        print("[OAI] skip gate (upload failed)")
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }

    user_text = (
        "Контекст:\n"
        f"- Формат кадра: {meta.get('format')}\n"
        f"- Сюжет: {meta.get('scene')}\n"
        f"- Фон: {meta.get('background')}\n\n"
        "Базовый промпт (его можно ДОПОЛНИТЬ нюансами движения и темпа, но нельзя менять композицию/внешность):\n"
        f"{base_prompt}\n\n"
        "Политика мягкой проверки:\n"
        "• НЕ блокируй за мелкие дефекты: слегка обрезанные кончики пальцев/подошвы, лёгкий ореол, микро-сколы маски — считаем, что Runway дорисует.\n"
        "• Блокируй ТОЛЬКО серьёзные проблемы: крупные отсутствующие части тела, сильное перекрытие лиц, явное «парение» без опоры двумя ногами, экстремальный наклон/масштаб.\n"
        "• Для объятий/поцелуя, если люди далеко — верни verdict='warn' и добавь в prompt_additions короткую подсказку про мягкий горизонтальный «съезд» навстречу (<=8% ширины кадра на человека), без изменения размера и с ногами на земле.\n\n"
        "Верни строго JSON по нашей схеме."
    )

    # 2) Thread
    thread_id = oai_create_thread_with_image(user_text, file_id)
    if not thread_id:
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }

    # 3) Run
    payload_run = {
        "assistant_id": OAI_ASSISTANT_ID,
        "instructions": MODERATION_INSTRUCTIONS,
        "response_format": {"type": "json_schema", "json_schema": MODERATION_SCHEMA},
        "temperature": 0.2
    }

    try:
        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                with open(os.path.join("renders/temp", f"oai_run_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                    json.dump(payload_run, f, ensure_ascii=False, indent=2)
                print("[OAI] run payload saved")
            except Exception as _e:
                print(f"[OAI] run payload save err: {_e}")
        r = requests.post(f"{OAI_BASE}/threads/{thread_id}/runs", headers=OAI_HEADERS, data=json.dumps(payload_run), timeout=60)
        if r.status_code != 200:
            _log_oai("runs", f"{OAI_BASE}/threads/{thread_id}/runs", r.status_code, r.text, _json_preview(payload_run))
            return None
        run_id = r.json().get("id")
    except Exception as e:
        print(f"[OAI runs] create error: {e}")
        return None

    # 4) Poll
    import time as _t
    start = _t.time()
    while True:
        rr = requests.get(f"{OAI_BASE}/threads/{thread_id}/runs/{run_id}", headers=OAI_HEADERS, timeout=60)
        if rr.status_code != 200:
            _log_oai("runs.get", f"{OAI_BASE}/threads/{thread_id}/runs/{run_id}", rr.status_code, rr.text)
            return None
        st = rr.json().get("status")
        if st in ("completed", "failed", "cancelled", "expired"):
            break
        if _t.time() - start > timeout_sec:
            print("[OAI] run timeout")
            return {
                "status": "accept",
                "user_notes": [],
                "backend_fixes": {
                    "recompose": False,
                    "issues": [],
                    "target_height_frac": [0.77, 0.77],
                    "target_centers": [0.35, 0.65],
                    "gap_frac": 0.05,
                    "align_feet": {"enabled": True, "floor_y": 1180}
                },
                "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
            }
        _t.sleep(1.5)

    if st != "completed":
        print(f"[OAI] run status={st}")
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }

    # 5) Read last message
    mm = requests.get(f"{OAI_BASE}/threads/{thread_id}/messages?limit=1&order=desc",
          headers=OAI_HEADERS, timeout=60)
    if mm.status_code != 200:
        _log_oai("messages", f"{OAI_BASE}/threads/{thread_id}/messages?limit=1", mm.status_code, mm.text)
        return {
            "status": "accept",
            "user_notes": [],
            "backend_fixes": {
                "recompose": False,
                "issues": [],
                "target_height_frac": [0.77, 0.77],
                "target_centers": [0.35, 0.65],
                "gap_frac": 0.05,
                "align_feet": {"enabled": True, "floor_y": 1180}
            },
            "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
        }
    data = mm.json().get("data", [])
    if not data:
        return None

    parts = data[0].get("content", [])
    for p in parts:
        # 1) structured JSON (предпочтительно при response_format=json_schema)
        if p.get("type") == "output_json":
            res = p.get("json")
            if isinstance(res, dict):
                try:
                    os.makedirs("renders/temp", exist_ok=True)
                    dbg = os.path.join("renders/temp", f"oai_gate_{int(time.time())}.json")
                    with open(dbg, "w", encoding="utf-8") as f:
                        json.dump(res, f, ensure_ascii=False, indent=2)
                    print(f"[OAI] parsed gate (output_json) -> {dbg}")
                except Exception as _e:
                    print(f"[OAI] debug save error: {_e}")
                res = _normalize_gate(res) or {
                    "status":"accept","user_notes":[],
                    "backend_fixes":{"recompose":False,"issues":[],"target_height_frac":[0.77,0.77],"target_centers":[0.35,0.65],"gap_frac":0.05,"align_feet":{"enabled":True,"floor_y":1180}},
                    "runway_prompt_additions":BACKUP_PROMPT_ADDITIONS
                }
                return res
            # если по какой-то причине там строка — попробуем распарсить
            if isinstance(p.get("json"), str):
                try:
                    res = json.loads(p["json"])
                    return res
                except Exception as _:
                    pass

        # 2) текстовый JSON (fallback)
        if p.get("type") in ("output_text", "text"):
            raw = p.get("text")
            if isinstance(raw, dict):
                txt = raw.get("value")
            elif isinstance(raw, str):
                txt = raw
            else:
                txt = None
            if not txt:
                continue
            try:
                res = json.loads(txt)
                res = _normalize_gate(res)

                # лог ассистента в консоль + файл
                try:
                    os.makedirs("renders/temp", exist_ok=True)
                    dbg = os.path.join("renders/temp", f"oai_gate_{int(time.time())}.json")
                    with open(dbg, "w", encoding="utf-8") as f:
                        json.dump(res or {}, f, ensure_ascii=False, indent=2)
                    print(f"[OAI] parsed gate -> {dbg}")
                except Exception as _e:
                    print(f"[OAI] debug save error: {_e}")
                return res
            except Exception as e:
                print(f"[OAI] JSON parse error: {e}\n{txt[:500]}")
                return {
                    "status": "accept",
                    "user_notes": [],
                    "backend_fixes": {
                        "recompose": False,
                        "issues": [],
                        "target_height_frac": [0.77, 0.77],
                        "target_centers": [0.35, 0.65],
                        "gap_frac": 0.05,
                        "align_feet": {"enabled": True, "floor_y": 1180}
                    },
                    "runway_prompt_additions": BACKUP_PROMPT_ADDITIONS
                }

# ---------- ВЫРЕЗАНИЕ И СТАРТ-КАДР ----------
def cutout(path: str) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    cut = remove(im, session=RMBG_SESSION)  # важное: используем общую сессию
    # rembg может вернуть bytes — нормализуем к PIL.Image
    if isinstance(cut, (bytes, bytearray)):
        cut = Image.open(io.BytesIO(cut)).convert("RGBA")
    return cut

def _resize_fit_center(img: Image.Image, W: int, H: int) -> Image.Image:
    """Вписать картинку в холст W×H с сохранением пропорций и кропом по центру."""
    wr, hr = W / img.width, H / img.height
    scale = max(wr, hr)
    new = img.resize((int(img.width * scale), int(img.height * scale)), RESAMPLE.LANCZOS)
    x = (new.width - W) // 2
    y = (new.height - H) // 2
    return new.crop((x, y, x + W, y + H))

def make_start_frame(photo_paths: List[str], framing_key: str, bg_file: str, layout: dict | None = None) -> str:

    def _min_target_for(framing: str, people_count: int) -> float:
        """
        Нижняя граница целевой высоты (доля H) — чтобы фигуры не стали слишком маленькими.
        Значения подобраны консервативно.
        """
        if "В рост" in framing:
            return 0.82 if people_count >= 2 else 0.90
        elif "По пояс" in framing:
            return 0.66 if people_count >= 2 else 0.72
        else:  # По грудь
            return 0.58 if people_count >= 2 else 0.62

    W, H = 720, 1280
    base_id = uuid.uuid4().hex  # единый id для превью/метрик
    floor_margin = 10  # отступ «пола» снизу
    # Верхний «воздух» в зависимости от формата
    if "По грудь" in framing_key:
        HEADROOM_FRAC = 0.06
    elif "По пояс" in framing_key:
        HEADROOM_FRAC = 0.04
    else:
        HEADROOM_FRAC = 0.02

    # 1) фон
    bg = Image.open(bg_file).convert("RGB")
    bg = _resize_fit_center(bg, W, H)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.8))
    canvas = bg.convert("RGBA")

    # 2) вырезаем людей (используем smart_cutout, если он у тебя уже есть; иначе remove)
    cuts = []
    for p in photo_paths:
        im = Image.open(p).convert("RGBA")
        try:
            cut_rgba = smart_cutout(im)  # если вставлял ранее
        except NameError:
            cut_rgba = remove(im)
            if isinstance(cut_rgba, (bytes, bytearray)):
                cut_rgba = Image.open(io.BytesIO(cut_rgba)).convert("RGBA")
        cuts.append(cut_rgba)

    if MF_DEBUG:
        try:
            for i, c in enumerate(cuts):
                bb, yb = alpha_metrics(c)
                eff_h = max(1, (yb - bb[1] + 1))
                print(f"[LAYOUT] person#{i+1}: img={c.width}x{c.height} eff_h={eff_h} bbox={bb}")
        except Exception as _e:
            print(f"[LAYOUT] cut metrics err: {_e}")

    # 3) целевая высота фигуры относительно кадра
    two = (len(photo_paths) > 1)
    if "В рост" in framing_key:
        target_h = TH_FULL_DOUBLE if two else TH_FULL_SINGLE
    elif "По пояс" in framing_key:
        target_h = TH_WAIST_DOUBLE if two else TH_WAIST_SINGLE
    else:  # «По грудь»
        target_h = TH_CHEST_DOUBLE if two else TH_CHEST_SINGLE

    # жёсткий минимум — не даём превратиться в «карликов»
    target_h_min = _min_target_for(framing_key, len(photo_paths))
    if target_h < target_h_min:
        target_h = target_h_min

    def scale_to_target_effective(img: Image.Image, target: float) -> Image.Image:
        # Масштабируем по целевой высоте без уменьшения
        bbox, yb = alpha_metrics(img)
        eff_h = max(1, (yb - bbox[1] + 1))
        scale = (H * target) / eff_h
        # Ограничиваем максимальный апскейл (можно поднять через env MAX_UPSCALE)
        if scale > MAX_UPSCALE:
            scale = MAX_UPSCALE
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        return img.resize((nw, nh), RESAMPLE.LANCZOS)


    def place_y_for_floor(img: Image.Image) -> int:
        """Вычисляет y так, чтобы нижняя «подошва» стала на линию пола."""
        bbox, yb = alpha_metrics(img)
        eff_h = (yb - bbox[1] + 1)
        # где должен оказаться верх bbox относительно кадра
        y_top_content = H - floor_margin - eff_h
        # переводим в y верхнего левого угла изображения
        y_img = y_top_content - bbox[1]
        return int(y_img)

    def draw_with_shadow(base: Image.Image, person: Image.Image, x: int, y: int):
        alpha = person.split()[-1]
        soft = alpha.filter(ImageFilter.GaussianBlur(6))
        shadow = Image.new("RGBA", person.size, (0, 0, 0, 0))
        shadow.putalpha(soft.point(lambda a: int(a * 0.45)))
        base.alpha_composite(shadow, (x, y + 8))
        base.alpha_composite(person, (x, y))

    def _rect_at(x, y, img):
        bx, by, bx1, by1 = alpha_metrics(img)[0]
        return (x + bx, y + by, x + bx1, y + by1)

    def _draw_debug_boxes(base: Image.Image, rects: list[tuple[int,int,int,int]]):
        if not START_OVERLAY_DEBUG:
            return
        ov = Image.new("RGBA", base.size, (0,0,0,0))
        g = ImageDraw.Draw(ov)
        # контентные рамки (красные)
        for r in rects:
            g.rectangle(r, outline=(255, 0, 0, 200), width=3)
        # зелёная «safe»-рамка по полям кадра
        m = 20
        g.rectangle((m, m, base.width - m, base.height - m), outline=(0, 255, 0, 180), width=2)
        base.alpha_composite(ov)

    if len(cuts) == 1:
        P = scale_to_target_effective(cuts[0], target_h)
        x = (W - P.width) // 2
        y = place_y_for_floor(P)

        # рассчитать видимую высоту фигуры в кадре до отрисовки
        def rect_at_single(px, py, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (px + bx, py + by, px + bx1, py + by1)

        r = rect_at_single(x, y, P)
        group_h = r[3] - r[1]

        # выбрать минимальную долю по формату
        fmt = "В рост" if "В рост" in framing_key else ("По пояс" if "По пояс" in framing_key else "По грудь")
        min_h_frac = MIN_SINGLE_FRAC[fmt]

        # если фигура заняла слишком мало — мягко подрастим
        if group_h < int(min_h_frac * H):
            need = (min_h_frac * H) / max(1, group_h)
            cap = SINGLE_UPSCALE_CAP
            new_target = min(target_h * need, target_h * cap)
            if new_target > target_h:
                P = scale_to_target_effective(cuts[0], new_target)
                x = (W - P.width) // 2
                y = place_y_for_floor(P)

        # финальные отступы от краёв
        margin = 20
        x = max(margin, min(W - P.width - margin, x))
        top_margin = max(margin, int(HEADROOM_FRAC * H))
        y = max(top_margin, min(H - P.height - margin, y))

        # --- дополнительный мягкий масштаб/сдвиг из layout_feedback (если есть)
        if layout and isinstance(layout, dict):
            # Одного человека трактуем как "левый"
            scl = int(layout.get("scale_left_pct", 0) or 0)
            dxl = int(layout.get("shift_left_px", 0) or 0)

            # мягкий коэффициент масштаба (±20%)
            if scl != 0:
                k = 1.0 + max(-0.20, min(0.20, scl / 100.0))
                nw, nh = max(1, int(P.width * k)), max(1, int(P.height * k))
                P = P.resize((nw, nh), RESAMPLE.LANCZOS)
                # пересчёт y (ставим на пол после рескейла)
                y = place_y_for_floor(P)

            # сдвиг: shift_left_px>0 — двигаем ВЛЕВО, поэтому dx = -shift_left_px
            if dxl != 0:
                x += int(-dxl)

            # Страховка от выхода за кадр
            margin = 20
            x = max(margin, min(W - P.width - margin, x))
            y = max(margin, min(H - P.height - margin, y))

        # Применяем layout_hint для 1-й фигуры (используем поля "left")
        if layout:
            try:
                sh = int(layout.get("shift_left_px", 0))
                sc = int(layout.get("scale_left_pct", 0))
                # масштаб
                if sc:
                    factor = max(0.7, min(1.4, 1.0 + sc / 100.0))
                    nw = max(1, int(P.width * factor))
                    nh = max(1, int(P.height * factor))
                    P = P.resize((nw, nh), RESAMPLE.LANCZOS)
                    y = place_y_for_floor(P)
                    # пересчёт x в центре
                    x = (W - P.width) // 2
                # сдвиг
                if sh:
                    x += sh
                # в границах кадра
                margin = 20
                x = max(margin, min(W - P.width - margin, x))
                y = max(margin, min(H - P.height - margin, y))
            except Exception as _e:
                print(f"[START_FRAME:1] layout_hint ignored: {_e}")

        # --- Анти-карлик: гарантируем минимальную «видимую» высоту силуэта
        min_frac = _min_frac_for(framing_key, 1)

        def _visible_frac(img: Image.Image) -> float:
            bb, yb = alpha_metrics(img)
            eff_h = max(1, (yb - bb[1] + 1))
            return eff_h / H

        grow_tries = 0
        while _visible_frac(P) < min_frac and grow_tries < 12:
            # пробуем чуть увеличить целевой таргет
            new_target = min(target_h * 1.04, 0.98)  # не просим >98% высоты кадра
            newP = scale_to_target_effective(cuts[0], new_target)
            # пересчёт позиции с сохранением центра
            cx = x + P.width // 2
            cy_floor = place_y_for_floor(newP)
            newx = cx - newP.width // 2
            margin = 20
            newx = max(margin, min(W - newP.width - margin, newx))
            newy = max(margin, min(H - newP.height - margin, cy_floor))

            # если упрёмся в верх/бока — прекращаем рост
            if newy <= margin or newx <= margin or (newx + newP.width) >= (W - margin):
                break

            # применяем увеличение
            P, x, y = newP, newx, newy
            target_h = new_target
            grow_tries += 1

        draw_with_shadow(canvas, P, x, y)
        # Диагностический оверлей (по желанию)
        try:
            _draw_debug_boxes(canvas, [_rect_at(x, y, P)])
        except Exception:
            pass

    else:
        L = scale_to_target_effective(cuts[0], target_h)
        R = scale_to_target_effective(cuts[1], target_h)
        base_target = target_h  # запомним исходную цель для пары, ниже не дадим уйти сильно меньше
        if MF_DEBUG:
            print(f"[LAYOUT] target_h={target_h:.3f} base_target={base_target:.3f}  L={L.width}x{L.height}  R={R.width}x{R.height}")

        # стартовые центры — чуть ближе к краям
        lx = int(W * CENTER_BIAS_FRAC) - L.width // 2
        rx = int(W * (1 - CENTER_BIAS_FRAC)) - R.width // 2
        yl = place_y_for_floor(L)
        yr = place_y_for_floor(R)

        def rect_at(x, y, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (x + bx, y + by, x + bx1, y + by1)

        def horizontal_overlap(a, b):
            return not (a[2] + MIN_GAP_PX <= b[0] or b[2] + MIN_GAP_PX <= a[0])

        # 1) начальное разведение если есть перекрытие
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)

        tries = 0
        margin = 20
        shrink_once = False
        while horizontal_overlap(ra, rb) and tries < 30:
            # пробуем раздвинуть симметрично
            new_lx = lx - 3
            new_rx = rx + 3

            if new_lx >= margin and new_rx + R.width <= W - margin:
                lx = new_lx
                rx = new_rx
            else:
                # если в стороны не лезет, позволим ОДИН раз очень мягко уменьшить
                if not shrink_once:
                    target_h = max(base_target * 0.94, target_h * 0.97)
                    if target_h < target_h_min:
                        target_h = target_h_min
                    L = scale_to_target_effective(cuts[0], target_h)
                    R = scale_to_target_effective(cuts[1], target_h)
                    yl = place_y_for_floor(L); yr = place_y_for_floor(R)
                    # пересчитаем старт ближе к центру
                    lx = int(W * 0.42) - L.width // 2
                    rx = int(W * 0.58) - R.width // 2
                    shrink_once = True
                else:
                    # дальше не уменьшаем — выходим из цикла, остальное поправит safety
                    break

            ra = rect_at(lx, yl, L)
            rb = rect_at(rx, yr, R)
            tries += 1

        # 2) подтягиваем людей к оптимальному расстоянию 
        def inner_gap_px(a, b):
            # горизонтальное расстояние между контентными прямоугольниками (не по прозрачным краям)
            return max(0, b[0] - a[2])

        # Целевой зазор = max(фракции кадра, фракции средней ширины фигур, минимального зазора)
        mean_width = (L.width + R.width) / 2
        ideal_gap_w = int(IDEAL_GAP_FRAC * W)   # 5% кадра (см. константу IDEAL_GAP_FRAC)
        ideal_gap_p = int(0.12 * mean_width)    # 12% средней ширины людей
        ideal_gap   = max(MIN_GAP_PX, ideal_gap_w, ideal_gap_p)

        pull_tries = 0
        while inner_gap_px(ra, rb) > ideal_gap and pull_tries < 25:
            excess = inner_gap_px(ra, rb) - ideal_gap
            step = max(1, min(8, excess // 2))
            lx += step
            rx -= step
            ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
            # не допускаем перекрытия — откатываем последний шаг
            if horizontal_overlap(ra, rb):
                lx -= step; rx += step
                break
            pull_tries += 1

        # 3) финальная проверка и единовременная коррекция размера
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)

        def any_outside(r):
            x0,y0,x1,y1 = r
            margin = 20  # увеличенный отступ
            return x0 < margin or x1 > W-margin or y0 < margin or y1 > H-margin

        def headroom_ok(r):
            return r[1] > int(HEADROOM_FRAC * H)

        max_person_width = int(0.48 * W)

        # Если есть проблемы, делаем ОДНУ коррекцию размера
        problems = []
        if any_outside(ra) or any_outside(rb): 
            problems.append("outside")
        if not headroom_ok(ra) or not headroom_ok(rb): 
            problems.append("headroom")
        if L.width > max_person_width or R.width > max_person_width: 
            problems.append("too_wide")

        if problems:
            # нижняя граница — не меньше 90% исходного плана для пары
            min_allowed = base_target * 0.90

            if "headroom" in problems:
                top_margin = int(0.01 * H)
                max_height = H - floor_margin - top_margin
                bbox_l, _ = alpha_metrics(L)
                bbox_r, _ = alpha_metrics(R)
                eff_h_l = (bbox_l[3] - bbox_l[1])
                eff_h_r = (bbox_r[3] - bbox_r[1])
                new_target = min(max_height / max(eff_h_l, eff_h_r), target_h)
                new_target = max(new_target, min_allowed)
            else:
                new_target = max(target_h * 0.97, min_allowed)
                if target_h < target_h_min:
                    target_h = target_h_min

            target_h = new_target  # зафиксируем новое целевое
            L = scale_to_target_effective(cuts[0], target_h)
            R = scale_to_target_effective(cuts[1], target_h)
            yl = place_y_for_floor(L); yr = place_y_for_floor(R)
            lx = int(W * 0.42) - L.width // 2
            rx = int(W * 0.58) - R.width // 2

        # 3b) центрирующая полоса под фон — сдвигаем пару внутрь полосы, сохраняя текущий зазор
        _p = _bg_layout_presets(bg_file)
        band_left  = int(W * (_p["center_frac"] - _p["band_frac"] / 2.0))
        band_right = int(W * (_p["center_frac"] + _p["band_frac"] / 2.0))

        ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
        cxL = (ra[0] + ra[2]) // 2
        cxR = (rb[0] + rb[2]) // 2

        shift_pair = 0
        if cxL < band_left:
            shift_pair = max(shift_pair, band_left - cxL)
        if cxR > band_right:
            shift_pair = min(shift_pair, band_right - cxR)  # это отрицательное смещение, если надо вправо сузить

        lx += shift_pair
        rx += shift_pair
        margin = 20
        lx = max(margin, min(W - L.width - margin, lx))
        rx = max(margin, min(W - R.width - margin, rx))

        # 3c) если сверху слишком много воздуха — слегка увеличим людей ОДИН РАЗ
        ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
        topY = min(ra[1], rb[1])
        if topY > int(_p["top_headroom_max"] * H):
            new_target = min(base_target * 1.08, 0.94)  # не раздуваем выше 94% кадра и не более +8% к исходной цели
            if new_target > target_h:
                target_h = new_target
                L = scale_to_target_effective(cuts[0], target_h)
                R = scale_to_target_effective(cuts[1], target_h)
                yl = place_y_for_floor(L); yr = place_y_for_floor(R)
                # снова выставим базовые позиции ближе к центру
                lx = int(W * CENTER_BIAS_FRAC) - L.width // 2
                rx = int(W * (1 - CENTER_BIAS_FRAC)) - R.width // 2

                # быстрый доводочный цикл: целевой зазор
                def _inner_gap(a, b): return max(0, b[0] - a[2])
                ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
                ideal_gap2 = max(MIN_GAP_PX, int(0.15 * ((L.width + R.width) / 2)))
                for _ in range(14):
                    if horizontal_overlap(ra, rb) or _inner_gap(ra, rb) <= ideal_gap2:
                        break
                    step = max(1, min(8, (_inner_gap(ra, rb) - ideal_gap2) // 2))
                    lx += step; rx -= step
                    ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)

        # Окончательная корректировка позиций в пределах кадра с увеличенным отступом
        margin = 20
        lx = max(margin, min(W - L.width - margin, lx))
        rx = max(margin, min(W - R.width - margin, rx))

        # Финальная проверка и корректировка перекрытий
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)
        if horizontal_overlap(ra, rb):
            # если все еще перекрываются, дополнительно раздвигаем или уменьшаем
            center = W // 2
            if lx + L.width // 2 < center:
                # левая фигура левее центра - сдвигаем влево
                lx = max(margin, lx - 10)
            if rx + R.width // 2 > center:
                # правая фигура правее центра - сдвигаем вправо  
                rx = min(W - R.width - margin, rx + 10)
        if MF_DEBUG:
            try:
                def rect_at(x, y, img):
                    bx, by, bx1, by1 = alpha_metrics(img)[0]
                    return (x + bx, y + by, x + bx1, y + by1)

                ra = rect_at(lx, yl, L)
                rb = rect_at(rx, yr, R)
                gap = max(0, rb[0] - ra[2])
                print(f"[LAYOUT] final: lx={lx}, yl={yl}, rx={rx}, yr={yr}, gap={gap}px  Lw={L.width} Rw={R.width}")

                # рисуем отладочный оверлей
                dbg = canvas.copy()
                drw = ImageDraw.Draw(dbg)
                drw.rectangle(ra, outline=(255,0,0,255), width=3)  # L - красный
                drw.rectangle(rb, outline=(0,128,255,255), width=3)  # R - синий
                drw.text((ra[0]+4, ra[1]+4), f"L {L.width}x{L.height}", fill=(255,0,0,255))
                drw.text((rb[0]+4, rb[1]+4), f"R {R.width}x{R.height}", fill=(0,128,255,255))
                drw.text((min(ra[2], rb[0])+4, max(ra[1], rb[1])+4), f"gap={gap}px", fill=(255,255,0,255))
                os.makedirs("renders/temp", exist_ok=True)
                dbg_path = os.path.join("renders/temp", f"start_debug_{int(time.time())}.png")
                dbg.save(dbg_path, "PNG")
                print(f"[LAYOUT] debug overlay -> {dbg_path}")
            except Exception as _e:
                print(f"[LAYOUT] debug overlay err: {_e}")

        # --- анти-микро: если пара заняла слишком мало высоты кадра — мягко подрастим обе фигуры
        def rect_at(x, y, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (x + bx, y + by, x + bx1, y + by1)

        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)
        group_top = min(ra[1], rb[1])
        group_bottom = max(ra[3], rb[3])
        group_h = group_bottom - group_top

        fmt = "В рост" if "В рост" in framing_key else ("По пояс" if "По пояс" in framing_key else "По грудь")
        min_group_frac = MIN_PAIR_FRAC[fmt]

        if group_h < int(min_group_frac * H):
            # насчитали, насколько надо вырасти относительно текущей целевой высоты
            need = (min_group_frac * H) / max(1, group_h)
            new_target = min(target_h * need, target_h * PAIR_UPSCALE_CAP)

            if new_target > target_h:
                # пересобираем L/R с новым target и снова аккуратно расставляем
                target_h = new_target
                L = scale_to_target_effective(cuts[0], target_h)
                R = scale_to_target_effective(cuts[1], target_h)

                lx = int(W * CENTER_BIAS_FRAC) - L.width // 2
                rx = int(W * (1 - CENTER_BIAS_FRAC)) - R.width // 2
                yl = place_y_for_floor(L)
                yr = place_y_for_floor(R)

                # быстрый проход устранения перекрытий + подтяжка к идеальному зазору (короче предыдущего)
                def horizontal_overlap(a, b):
                    return not (a[2] + MIN_GAP_PX <= b[0] or b[2] + MIN_GAP_PX <= a[0])

                ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
                tries = 0
                while horizontal_overlap(ra, rb) and tries < 24:
                    if (lx + L.width//2) <= (rx + R.width//2):
                        lx -= 3; rx += 3
                    else:
                        lx += 3; rx -= 3
                    ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
                    tries += 1

                # подтянуть к разумному зазору, но не допустить перекрытия
                def inner_gap_px(a, b): return max(0, b[0] - a[2])
                ideal_gap = max(MIN_GAP_PX, int(0.15 * ((L.width + R.width) / 2)))
                pulls = 0
                while inner_gap_px(ra, rb) > ideal_gap and pulls < 20:
                    step = max(1, min(8, (inner_gap_px(ra, rb) - ideal_gap) // 2))
                    lx += step; rx -= step
                    ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
                    if horizontal_overlap(ra, rb):
                        lx -= step; rx += step
                        break
                    pulls += 1

                # отступы от краёв
                margin = 20
                lx = max(margin, min(W - L.width - margin, lx))
                rx = max(margin, min(W - R.width - margin, rx))

                # финальная страховка по «воздуху» сверху (не подпираем совсем)
                def headroom_ok(r): return r[1] > int(0.01 * H)
                ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
                if not headroom_ok(ra) or not headroom_ok(rb):
                    # если упёрлись в верх — слегка поджать (2%)
                    t2 = target_h * 0.98
                    if target_h < target_h_min:
                        target_h = target_h_min
                    L = scale_to_target_effective(cuts[0], t2)
                    R = scale_to_target_effective(cuts[1], t2)
                    yl = place_y_for_floor(L); yr = place_y_for_floor(R)
                    lx = int(W * CENTER_BIAS_FRAC) - L.width // 2
                    rx = int(W * (1 - CENTER_BIAS_FRAC)) - R.width // 2

        # --- дополнительный мягкий масштаб/сдвиг из layout_feedback (если есть)
        if layout and isinstance(layout, dict):
            scl_l = int(layout.get("scale_left_pct", 0)  or 0)
            scl_r = int(layout.get("scale_right_pct", 0) or 0)
            dx_l  = int(layout.get("shift_left_px", 0)   or 0)
            dx_r  = int(layout.get("shift_right_px", 0)  or 0)

            # масштабируем в пределах ±20%
            def _apply_scale(img: Image.Image, scl_pct: int) -> Image.Image:
                if scl_pct == 0:
                    return img
                k = 1.0 + max(-0.20, min(0.20, scl_pct / 100.0))
                nw, nh = max(1, int(img.width * k)), max(1, int(img.height * k))
                return img.resize((nw, nh), RESAMPLE.LANCZOS)

            L = _apply_scale(L, scl_l)
            R = _apply_scale(R, scl_r)
            # после рескейла пересчитываем "пол"
            yl = place_y_for_floor(L)
            yr = place_y_for_floor(R)

            # смещения:
            # shift_left_px>0 — двигай ЛЕВОГО влево → x -= shift_left_px
            # shift_right_px>0 — двигай ПРАВОГО вправо → x += shift_right_px
            if dx_l != 0:
                lx += int(-dx_l)
            if dx_r != 0:
                rx += int(dx_r)

            # страхуемся от выхода за кадр
            margin = 20
            lx = max(margin, min(W - L.width - margin, lx))
            rx = max(margin, min(W - R.width - margin, rx))

            # финальная проверка перекрытия после ручных сдвигов
            ra = rect_at(lx, yl, L)
            rb = rect_at(rx, yr, R)
            if horizontal_overlap(ra, rb):
                # мягко раздвинем на MIN_GAP_PX
                gap = max(0, min(12, MIN_GAP_PX // 2))
                lx -= gap
                rx += gap
                # и заново в границы
                lx = max(margin, min(W - L.width - margin, lx))
                rx = max(margin, min(W - R.width - margin, rx))

        # --- Применяем layout_hint (масштаб/сдвиг для L/R), затем «автоподтяжка» ближе без перекрытия
        try:
            if layout:
                sl = int(layout.get("scale_left_pct", 0))
                sr = int(layout.get("scale_right_pct", 0))
                shl = int(layout.get("shift_left_px", 0))
                shr = int(layout.get("shift_right_px", 0))

                def _apply_scale(img, delta_pct):
                    if not delta_pct:
                        return img
                    factor = max(0.7, min(1.4, 1.0 + delta_pct / 100.0))
                    nw = max(1, int(img.width * factor))
                    nh = max(1, int(img.height * factor))
                    return img.resize((nw, nh), RESAMPLE.LANCZOS)

                if sl:
                    L = _apply_scale(L, sl)
                    yl = place_y_for_floor(L)
                if sr:
                    R = _apply_scale(R, sr)
                    yr = place_y_for_floor(R)

                if shl:
                    lx += shl
                if shr:
                    rx += shr

                # в пределах кадра
                margin = 20
                lx = max(margin, min(W - L.width - margin, lx))
                rx = max(margin, min(W - R.width - margin, rx))

                # если перекрылись — раздвигаем минимально поровну
                ra = rect_at(lx, yl, L)
                rb = rect_at(rx, yr, R)
                if horizontal_overlap(ra, rb):
                    step = 2
                    tries2 = 0
                    while horizontal_overlap(ra, rb) and tries2 < 80:
                        if lx + L.width // 2 <= W // 2:
                            lx -= step
                        else:
                            lx += step
                        if rx + R.width // 2 >= W // 2:
                            rx += step
                        else:
                            rx -= step
                        lx = max(margin, min(W - L.width - margin, lx))
                        rx = max(margin, min(W - R.width - margin, rx))
                        ra = rect_at(lx, yl, L)
                        rb = rect_at(rx, yr, R)
                        tries2 += 1
        except Exception as _e:
            print(f"[START_FRAME:2] layout_hint ignored: {_e}")

        # Автоподтяжка ближе (если между людьми слишком большой зазор)
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)

        def _inner_gap_px(a, b):
            return max(0, b[0] - a[2])

        min_ideal_gap = max(MIN_GAP_PX, int(IDEAL_GAP_FRAC * W))  # ~5% ширины кадра
        gap = _inner_gap_px(ra, rb)
        if gap > min_ideal_gap:
            tries3 = 0
            while gap > min_ideal_gap and tries3 < 40:
                lx += 2
                rx -= 2
                ra = rect_at(lx, yl, L)
                rb = rect_at(rx, yr, R)
                if horizontal_overlap(ra, rb):
                    # откатываем последний шаг, чтобы не перекрыться
                    lx -= 2
                    rx += 2
                    break
                gap = _inner_gap_px(ra, rb)
                tries3 += 1

        # --- Анти-карлик: обе фигуры не должны быть «слишком маленькими»
        min_frac = _min_frac_for(framing_key, 2)

        def _visible_frac(img: Image.Image) -> float:
            bb, yb = alpha_metrics(img)
            eff_h = max(1, (yb - bb[1] + 1))
            return eff_h / H

        def _any_outside_rects():
            ra_ = rect_at(lx, yl, L)
            rb_ = rect_at(rx, yr, R)
            return any_outside(ra_) or any_outside(rb_)

        def _overlap_now():
            ra_ = rect_at(lx, yl, L)
            rb_ = rect_at(rx, yr, R)
            return horizontal_overlap(ra_, rb_)

        grow_tries = 0
        # будем растить ОБЕ фигуры синхронно, сохраняя их центры и зазор
        while (min(_visible_frac(L), _visible_frac(R)) < min_frac) and grow_tries < 12:
            new_target = min(target_h * 1.04, 0.96)
            newL = scale_to_target_effective(cuts[0], new_target)
            newR = scale_to_target_effective(cuts[1], new_target)

            # сохраняем центры и относительный зазор
            lcx = lx + L.width // 2
            rcx = rx + R.width // 2
            # новый Y по полу
            new_yl = place_y_for_floor(newL)
            new_yr = place_y_for_floor(newR)
            # выставляем по центрам
            new_lx = lcx - newL.width // 2
            new_rx = rcx - newR.width // 2

            margin = 20
            new_lx = max(margin, min(W - newL.width - margin, new_lx))
            new_rx = max(margin, min(W - newR.width - margin, new_rx))

            # проверим границы/верх/перекрытие
            L_tmp, R_tmp = newL, newR
            yl_tmp, yr_tmp = new_yl, new_yr
            lx_tmp, rx_tmp = new_lx, new_rx

            ra_tmp = rect_at(lx_tmp, yl_tmp, L_tmp)
            rb_tmp = rect_at(rx_tmp, yr_tmp, R_tmp)

            # если «подпираем» верх — прекращаем рост
            if not headroom_ok(ra_tmp) or not headroom_ok(rb_tmp):
                break
            # если вывалились за кадр — прекращаем рост
            if _any_outside_rects():
                break
            # если начали перекрываться — попробуем слегка раздвинуть
            if horizontal_overlap(ra_tmp, rb_tmp):
                # симметрично раздвинем на небольшой шаг
                step = max(4, int(0.01 * W))
                lx_tmp = max(margin, lx_tmp - step)
                rx_tmp = min(W - R_tmp.width - margin, rx_tmp + step)
                ra_tmp = rect_at(lx_tmp, yl_tmp, L_tmp)
                rb_tmp = rect_at(rx_tmp, yr_tmp, R_tmp)
                # если всё равно перекрываются — прекращаем рост
                if horizontal_overlap(ra_tmp, rb_tmp):
                    break

            # применяем увеличение
            L, R = L_tmp, R_tmp
            lx, rx = lx_tmp, rx_tmp
            yl, yr = yl_tmp, yr_tmp
            target_h = new_target
            grow_tries += 1

        # финальная проверка на перекрытия после роста (на всякий)
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)
        if horizontal_overlap(ra, rb):
            center = W // 2
            if lx + L.width // 2 < center:
                lx = max(margin, lx - 8)
            if rx + R.width // 2 > center:
                rx = min(W - R.width - margin, rx + 8)

        draw_with_shadow(canvas, L, lx, yl)
        draw_with_shadow(canvas, R, rx, yr)
        # Диагностический оверлей (по желанию)
        try:
            _draw_debug_boxes(canvas, [_rect_at(lx, yl, L), _rect_at(rx, yr, R)])
        except Exception:
            pass

    out = f"uploads/start_{base_id}.png"
    # --- МЕТРИКИ КОМПОНОВКИ ---
    metrics = {
        "W": W, "H": H,
        "framing": framing_key,
    }

    def _abs_rect(x, y, img):
        (bx, by, bx1, by1), yb = alpha_metrics(img)
        return [x + bx, y + by, x + bx1, y + by1], yb + y

    if len(cuts) == 1:
        rP, fy = _abs_rect(x, y, P)
        h_px = rP[3] - rP[1]
        w_px = rP[2] - rP[0]
        metrics["L"] = {
            "rect_abs": rP,
            "height_px": int(h_px),
            "width_px": int(w_px),
            "height_frac": float(h_px) / H,
            "center_x_frac": float((rP[0]+rP[2])/2) / W,
            "scale": float(P.width) / max(1.0, cuts[0].width),
            "floor_y": int(fy)
        }
    else:
        rL, fyl = _abs_rect(lx, yl, L)
        rR, fyr = _abs_rect(rx, yr, R)
        hL = rL[3]-rL[1]; wL = rL[2]-rL[0]
        hR = rR[3]-rR[1]; wR = rR[2]-rR[0]
        gap_px = max(0, rR[0] - rL[2])
        metrics["L"] = {
            "rect_abs": rL,
            "height_px": int(hL),
            "width_px": int(wL),
            "height_frac": float(hL)/H,
            "center_x_frac": float((rL[0]+rL[2])/2)/W,
            "scale": float(L.width)/max(1.0, cuts[0].width),
            "floor_y": int(fyl)
        }
        metrics["R"] = {
            "rect_abs": rR,
            "height_px": int(hR),
            "width_px": int(wR),
            "height_frac": float(hR)/H,
            "center_x_frac": float((rR[0]+rR[2])/2)/W,
            "scale": float(R.width)/max(1.0, cuts[1].width),
            "floor_y": int(fyr)
        }
        metrics["gap_px"]  = int(gap_px)
        metrics["gap_frac"]= float(gap_px)/W

    # сохраняем сайдкары всегда в debug-режиме или при превью
    if OAI_DEBUG or PREVIEW_START_FRAME:
        _save_layout_debug(canvas, metrics, base_id)
    canvas.save(out, "PNG")
    return out

# ---------- ПОСТ-ОБРАБОТКА через ffmpeg (wm + музыка + титр + склейка) ----------
def create_title_image(width: int, height: int, text: str, output_path: str):
    """Создает изображение с титром с автоматическим подбором размера шрифта"""
    title_img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(title_img)

    # Автоматический подбор размера шрифта
    max_width = width - 40  # Отступ 20 пикселей с каждой стороны
    font_size = 60  # Начинаем с большого шрифта

    while font_size > 12:  # Минимальный размер шрифта
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except:
            font = ImageFont.load_default()

        # Измеряем ширину текста с текущим шрифтом
        bbox = d.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]

        if text_width <= max_width:
            break  # Шрифт подходит

        font_size -= 2  # Уменьшаем размер шрифта

    # Рисуем текст по центру
    d.text((width//2, height//2), text, fill=(255,255,255), font=font, anchor="mm")
    title_img.save(output_path)
    return output_path

def postprocess_concat_ffmpeg(video_paths: List[str], music_path: str|None, title_text: str, save_as: str, bg_overlay_file: str|None = None) -> str:
    """Постобработка видео через ffmpeg вместо MoviePy"""
    import tempfile

    # Создаем временные файлы
    temp_dir = "renders/temp"
    os.makedirs(temp_dir, exist_ok=True)

    # 1. Создаем финальный титр как изображение
    title_img_path = f"{temp_dir}/title.png"
    create_title_image(720, 1280, title_text, title_img_path)

    # 2. Конвертируем титр в 2-секундное видео
    title_video_path = f"{temp_dir}/title_video.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", title_img_path, 
        "-c:v", "libx264", "-t", "2", "-pix_fmt", "yuv420p", "-r", "24",
        title_video_path
    ], check=True, capture_output=True)

    # 3. Создаем файл со списком видео для склейки
    concat_list_path = f"{temp_dir}/concat_list.txt"
    with open(concat_list_path, "w") as f:
        for video_path in video_paths:
            f.write(f"file '{os.path.abspath(video_path)}'\n")
        f.write(f"file '{os.path.abspath(title_video_path)}'\n")

    # 4. Склеиваем все видео (включая титр)
    concat_video_path = f"{temp_dir}/concat_video.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
        "-c", "copy", concat_video_path
    ], check=True, capture_output=True)

    # 4.5. Очень деликатная анимация фона сверху (если передан bg_overlay_file)
    bg_anim_video_path = concat_video_path
    if bg_overlay_file and os.path.isfile(bg_overlay_file):
        try:
            # Создаём плывущую полупрозрачную «туманность» из того же фона:
            # - масштаб до 720x1280
            # - сильный blur
            # - альфа ~0.08
            # - медленный сдвиг по X (2 px/сек)
            bg_anim_video_path = f"{temp_dir}/with_bg_anim.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-i", concat_video_path,
                "-loop", "1", "-i", bg_overlay_file,
                "-filter_complex",
                "[1:v]scale=720:1280,boxblur=25:1,format=rgba,colorchannelmixer=aa=0.08,setsar=1[ov];"
                "[0:v][ov]overlay=x='t*2':y=0:shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
                bg_anim_video_path
            ], check=True, capture_output=True)
        except Exception as e:
            print(f"BG overlay skipped: {e}")
    else:
        print("BG overlay disabled (no file)")

    # 5. Добавляем водяной знак если есть
    wm_video_path = bg_anim_video_path
    if os.path.isfile(WATERMARK_PATH):
        wm_video_path = f"{temp_dir}/with_watermark.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", bg_anim_video_path, "-i", WATERMARK_PATH,
            "-filter_complex", "[1:v]scale=120:-1[wm];[0:v][wm]overlay=W-w-24:24",
            "-c:a", "copy", wm_video_path
        ], check=True, capture_output=True)

    # 6. Добавляем музыку если есть
    final_video_path = wm_video_path
    if music_path and os.path.isfile(music_path):
        final_video_path = save_as
        # Простая замена аудиодорожки музыкой (зацикленной)
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", music_path, "-i", wm_video_path,
            "-map", "1:v", "-map", "0:a", "-c:v", "copy", "-c:a", "aac", 
            "-shortest", "-af", "volume=0.6", final_video_path
        ], check=True, capture_output=True)
    else:
        # Если нет музыки, просто копируем итоговое видео
        subprocess.run(["cp", wm_video_path, save_as], check=True)

    return save_as

def cleanup_dir_keep_last_n(dir_path: str, keep_n: int = 10, extensions: tuple[str, ...] = ()):
    try:
        items = []
        for name in os.listdir(dir_path):
            p = os.path.join(dir_path, name)
            if os.path.isfile(p):
                if not extensions or name.lower().endswith(extensions):
                    items.append((p, os.path.getmtime(p)))
        items.sort(key=lambda x: x[1], reverse=True)
        for p, _ in items[keep_n:]:
            try:
                os.remove(p)
            except Exception:
                pass
    except FileNotFoundError:
        pass

def cleanup_artifacts(keep_last: int = 10):
    # Полностью чистим временную папку рендеров (кроме режима отладки)
    if not OAI_DEBUG:
        shutil.rmtree("renders/temp", ignore_errors=True)
    # Оставляем только N последних оригиналов и финалов
    cleanup_dir_keep_last_n("uploads", keep_n=keep_last, extensions=(".jpg", ".jpeg", ".png", ".webp"))
    cleanup_dir_keep_last_n("renders", keep_n=keep_last, extensions=(".mp4", ".mov", ".mkv", ".webm"))

def _download_tg_photo(file_id: str, uid: int) -> str:
    fi = bot.get_file(file_id)
    content = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{fi.file_path}", timeout=120).content
    pth = f"uploads/{uid}_{int(time.time())}_{uuid.uuid4().hex}.jpg"
    with open(pth, "wb") as f:
        f.write(content)
    return pth

# ---------- ХЭНДЛЕРЫ ----------
@bot.message_handler(commands=["start","reset"])
def start_cmd(m: telebot.types.Message):
    uid = m.from_user.id
    # Сброс текущего состояния и показ главного меню
    users[uid] = new_state()
    show_main_menu(uid, 'Выберите пункт меню или перейдите к созданию видео, нажав «Сделать видео».')

# Главное меню (кнопка)
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_MAIN)
def on_menu_main(m: telebot.types.Message):
    uid = m.from_user.id
    # Не трогаем текущую генерацию, просто показываем меню
    show_main_menu(uid)

# Запуск мастера (кнопка «Сделать видео»)
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_START)
def on_menu_start_wizard(m: telebot.types.Message):
    uid = m.from_user.id
    users[uid] = new_state()
    bot.send_message(
        uid,
        "Шаг 1/5. Выберите <b>формат кадра</b>.",
        reply_markup=kb_formats()
    )

# Стоимость
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_PRICE)
def on_menu_price(m: telebot.types.Message):
    uid = m.from_user.id
    price_text = (
        "<b>Стоимость</b>\n\n"
        "• 5 сек — <b>бесплатно</b>\n"
        "• Любой другой сюжет (10с) — <b>100</b>\n"
        "• Объединение сюжетов — сумма цен выбранных сюжетов\n"
        "• Музыка — <b>50</b>\n"
        "• Финальные титры — <b>50</b>\n"
        "• Вторая вариация (другой сервис генерации) — <b>+50%</b> к итоговой стоимости\n"
    )
    bot.send_message(uid, price_text, reply_markup=kb_main_menu())

# Инструкция
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_GUIDE)
def on_menu_guide(m: telebot.types.Message):
    uid = m.from_user.id
    guide = (
        "<b>Как сделать видео</b>\n"
        "1) Нажмите «Сделать видео».\n"
        "2) Выберите формат кадра.\n"
        "3) Выберите сюжеты (можно несколько) → «✅ Выбрано, дальше».\n"
        "4) Выберите фон.\n"
        "5) Выберите музыку (или «Без музыки»).\n"
        "6) Пришлите 1–2 фото анфас (каждого — отдельно).\n"
        "7) Дождитесь рендера — итоговое видео придёт сюда.\n\n"
        "Подсказка: чем лучше освещение и контраст с фоном на фото — тем чище вырезание."
    )
    bot.send_message(uid, guide, reply_markup=kb_main_menu())

# Примеры работ
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_DEMO)
def on_menu_demo(m: telebot.types.Message):
    uid = m.from_user.id
    demo_dir = "assets/examples"
    paths = [
        os.path.join(demo_dir, "example1.mp4"),
        os.path.join(demo_dir, "example2.mp4"),
        os.path.join(demo_dir, "example3.mp4"),
    ]
    sent = False
    for p in paths:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                bot.send_video(uid, f)
            sent = True
    if not sent:
        bot.send_message(uid, "Загрузите 3 файла примеров в папку <code>assets/examples</code> под именами example1.mp4, example2.mp4, example3.mp4", reply_markup=kb_main_menu())

# Техподдержка
@bot.message_handler(func=lambda msg: msg.text == BTN_MENU_SUPPORT)
def on_menu_support(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["support"] = True
    bot.send_message(uid, "Напишите ваше сообщение. Мы свяжемся с вами. (Для выхода нажмите «В главное меню»).", reply_markup=kb_main_menu())

@bot.message_handler(func=lambda msg: msg.text=="🔁 Сбросить выбор сюжетов")
def reset_scenes(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["scenes"] = []
    bot.send_message(uid, "Сюжеты очищены. Выберите заново.", reply_markup=kb_scenes(st.get("format")))

@bot.message_handler(func=lambda msg: msg.text=="✅ Выбрано, дальше")
def after_scenes(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    if not st["scenes"]:
        bot.send_message(uid, "Пока ничего не выбрано. Отметьте хотя бы один сюжет.",
                         reply_markup=kb_scenes(st.get("format")))
        return
    bot.send_message(uid, "Шаг 3/5. Выберите <b>фон</b>.", reply_markup=kb_backgrounds())

@bot.message_handler(func=lambda msg: msg.text in SCENES.keys())
def choose_scene(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # если формат ещё не выбран (на всякий) — просим выбрать формат
    if not st.get("format"):
        bot.send_message(uid, "Сначала выберите формат кадра (Шаг 1/5).", reply_markup=kb_formats())
        return

    allowed = set(available_scene_keys(st["format"]))
    if m.text not in allowed:
        # конкретная ошибка для «лестницы»
        if SCENES.get(m.text, {}).get("kind") == "stairs":
            bot.send_message(uid, "Сюжет «Уходит в небеса» доступен только для формата «🧍 В рост». "
                                  "Поменяйте формат или выберите другой сюжет.",
                             reply_markup=kb_scenes(st["format"]))
        else:
            bot.send_message(uid, "Этот сюжет недоступен для выбранного формата.", reply_markup=kb_scenes(st["format"]))
        return

    if m.text not in st["scenes"]:
        st["scenes"].append(m.text)

    picked = " · ".join(st["scenes"])
    bot.send_message(uid, f"Выбрано: {picked}\nДобавьте ещё или нажмите «✅ Выбрано, дальше».",
                     reply_markup=kb_scenes(st["format"]))

@bot.message_handler(func=lambda msg: msg.text in FORMATS.keys())
def choose_format(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["format"] = m.text
    st["scenes"] = []  # обнуляем выбор сцен под новый формат
    bot.send_message(
        uid,
        "Шаг 2/5. Выберите <b>сюжеты</b> (можно несколько). Когда закончите — нажмите «✅ Выбрано, дальше».",
        reply_markup=kb_scenes(st["format"])
    )

@bot.message_handler(func=lambda msg: msg.text in BACKGROUNDS.keys())
def choose_background(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["bg"] = m.text
    bot.send_message(uid, "Шаг 4/5. Выберите <b>музыку</b> (или «Без музыки»).", reply_markup=kb_music())

@bot.message_handler(func=lambda msg: msg.text in MUSIC.keys() or msg.text=="🔇 Без музыки")
def choose_music(m):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())
    st["music"] = None if m.text=="🔇 Без музыки" else m.text
    # Сколько фото надо?
    if not st["scenes"]:
        bot.send_message(uid, "Ошибка: не выбраны сюжеты. Начните с /start")
        return
    need_people = max(SCENES[k]["people"] for k in st["scenes"])
    bot.send_message(uid, f"Шаг 5/5. Пришлите {need_people} фото (анфас).")

@bot.message_handler(func=lambda msg: msg.text == BTN_GO_HOME)
def go_home(m: telebot.types.Message):
    uid = m.from_user.id
    # Не ломаем текущую очередь задач — просто показываем меню
    show_main_menu(uid)

@bot.message_handler(content_types=["photo"])
def on_photo(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # Проверяем, что прошли шаги до фото
    if not (st["scenes"] and st["format"] and st["bg"] and (st["music"] is not None or st["music"] == None)):
        bot.send_message(uid, "Сначала пройдите шаги: Формат → Сюжет(ы) → Фон → Музыка.")
        return

    need_people = max(SCENES[k]["people"] for k in st["scenes"])

    # 1) Если сцена с 1 человеком и фото уже есть — вежливо игнорируем всё лишнее
    if need_people == 1 and len(st["photos"]) >= 1:
        bot.send_message(uid, "Для выбранного сюжета достаточно одного фото — использую первое.")
        return

    # 2) Скачиваем текущую фотку
    file_id = m.photo[-1].file_id
    saved_path = _download_tg_photo(file_id, uid)

    # --- Мягкая валидация фото ---
    ok_photo, warns = validate_photo(saved_path)
    if warns:
        bot.send_message(uid, "⚠️ Подсказка по фото:\n" + "\n".join(f"• {w}" for w in warns))
    if not ok_photo:
        bot.send_message(uid, "Фото очень низкого качества. Можем продолжить, но результат может быть хуже. "
                              "Если есть другое фото — пришлите ещё одно. Продолжаю с этим фото.")

    # 3) Если это альбом (несколько фото с одним media_group_id)
    if m.media_group_id:
        rec = PENDING_ALBUMS.setdefault(m.media_group_id, {"uid": uid, "need": need_people, "paths": []})
        # Если альбом пришёл к другому сценарию/пользователю — перезапишем под текущего
        rec["uid"] = uid
        rec["need"] = need_people
        rec["paths"].append(saved_path)

        # Когда собрали достаточно для текущего сюжета — переносим в стейт и запускаем пайплайн
        if len(rec["paths"]) >= need_people:
            # Берём ровно столько, сколько требуется
            st["photos"].extend(rec["paths"][:need_people])
            # Чистим буфер альбома
            PENDING_ALBUMS.pop(m.media_group_id, None)

            bot.send_message(uid, "Начинаю генерацию…")
            try:
                run_all_and_send(uid, st)
            except Exception as e:
                print("GEN ERR:", e)
                bot.send_message(uid, f"Что-то пошло не так: {e}")
            finally:
                users[uid] = new_state()
                show_main_menu(uid)
        # Если пока фото не хватает — ничего не говорим (чтобы не было «Осталось 1» на первой картинке альбома)
        return

    # 4) Обычное одиночное сообщение с фото (не альбом)
    st["photos"].append(saved_path)

    if len(st["photos"]) < need_people:
        left = need_people - len(st["photos"])
        bot.send_message(uid, f"Фото получено ✅  Осталось прислать ещё {left}.")
        return

    # Собрали всё — генерим
    bot.send_message(uid, "Начинаю генерацию…")
    try:
        run_all_and_send(uid, st)
    except Exception as e:
        print("GEN ERR:", e)
        bot.send_message(uid, f"Что-то пошло не так: {e}")
    finally:
        users[uid] = new_state()
        show_main_menu(uid)

@bot.message_handler(commands=["cfg"])
def cmd_cfg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    txt = (
        f"<b>Config</b>\n"
        f"ASSISTANT_GATE_ENABLED: {ASSISTANT_GATE_ENABLED}\n"
        f"PREVIEW_START_FRAME: {PREVIEW_START_FRAME}\n"
        f"DEBUG_TO_ADMIN: {DEBUG_TO_ADMIN}\n"
        f"RUNWAY_SEND_JPEG: {RUNWAY_SEND_JPEG}\n"
    )
    bot.reply_to(m, txt)

@bot.message_handler(commands=["gate_on", "gate_off"])
def cmd_gate(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global ASSISTANT_GATE_ENABLED
    ASSISTANT_GATE_ENABLED = (m.text == "/gate_on")
    bot.reply_to(m, f"ASSISTANT_GATE_ENABLED = {ASSISTANT_GATE_ENABLED}")

@bot.message_handler(commands=["preview_on", "preview_off"])
def cmd_preview(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global PREVIEW_START_FRAME
    PREVIEW_START_FRAME = (m.text == "/preview_on")
    bot.reply_to(m, f"PREVIEW_START_FRAME = {PREVIEW_START_FRAME}")

@bot.message_handler(commands=["admdbg_on", "admdbg_off"])
def cmd_admdbg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global DEBUG_TO_ADMIN
    DEBUG_TO_ADMIN = (m.text == "/admdbg_on")
    bot.reply_to(m, f"DEBUG_TO_ADMIN = {DEBUG_TO_ADMIN}")

@bot.message_handler(commands=["jpeg_on", "jpeg_off"])
def cmd_jpeg(m: telebot.types.Message):
    uid = m.from_user.id
    if not _is_admin(uid):
        return bot.reply_to(m, "Недоступно")
    global RUNWAY_SEND_JPEG
    RUNWAY_SEND_JPEG = (m.text == "/jpeg_on")
    bot.reply_to(m, f"RUNWAY_SEND_JPEG = {RUNWAY_SEND_JPEG}")

# ---------- ПАЙПЛАЙН ----------
def run_all_and_send(uid: int, st: dict):
    framing_text = FORMATS[st["format"]]
    bg_prompt    = BG_TEXT[st["bg"]]          # текст-описание для лёгкой анимации
    music_path   = MUSIC.get(st["music"]) if st["music"] else None
    bg_file      = BG_FILES[st["bg"]]         # сам «плейт» (картинка фона)

    out_videos = []
    for scene_key in st["scenes"]:
        scene = SCENES[scene_key]
        # 1) строим ЧЕРНОВОЙ старт-кадр без layout-добавок — только для ассистента
        start_frame_draft = make_start_frame(st["photos"], st["format"], bg_file, layout=None)
        base_prompt = build_prompt(scene["kind"], framing_text, bg_prompt, scene["duration"])

        # 2) прогон через ассистента (мягкая модерация + дополнения к промпту)
        gate = None
        try:
            gate = oai_gate_check(start_frame_draft, base_prompt, {
                "format": st["format"], "scene": scene_key, "background": st["bg"]
            }, timeout_sec=180)
        except Exception as _e:
            print(f"[OAI] gate error: {_e}")

        # 3) политика по новой схеме status/user_notes/backend_fixes
        if gate is None:
            prompt = compact_prompt(base_prompt)
        else:
            gate = _normalize_gate(gate) or {
                "status": "accept", 
                "user_notes": [], 
                "backend_fixes": {
                    "recompose": False, "issues": [], 
                    "target_height_frac": [0.77, 0.77], 
                    "target_centers": [0.35, 0.65], 
                    "gap_frac": 0.05, 
                    "align_feet": {"enabled": True, "floor_y": 1180}
                }, 
                "runway_prompt_additions": ""
            }

            status = gate.get("status", "accept")

            if status == "reject_user_photo":
                # Отправляем замечания пользователю о качестве фото
                user_notes = gate.get("user_notes", [])
                if user_notes:
                    note_messages = {
                        "missing_head": "отсутствует голова",
                        "missing_hands": "отсутствуют руки", 
                        "prohibited_content": "запрещенное содержимое",
                        "low_resolution": "низкое качество фото",
                        "too_dark": "слишком темное фото",
                        "blurred": "размытое фото",
                        "profile_view": "силуэт боком вместо анфас",
                        "sitting_pose": "сидящая поза", 
                        "occluded_face": "закрытое лицо",
                        "cutout_artifacts": "артефакты вырезки"
                    }
                    messages = [note_messages.get(note, note) for note in user_notes[:3]]
                    bot.send_message(uid, f"⚠️ Проблемы с фотографиями: {'; '.join(messages)}. Попробуйте другие фото.")
                else:
                    bot.send_message(uid, "К сожалению, из этих фото сложно собрать корректную сцену.")
                continue

            # Для accept и accept_with_backend_fixes - НЕ отправляем замечания пользователю

            if status == "accept_with_backend_fixes":
                # Применяем backend_fixes для пересборки кадра
                backend_fixes = gate.get("backend_fixes", {})
                if backend_fixes.get("recompose", False):
                    print(f"[Assistant] Backend fixes requested: {backend_fixes.get('issues', [])}")
                    # TODO: Здесь будет логика применения исправлений
                    # Пока оставляем как есть, исправления будут добавлены позже

            # Строим промпт с дополнениями от Assistant'а
            additions = gate.get("runway_prompt_additions", "").strip()
            prompt = compact_prompt(base_prompt + ("; " + additions if additions else ""))

        # 4) строим ЧИСТОВОЙ старт-кадр с учётом layout_feedback (если он есть)
        # Базовый старт-кадр (для проверки ассистентом)
        start_frame = make_start_frame(st["photos"], st["format"], bg_file)
        # Аннотированное превью (если есть)
        try:
            base_id = os.path.splitext(os.path.basename(start_frame))[0].replace("start_", "", 1)
            annot_path  = f"renders/temp/annot_{base_id}.png"
            metrics_json= f"renders/temp/metrics_{base_id}.json"
            if PREVIEW_START_FRAME and os.path.isfile(annot_path):
                cap = "Старт-кадр (аннотированное превью)"
                _send_debug_preview(uid, annot_path, cap)
            if OAI_DEBUG and os.path.isfile(metrics_json):
                with open(metrics_json, "r", encoding="utf-8") as f:
                    print("[DEBUG] metrics json:\n" + f.read()[:2000])
        except Exception as _e:
            print(f"[DEBUG] preview/metrics error: {_e}")
        start_data, start_frame_used = ensure_runway_datauri_under_limit(start_frame)

        # 5) кодирование и проверки размера (с принудительным JPEG при включённом флаге)
        send_path = ensure_jpeg_copy(start_frame) if RUNWAY_SEND_JPEG else start_frame
        try:
            fs = os.path.getsize(send_path)
            print(f"[Runway] start_frame path={send_path} size={fs} bytes (jpeg={RUNWAY_SEND_JPEG})")
        except Exception:
            pass

        start_data = encode_image_datauri(send_path)
        if not start_data or len(start_data) < 64:
            print("[Runway] data URI is empty/too short")
            raise RuntimeError("Start frame data URI is empty")
        if len(start_data) > 5_000_000:
            print(f"[Runway warn] data URI length {len(start_data)} > 5MB; try lower JPEG quality or smaller frame.")

        # 6) отладка
        try:
            os.makedirs("renders/temp", exist_ok=True)
            dbg = {
                "scene": scene_key,
                "format": st["format"],
                "background": st["bg"],
                "start_frame": start_frame_used,
                "start_frame_draft": start_frame_draft,
                "start_frame_final": start_frame,
                "gate": gate,
                "base_prompt": base_prompt,
                "final_prompt": prompt
            }
            dbg["layout_hint_used"] = layout_hint if use_hint else None
            dbg["scene_start_frame"] = scene_frame
            dbg_path = os.path.join("renders/temp", f"runway_dbg_{uid}_{uuid.uuid4().hex}.json")
            with open(dbg_path, "w", encoding="utf-8") as f:
                json.dump(dbg, f, ensure_ascii=False, indent=2)
            print(f"[RUNWAY DBG] saved {dbg_path}\n[RUNWAY DBG] final_prompt_len={len(prompt)}")
        except Exception as _e:
            print(f"[RUNWAY DBG] save error: {_e}")

        base_prompt = build_prompt(scene["kind"], framing_text, bg_prompt, scene["duration"])

        # --- вызов ассистента (если используешь проверки) ---
        gate = None
        try:
            gate = oai_gate_check(base_start_frame, base_prompt, {
                "format": st["format"], "scene": scene_key, "background": st["bg"]
            }, timeout_sec=180)  # можно 180, чтобы меньше таймаутов
        except Exception as _e:
            print(f"[OAI] gate error: {_e}")

        # политика по verdict (ok/warn/block) с авто-понижением "block → warn" для мелочей
        if gate is None:
            prompt = compact_prompt(base_prompt)
        else:
            verdict = gate.get("verdict") or ("ok" if gate.get("ok", True) else "block")
            reasons = gate.get("reasons") or []
            additions = gate.get("prompt_additions") or ""
            # если блок только из-за «мелочей» — понижаем до warn
            if verdict == "block" and _is_minor_only(reasons):
                verdict = "warn"

            if verdict == "block":
                user_msg = gate.get("user_msg") or "К сожалению, из этих фото сложно собрать корректную сцену."
                bot.send_message(uid, user_msg)
                # логируем для диагностики
                _log_fail(uid, "assistant_block", {"scene": scene_key, "reasons": reasons}, gate)
                continue

            if verdict == "warn":
                if reasons:
                    bot.send_message(uid, "⚠️ Замечание: " + "; ".join(reasons[:3]))

            prompt = compact_prompt(base_prompt + ("; " + additions if additions else ""))

        # сохранить всё в debug-файл
        try:
            os.makedirs("renders/temp", exist_ok=True)
            dbg = {
                "scene": scene_key,
                "format": st["format"],
                "background": st["bg"],
                "base_start_frame": base_start_frame,
                "gate": gate,
                "base_prompt": base_prompt,
                "final_prompt": prompt
            }
            dbg_path = os.path.join("renders/temp", f"runway_dbg_{uid}_{uuid.uuid4().hex}.json")
            with open(dbg_path, "w", encoding="utf-8") as f:
                json.dump(dbg, f, ensure_ascii=False, indent=2)
            print(f"[RUNWAY DBG] saved {dbg_path}\n[RUNWAY DBG] final_prompt_len={len(prompt)}")
        except Exception as _e:
            print(f"[RUNWAY DBG] save error: {_e}")

        try:
            # Собираем сценный старт-кадр с возможным учётом layout_feedback
            layout_hint = (gate or {}).get("layout_feedback") if gate else None
            use_hint = bool(layout_hint and any(int(layout_hint.get(k, 0)) != 0 
                       for k in ("shift_left_px", "shift_right_px", "scale_left_pct", "scale_right_pct")))

            scene_frame = make_start_frame(st["photos"], st["format"], bg_file, layout_hint if use_hint else None)

            # Кодирование (JPEG опционально) — используем настройки из Ч.10
            send_path = ensure_jpeg_copy(scene_frame) if RUNWAY_SEND_JPEG else scene_frame
            # Отправляем превью кадра и промпта (перед кодированием в data:URI)
            try:
                _send_debug_preview(uid, scene_key, send_path, prompt, gate)
            except Exception as _e:
                print(f"[DBG] preview send err: {_e}")
            try:
                fs = os.path.getsize(send_path)
                print(f"[Runway] scene_frame path={send_path} size={fs} bytes (jpeg={RUNWAY_SEND_JPEG})")
            except Exception:
                pass

            scene_data = encode_image_datauri(send_path)
            if not scene_data or len(scene_data) < 64:
                raise RuntimeError("Scene start frame data URI is empty")
            if len(scene_data) > 5_000_000:
                print(f"[Runway warn] scene data URI length {len(scene_data)} > 5MB; try lower JPEG quality.")
            additions = additions if 'additions' in locals() else ""
            # основной запуск + фолбэк на 5с при 4xx от Runway
            try:
                start_resp = runway_start(scene_data, prompt, scene["duration"])
            except RuntimeError as e:
                msg = str(e)
                if "400/4xx" in msg and int(scene["duration"]) > 5:
                    bot.send_message(uid, f"⚠️ Runway отказал в {scene['duration']} с для «{scene_key}». Пробую 5 с.")
                    base_prompt_short = build_prompt(scene["kind"], framing_text, bg_prompt, 5)
                    prompt_short = compact_prompt(base_prompt_short + ("; " + additions if additions else ""))
                    try:
                        start_resp = runway_start(scene_data, prompt_short, 5)
                        # чтобы в дебаге и логах видеть фактический промпт/длительность
                        prompt = prompt_short
                        scene["duration"] = 5
                    except Exception as e2:
                        bot.send_message(uid, f"Сцена «{scene_key}» упала повторно: {e2}")
                        _log_fail(uid, "runway_4xx_fallback_failed",
                                  {"scene": scene_key, "prompt_len": len(prompt)}, str(e2))
                        continue
                else:
                    bot.send_message(uid, f"Сцена «{scene_key}» упала с ошибкой: {e}")
                    _log_fail(uid, "runway_start_failed",
                              {"scene": scene_key, "prompt_len": len(prompt)}, str(e))
                    continue
            task_id = start_resp.get("id") or start_resp.get("task", {}).get("id")
            if not task_id:
                bot.send_message(uid, f"Не получил id задачи от Runway для «{scene_key}». Пропускаю.")
                _log_fail(uid, "no_task_id", {"scene": scene_key, "prompt_len": len(prompt)}, start_resp)
                continue

            poll = runway_poll(task_id)
            if poll.get("status") != "SUCCEEDED":
                msg = (f"Сцена «{scene_key}» не удалась: {poll.get('status')}. "
                       f"Попробуйте другой фон или формат «🧍 В рост», и фото, где человек(и) видны целиком.")
                bot.send_message(uid, msg)
                _log_fail(uid, "poll_failed", {"scene": scene_key, "prompt_len": len(prompt)}, poll)
                continue

            out = poll.get("output") or []
            url = out[0] if isinstance(out[0], str) else (out[0].get("url") if out else None)
            if not url:
                bot.send_message(uid, f"Runway не вернул ссылку для «{scene_key}».")
                _log_fail(uid, "no_url", {"scene": scene_key}, poll)
                continue

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            save_as = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}.mp4"
            download(url, save_as)
            out_videos.append(save_as)

        except Exception as e:
            bot.send_message(uid, f"Сцена «{scene_key}» упала с ошибкой: {e}")
            continue

    if not out_videos:
        bot.send_message(uid, "Ни одна сцена не сгенерировалась. Попробуйте другие фото.")
        users[uid] = new_state()
        show_main_menu(uid)
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    final_path = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}_FINAL.mp4"
    title_text = "Memory Forever — Память навсегда с вами"
    # Постобработка через ffmpeg: склейка + водяной знак + музыка + титр
    try:
        postprocess_concat_ffmpeg(out_videos, music_path, title_text, final_path, bg_overlay_file=bg_file)
    except Exception as e:
        print(f"Postprocess error: {e}")
        bot.send_message(uid, f"Постобработка не удалась ({e}). Шлю сырые сцены по отдельности.")
        for i, p in enumerate(out_videos, 1):
            with open(p,"rb") as f: 
                bot.send_video(uid, f, caption=f"Сцена {i}")

        cleanup_artifacts(keep_last=10)
        # Сброс состояния и показ главного меню
        users[uid] = new_state()
        show_main_menu(uid, "Готово! Видео отправлены (постобработка не удалась).")
        return

    with open(final_path,"rb") as f:
        cap = " · ".join(st["scenes"]) + f" · {st['format']}"
        bot.send_video(uid, f, caption=cap)

    cleanup_artifacts(keep_last=10)
    # Сброс состояния и показ главного меню
    users[uid] = new_state()
    show_main_menu(uid, "Готово! Видео создано успешно.")

# ---------- ОБРАБОТЧИКИ CALLBACK-КНОПОК МУЗЫКИ ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("listen_"))
def on_music_listen(call):
    """Обработчик прослушивания музыки"""
    uid = call.from_user.id
    music_name = call.data.replace("listen_", "")
    music_path = find_music_by_name(music_name)

    if music_path and os.path.isfile(music_path):
        try:
            with open(music_path, 'rb') as audio:
                bot.send_audio(uid, audio, title=music_name, performer="Memory Forever")
            bot.answer_callback_query(call.id, f"🎧 Воспроизводится: {music_name}")
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка при отправке аудио: {e}")
    else:
        bot.answer_callback_query(call.id, "Файл не найден")

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_music_"))
def on_music_select(call):
    """Обработчик выбора музыки"""
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())

    music_choice = call.data.replace("select_music_", "")

    if music_choice == "none":
        st["music"] = None
        bot.answer_callback_query(call.id, "🔇 Выбрано: Без музыки")
    else:
        # Находим полное имя музыки с эмодзи
        full_name = None
        for key in MUSIC.keys():
            if key.replace("🎵 ", "") == music_choice:
                full_name = key
                break

        if full_name:
            st["music"] = full_name
            bot.answer_callback_query(call.id, f"✅ Выбрано: {music_choice}")
        else:
            bot.answer_callback_query(call.id, "Музыка не найдена")
            return

    # Переход к следующему шагу
    if not st["scenes"]:
        bot.send_message(uid, "Ошибка: не выбраны сюжеты. Начните с /start")
        return

    need_people = max(SCENES[k]["people"] for k in st["scenes"])
    bot.send_message(uid, f"Шаг 5/5. Пришлите {need_people} фото (анфас).")

@bot.callback_query_handler(func=lambda call: call.data == "go_home")
def on_go_home_callback(call):
    """Обработчик кнопки 'В главное меню' из inline-клавиатуры"""
    uid = call.from_user.id
    bot.answer_callback_query(call.id, "🏠 Переход в главное меню")
    show_main_menu(uid)

@bot.message_handler(func=lambda msg: True, content_types=["text"])
def fallback_text(m: telebot.types.Message):
    uid = m.from_user.id
    st = users.setdefault(uid, new_state())

    # Если ждём сообщение для поддержки — пересылаем админу и выходим в меню
    if st.get("support"):
        if ADMIN_CHAT_ID:
            # сначала пробуем форвард
            ok = True
            try:
                bot.forward_message(int(ADMIN_CHAT_ID), uid, m.message_id)
            except Exception:
                ok = False
            # если не получилось форвардом — отправим как текст
            if not ok:
                uname = (m.from_user.username or "")
                header = f"Сообщение в поддержку от @{uname} (id {uid}):"
                bot.send_message(int(ADMIN_CHAT_ID), f"{header}\n\n{m.text}")
        else:
            bot.send_message(uid, "Адрес поддержки не настроен. Укажите ADMIN_CHAT_ID в Secrets.")

        st["support"] = False
        show_main_menu(uid, "Спасибо! Сообщение передано. Мы свяжемся с вами.")
        return

    # Иначе — вежливый намёк, что надо пользоваться кнопками
    # (ничего не ломаем, просто показываем меню)
    show_main_menu(uid, "Пожалуйста, используйте кнопки ниже.")

# ---------- RUN ----------
if __name__ == "__main__":
    print("Memory Forever v0.3 started.")
    bot.infinity_polling(skip_pending=True, timeout=60) 
