# bot.py — Memory Forever v0.4
# Шаги: Сюжет(ы) → Формат → Фон → Музыка → Фото(1/2) → Runway → постобработка (wm+audio+титр) → отправка
import os, io, time, uuid, base64, requests, subprocess, shutil, json
from datetime import datetime
from typing import List
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from PIL import ImageFilter

# rembg: где лежат модели и сессии вырезки
os.environ.setdefault("U2NET_HOME", os.path.join(os.getcwd(), "models"))
from rembg import remove, new_session
RMBG_SESSION = new_session("u2net")
# Дополнительные модели для портретов (если используешь внутри smart_cutout)
RMBG_HUMAN = new_session("u2net_human_seg")
RMBG_ISNET  = new_session("isnet-general-use")

import telebot

# ---------- КЛЮЧИ ----------
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RUNWAY_KEY = os.environ.get("RUNWAY_API_KEY", "")
if not TG_TOKEN or not RUNWAY_KEY:
    print("⚠️ Задай TELEGRAM_BOT_TOKEN и RUNWAY_API_KEY в Secrets.")
bot = telebot.TeleBot(TG_TOKEN, parse_mode="HTML")

# ---------- РЕЖИМЫ/ОТЛАДКА (без OpenAI Assistants) ----------
# Этот флаг оставим как общий «расширенный лог», он НЕ связан больше с OpenAI.
OAI_DEBUG = os.environ.get("OAI_DEBUG", "0") == "1"   # просто флаг подробного лога
# Визуальное превью старт-кадра и промпта (перед Runway)
PREVIEW_START_FRAME = os.environ.get("PREVIEW_START_FRAME", "0") == "1"  # 1 — отправлять пользователю
DEBUG_TO_ADMIN      = os.environ.get("DEBUG_TO_ADMIN", "1") == "1"       # 1 — слать превью админу (если ADMIN_CHAT_ID задан)
RUNWAY_SEND_JPEG    = os.environ.get("RUNWAY_SEND_JPEG", "1") == "1"     # конвертировать старт-кадр в JPEG перед отправкой
START_OVERLAY_DEBUG = os.environ.get("START_OVERLAY_DEBUG", "0") == "1"  # рисовать диагностические рамки на старте
MF_DEBUG            = OAI_DEBUG or (os.environ.get("MF_DEBUG", "0") == "1")

# Полностью отключаем любые «ворота/проверки» ассистента (и ниже не используем их нигде)
ASSISTANT_GATE_ENABLED = False  # жёстко OFF
# --- Отладка/превью (Assistant OpenAI удалён) ---

def _safe_send_photo(chat_id: int, path: str, caption: str = ""):
    try:
        with open(path, "rb") as ph:
            bot.send_photo(chat_id, ph, caption=caption[:1024])
    except Exception as e:
        print(f"[DBG] send_photo error: {e}")

def _send_debug_preview(uid: int, scene_key: str, start_path: str, prompt: str, gate: dict | None = None):
    """
    Превью старт-кадра и текста промпта.
    Параметр gate оставлен для совместимости с существующими вызовами,
    но игнорируется (ассистент выключен).
    """
    cap = (
        f"🎯 PREVIEW → {scene_key}\n"
        f"prompt[{len(prompt)}]: {prompt[:500]}{'…' if len(prompt) > 500 else ''}\n"
        f"gate: disabled"
    )
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

# --- Админ для техподдержки (ID чата/пользователя/группы) ---
# Пример: "123456789" для юзера, "-1001234567890" для супергруппы.
_raw_admin = os.environ.get("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_raw_admin) if _raw_admin.lstrip("-").isdigit() else None  # None, если не задано корректно

# --- Тексты кнопок главного меню ---
BTN_MENU_MAIN    = "📋 Главное меню"
BTN_MENU_START   = "🎬 Сделать видео"
BTN_MENU_PRICE   = "💲 Стоимость"
BTN_MENU_SUPPORT = "🛟 Техподдержка"
BTN_MENU_GUIDE   = "📘 Инструкция по созданию видео"
BTN_MENU_DEMO    = "🎞 Пример работ"

# Кнопка «домой» для всех шагов мастера
BTN_GO_HOME = "🏠 В главное меню"

def kb_main_menu() -> telebot.types.ReplyKeyboardMarkup:
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2, selective=True)
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_MAIN),
        telebot.types.KeyboardButton(BTN_MENU_START),
    )
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_PRICE),
        telebot.types.KeyboardButton(BTN_MENU_SUPPORT),
    )
    kb.add(
        telebot.types.KeyboardButton(BTN_MENU_GUIDE),
        telebot.types.KeyboardButton(BTN_MENU_DEMO),
    )
    return kb

def show_main_menu(uid: int, text: str | None = None) -> None:
    """Показывает главное меню пользователю."""
    text = text or "Выберите пункт меню или перейдите к созданию видео, нажав «Сделать видео»."
    try:
        bot.send_message(uid, text, reply_markup=kb_main_menu())
    except Exception as e:
        # не падаем из-за телеграм-ошибок (например, пользователь отключил бота)
        print(f"[WARN] show_main_menu({uid}) failed: {e}")

# ---------- ПАПКИ ----------
os.makedirs("uploads",  exist_ok=True)
os.makedirs("renders",  exist_ok=True)
os.makedirs("assets",   exist_ok=True)
os.makedirs("audio",    exist_ok=True)
WATERMARK_PATH = "assets/watermark_black.jpg"

# ---------- СЦЕНЫ / ФОРМАТЫ / ФОНЫ / МУЗЫКА ----------
SCENES = {
    "🫂 Объятия 5с - БЕСПЛАТНО":      {"duration": 5,  "kind": "hug",         "people": 2},
    "🫂 Объятия 10с - 100 рублей":    {"duration": 10, "kind": "hug",         "people": 2},
    "💏 Поцелуй 10с - 100 рублей":    {"duration": 10, "kind": "kiss_cheek",  "people": 2},
    "👋 Прощание 10с - 100 рублей":   {"duration": 10, "kind": "wave",        "people": 1},
    "🪜 Уходит в небеса 10с - 100 рублей": {"duration": 10, "kind": "stairs", "people": 2},
}

FORMATS = {
    "🧍 В рост":   "full-body shot",
    "👨‍💼 По пояс": "waist-up shot",
    "👨‍💼 По грудь": "chest-up shot",
}

# Единый источник истины: фон → путь к картинке
BG_FILES = {
    "☁️ Лестница среди облаков": "assets/backgrounds/bg_stairs.jpg",
    "🔆 Врата света":            "assets/backgrounds/bg_gates.jpg",
    "🪽 Ангелы и крылья":        "assets/backgrounds/bg_angels.jpg",
}

# Для совместимости со старым кодом используем то же имя (кнопки смотрят на ключи BACKGROUNDS)
BACKGROUNDS = BG_FILES  # алиас: те же ключи и те же пути

MUSIC = {
    "🎵 Спокойная": "audio/soft_pad.mp3",
    "🎵 Церковная": "audio/gentle_arpeggio.mp3",
    "🎵 Лиричная":  "audio/strings_hymn.mp3",
}

# Короткие подсказки для генератора (используются в промпте)
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

# Зазоры и центры
MIN_GAP_PX       = 24     # было 20 — чуть безопаснее от «слипания»
IDEAL_GAP_FRAC   = 0.07   # было 0.05 — целевой зазор ~7% ширины
CENTER_BIAS_FRAC = 0.40   # было 0.42 — в старой раскладке уводит людей чуть к краям

# Максимальный допустимый апскейл
MAX_UPSCALE = float(os.environ.get("MAX_UPSCALE", "1.45"))

# Минимальные «видимые» высоты (анти-карлик), доля от высоты кадра H
MIN_VISIBLE_FRAC = {
    ("🧍 В рост", 1): 0.66,  # было 0.70
    ("🧍 В рост", 2): 0.64,  # было 0.70
    ("👨‍💼 По пояс", 1): 0.56,  # было 0.60
    ("👨‍💼 По пояс", 2): 0.54,  # было 0.60
    ("👨‍💼 По грудь", 1): 0.48,  # было 0.50
    ("👨‍💼 По грудь", 2): 0.46,  # было 0.50
}
def _min_frac_for(format_key: str, count_people: int) -> float:
    return MIN_VISIBLE_FRAC.get((format_key, count_people), 0.56)

# Целевые стартовые высоты (ещё чуть меньше, чем раньше)
TH_FULL_SINGLE   = 0.66   # было 0.70
TH_FULL_DOUBLE   = 0.64   # было 0.70
TH_WAIST_SINGLE  = 0.56   # было 0.60
TH_WAIST_DOUBLE  = 0.54   # было 0.60
TH_CHEST_SINGLE  = 0.48   # было 0.50
TH_CHEST_DOUBLE  = 0.46   # было 0.50

# Минимальная доля высоты группы (для «подростить», если совсем мелко)
MIN_SINGLE_FRAC = {
    "В рост":  0.66,
    "По пояс": 0.56,
    "По грудь":0.48,
}
MIN_PAIR_FRAC = {
    "В рост":  0.64,
    "По пояс": 0.54,
    "По грудь":0.46,
}

# Мягкий предел апскейла при доводке (чтобы внезапно не «раздуть»)
PAIR_UPSCALE_CAP   = 1.10   # было 1.22
SINGLE_UPSCALE_CAP = 1.12   # было 1.25

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

# ------------------------- PROMPT BUILDER (per scene) -------------------------

def _people_count_by_kind(kind: str) -> int:
    """Если не передали явно людей — выводим по типу сцены."""
    k = (kind or "").lower()
    if k in ("wave",):
        return 1
    # hug, kiss_cheek, stairs и проч. — обычно пара
    return 2

SCENE_TEMPLATES = {
    # два человека, мягкое сближение и объятие
    "hug": (
        "{who} are already close to each other; gently approach and embrace; "
        "keep relative sizes constant; natural breathing; subtle micro-motions; {framing}. "
        "{bg_rules}"
    ),
    # «поцелуй» без риска — щека/лоб
    "kiss_cheek": (
        "{who} are close; tender cheek-to-cheek moment or a soft forehead kiss; "
        "no lip contact; gentle leaning; hold briefly; {framing}. "
        "{bg_rules}"
    ),
    # один человек — прощание
    "wave": (
        "{who} stands in place and slowly waves a hand; small natural body sway; "
        "calm mood; {framing}. {bg_rules}"
    ),
    # «уходит в небеса» — мягкое, без прыжков камеры
    "stairs": (
        "{who} gently ascend the stairs together; small synchronized steps; "
        "no rushing; keep sizes consistent; {framing}. {bg_rules}"
    ),
}

def build_prompt(kind: str, framing_text: str, bg_text: str, duration_s: int, people: int | None = None) -> str:
    """
    Строит промпт без ассистента:
    - базовый текст по сцене (SCENE_TEMPLATES)
    - формат кадра (framing_text)
    - правила фона (bg_text)
    - страхующие добавки: BACKUP_PROMPT_ADDITIONS + NEG_TAIL
    """
    k = (kind or "").lower()
    tpl = SCENE_TEMPLATES.get(k, "{who} are present; natural small motions; {framing}. {bg_rules}")
    n = people if (people and people > 0) else _people_count_by_kind(k)
    who = "two people" if n >= 2 else "one person"

    # Базовые кусочки
    core = tpl.format(
        who=who,
        framing=framing_text.strip(),
        bg_rules=(bg_text or "").strip()
    )

    # Небольшая пометка про длительность (без жёсткого таймлайна, чтобы не усложнять пока)
    dur_hint = f" overall duration about {int(duration_s)} seconds;"

    # Финальная сборка
    prompt = (
        core.strip() + dur_hint + " " +
        BACKUP_PROMPT_ADDITIONS.strip() + " " +
        NEG_TAIL.strip()
    )
    return " ".join(prompt.split())

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

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
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

# --- Заглушка под старые вызовы ассистента (удалим позже вместе с ними) ---
def _is_minor_only(reasons: list[str] | None) -> bool:
    """Ассистент отключён: минор/мажор причины не анализируем."""
    return False

def validate_photo(path: str) -> tuple[bool, list[str]]:
    """
    Мягкая валидация фото.
    Возвращает (ok, warnings). ok=False — очень маленькое фото, но пайплайн не блокируем.
    """
    warns = []
    ok = True
    try:
        im = Image.open(path)
        # Нормализуем ориентацию по EXIF (если телефон переворачивал)
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
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

    # 3) Темнота/экспозиция (очень грубо)
    gray = im.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    mean = float(arr.mean())
    if mean < 55:
        warns.append("фото тёмное — попробуйте более светлое/контрастное")

    # 4) Размытость (приблизительно через «края»)
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
    Вырезка человека:
      1) пробуем портретную модель, иначе базовую;
      2) если силуэт слишком мал — пробуем ISNet;
      3) убираем «ореол» и чуть смягчаем край.
    """
    def _run(session):
        out = remove(img_rgba, session=session, post_process_mask=True)
        if isinstance(out, (bytes, bytearray)):
            out = Image.open(io.BytesIO(out)).convert("RGBA")
        else:
            out = out.convert("RGBA")
        return out

    # 1) Портретная модель → fallback
    try:
        cut = _run(RMBG_HUMAN)
    except Exception:
        cut = _run(RMBG_SESSION)

    # 2) Если силуэт подозрительно маленький — пробуем ISNet
    try:
        bb = cut.getbbox() or (0, 0, cut.width, cut.height)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if area < 0.12 * cut.width * cut.height:
            try:
                alt = _run(RMBG_ISNET)
                bb2 = alt.getbbox() or (0, 0, alt.width, alt.height)
                area2 = (bb2[2] - bb2[0]) * (bb2[3] - bb2[1])
                if area2 > area:
                    cut = alt
            except Exception:
                pass
    except Exception:
        pass

    # 3) Рафинирование маски: чуть «поджать» и дать перо
    a = cut.split()[-1]
    a = a.filter(ImageFilter.MinFilter(3))       # ~1px эрозия — убираем ореол
    a = a.filter(ImageFilter.GaussianBlur(1.2))  # мягкое перо ~1–2px
    cut.putalpha(a)
    return cut
    
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
        _pl = ""
        try:
            _pl = (payload.get("promptText") or payload.get("prompt") or "") if isinstance(payload, dict) else ""
        except Exception:
            pass

        model = payload.get("model")
        ratio = payload.get("ratio") or payload.get("aspect_ratio")
        dur   = payload.get("duration")

        msg = f"[Runway] model={model} dur={dur} ratio={ratio}"
        if _pl:
            msg += f" prompt[:200]={_pl[:200].replace(chr(10),' ')}..."
        print(msg)

        if MF_DEBUG:
            try:
                os.makedirs("renders/temp", exist_ok=True)
                preview = {
                    "model": model,
                    "duration": dur,
                    "ratio": ratio,
                    "prompt_len": len(_pl),
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

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    """
    Ассистент отключён: ничего не проверяем и ничего не добавляем.
    Возвращаем None, чтобы остальной код шёл по «без ассистента» ветке.
    """
    return None

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
    """
    Формирует стартовый кадр. Ветку для 2х людей упростили (LEAN v0):
    - одинаковая видимая высота силуэтов (~70% H, но не больше MAX_VISIBLE_FRAC);
    - жёсткий внутренний зазор >= 5% ширины;
    - без автоподтяжек/ростов; фиксированная, предсказуемая геометрия.
    """

    def _min_target_for(framing: str, people_count: int) -> float:
        if "В рост" in framing:
            return 0.82 if people_count >= 2 else 0.90
        elif "По пояс" in framing:
            return 0.66 if people_count >= 2 else 0.72
        else:  # По грудь
            return 0.58 if people_count >= 2 else 0.62

    W, H = 720, 1280
    base_id = uuid.uuid4().hex
    floor_margin = 10

    # верхний «воздух»
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

    # 2) вырезаем людей
    cuts = []
    for p in photo_paths:
        im = Image.open(p).convert("RGBA")
        try:
            cut_rgba = smart_cutout(im)
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

    # 3) целевая высота относительно кадра (используется в одиночной ветке)
    two = (len(photo_paths) > 1)
    if "В рост" in framing_key:
        target_h = TH_FULL_DOUBLE if two else TH_FULL_SINGLE
    elif "По пояс" in framing_key:
        target_h = TH_WAIST_DOUBLE if two else TH_WAIST_SINGLE
    else:  # «По грудь»
        target_h = TH_CHEST_DOUBLE if two else TH_CHEST_SINGLE

    # минимум (анти-карлик)
    target_h_min = _min_target_for(framing_key, len(photo_paths))
    if target_h < target_h_min:
        target_h = target_h_min

    def scale_to_target_effective(img: Image.Image, target: float) -> Image.Image:
        bbox, yb = alpha_metrics(img)
        eff_h = max(1, (yb - bbox[1] + 1))
        scale = (H * target) / eff_h
        if scale > MAX_UPSCALE:
            scale = MAX_UPSCALE
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        return img.resize((nw, nh), RESAMPLE.LANCZOS)

    def place_y_for_floor(img: Image.Image) -> int:
        bbox, yb = alpha_metrics(img)
        eff_h = (yb - bbox[1] + 1)
        y_top_content = H - floor_margin - eff_h
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
        for r in rects:
            g.rectangle(r, outline=(255, 0, 0, 200), width=3)
        m = 20
        g.rectangle((m, m, base.width - m, base.height - m), outline=(0, 255, 0, 180), width=2)
        base.alpha_composite(ov)

    # ------------------------------- 1 человек -------------------------------
    if len(cuts) == 1:
        P = scale_to_target_effective(cuts[0], target_h)
        x = (W - P.width) // 2
        y = place_y_for_floor(P)

        # оценка видимой высоты
        def rect_at_single(px, py, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (px + bx, py + by, px + bx1, py + by1)

        r = rect_at_single(x, y, P)
        group_h = r[3] - r[1]
        fmt = "В рост" if "В рост" in framing_key else ("По пояс" if "По пояс" in framing_key else "По грудь")
        min_h_frac = MIN_SINGLE_FRAC[fmt]

        if group_h < int(min_h_frac * H):
            need = (min_h_frac * H) / max(1, group_h)
            cap = SINGLE_UPSCALE_CAP
            new_target = min(target_h * need, target_h * cap)
            if new_target > target_h:
                P = scale_to_target_effective(cuts[0], new_target)
                x = (W - P.width) // 2
                y = place_y_for_floor(P)

        margin = 20
        x = max(margin, min(W - P.width - margin, x))
        top_margin = max(margin, int(HEADROOM_FRAC * H))
        y = max(top_margin, min(H - P.height - margin, y))

        # мягкий ручной layout для 1 человека (если вдруг прилетит)
        if layout and isinstance(layout, dict):
            scl = int(layout.get("scale_left_pct", 0) or 0)
            dxl = int(layout.get("shift_left_px", 0) or 0)
            if scl != 0:
                k = 1.0 + max(-0.20, min(0.20, scl / 100.0))
                nw, nh = max(1, int(P.width * k)), max(1, int(P.height * k))
                P = P.resize((nw, nh), RESAMPLE.LANCZOS)
                y = place_y_for_floor(P)
            if dxl != 0:
                x += int(-dxl)
            x = max(margin, min(W - P.width - margin, x))
            y = max(margin, min(H - P.height - margin, y))

        # анти-карлик для одиночки
        def _visible_frac(img: Image.Image) -> float:
            bb, yb = alpha_metrics(img)
            eff_h = max(1, (yb - bb[1] + 1))
            return eff_h / H

        grow_tries = 0
        while _visible_frac(P) < _min_target_for(framing_key, 1) and grow_tries < 12:
            new_target = min(target_h * 1.04, 0.98)
            newP = scale_to_target_effective(cuts[0], new_target)
            cx = x + P.width // 2
            cy_floor = place_y_for_floor(newP)
            newx = cx - newP.width // 2
            margin = 20
            newx = max(margin, min(W - newP.width - margin, newx))
            newy = max(margin, min(H - newP.height - margin, cy_floor))
            if newy <= margin or newx <= margin or (newx + newP.width) >= (W - margin):
                break
            P, x, y = newP, newx, newy
            target_h = new_target
            grow_tries += 1

        draw_with_shadow(canvas, P, x, y)
        try:
            _draw_debug_boxes(canvas, [_rect_at(x, y, P)])
        except Exception:
            pass

    # ------------------------------ 2 человека (LEAN v1) ------------------------------
    else:
        L = cuts[0]
        R = cuts[1]

        # Метрики и масштаб по видимой высоте (bbox по альфе)
        def _vis_h(img: Image.Image) -> int:
            bb, yb = alpha_metrics(img)
            return max(1, (yb - bb[1] + 1))

        def _vis_frac(img: Image.Image) -> float:
            return _vis_h(img) / H

        def _scale_to_vis_frac(img: Image.Image, target_frac: float) -> Image.Image:
            cur = _vis_frac(img)
            if cur <= 1e-6:
                return img
            k = max(0.4, min(MAX_UPSCALE, target_frac / cur))
            nw = max(1, int(img.width * k))
            nh = max(1, int(img.height * k))
            return img.resize((nw, nh), RESAMPLE.LANCZOS)

        def rect_at(x, y, img):
            bx, by, bx1, by1 = alpha_metrics(img)[0]
            return (x + bx, y + by, x + bx1, y + by1)

        def _inner_gap_px(a, b):
            return max(0, b[0] - a[2])

        MARGIN = 20
        is_full = ("В рост" in framing_key) or ("в рост" in framing_key)

        # Жёсткие пределы видимой высоты
        MAX_VISIBLE_FRAC = LEAN_MAX_VISIBLE_FRAC if is_full else max(LEAN_MAX_VISIBLE_FRAC, 0.76)
        TARGET_VISIBLE_FRAC = min(LEAN_TARGET_VISIBLE_FRAC, MAX_VISIBLE_FRAC)

        # 1) Нормализуем видимую высоту обоих под один таргет, но не превышаем MAX
        L = _scale_to_vis_frac(L, TARGET_VISIBLE_FRAC)
        R = _scale_to_vis_frac(R, TARGET_VISIBLE_FRAC)
        if _vis_frac(L) > MAX_VISIBLE_FRAC:
            L = _scale_to_vis_frac(L, MAX_VISIBLE_FRAC)
        if _vis_frac(R) > MAX_VISIBLE_FRAC:
            R = _scale_to_vis_frac(R, MAX_VISIBLE_FRAC)

        # 2) «Поставить на пол»
        yl = place_y_for_floor(L)
        yr = place_y_for_floor(R)

        # 3) Базовые центры по X (слева/справа)
        cxL = int(W * LEAN_CX_LEFT)
        cxR = int(W * LEAN_CX_RIGHT)
        lx  = cxL - L.width // 2
        rx  = cxR - R.width // 2

        # 4) (опционально) мягкий ручной layout, если вдруг передан layout
        if layout and isinstance(layout, dict):
            scl_l = int(layout.get("scale_left_pct", 0)  or 0)
            scl_r = int(layout.get("scale_right_pct", 0) or 0)
            dx_l  = int(layout.get("shift_left_px", 0)   or 0)
            dx_r  = int(layout.get("shift_right_px", 0)  or 0)

            def _apply_scale_soft(img: Image.Image, pct: int) -> Image.Image:
                if pct == 0:
                    return img
                k = 1.0 + max(-0.15, min(0.15, pct / 100.0))  # мягче: ±15%
                nw, nh = max(1, int(img.width * k)), max(1, int(img.height * k))
                return img.resize((nw, nh), RESAMPLE.LANCZOS)

            if scl_l:
                L = _apply_scale_soft(L, scl_l)
                yl = place_y_for_floor(L)
                lx = cxL - L.width // 2
            if scl_r:
                R = _apply_scale_soft(R, scl_r)
                yr = place_y_for_floor(R)
                rx = cxR - R.width // 2
            if dx_l:
                # shift_left_px > 0 — «влево», т.е. x -= dx
                lx += int(-dx_l)
            if dx_r:
                # shift_right_px > 0 — «вправо», т.е. x += dx
                rx += int(dx_r)

        # 5) Гарантируем жёсткий внутренний зазор (>= max(MIN_GAP_PX, LEAN_MIN_GAP_FRAC * W))
        ra = rect_at(lx, yl, L)
        rb = rect_at(rx, yr, R)
        min_gap = max(MIN_GAP_PX, int(LEAN_MIN_GAP_FRAC * W))
        tries = 0
        while (_inner_gap_px(ra, rb) < min_gap) and tries < 80:
            center = W // 2
            # разводим от центра, но не выходим за поля
            lx = max(MARGIN, lx - 2) if (lx + L.width // 2) <= center else min(W - L.width - MARGIN, lx + 2)
            rx = min(W - R.width - MARGIN, rx + 2) if (rx + R.width // 2) >= center else max(MARGIN, rx - 2)
            ra = rect_at(lx, yl, L)
            rb = rect_at(rx, yr, R)
            tries += 1

        # 6) Держим пару внутри «полосы» композиции фона (чтобы не «съедали» важную геометрию)
        p = _bg_layout_presets(bg_file)
        band_left  = int(W * (p["center_frac"] - p["band_frac"] / 2.0))
        band_right = int(W * (p["center_frac"] + p["band_frac"] / 2.0))

        # Если любая из рамок выходит за полосу — смещаем пару целиком, сохраняя зазор
        ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
        shift = 0
        if ra[0] < band_left:
            shift = max(shift, band_left - ra[0])
        if rb[2] > band_right:
            shift = min(shift, band_right - rb[2])  # отрицательное смещение, если надо сдвинуть влево
        lx = max(MARGIN, min(W - L.width - MARGIN, lx + shift))
        rx = max(MARGIN, min(W - R.width - MARGIN, rx + shift))

        # 7) Страховка «headroom» (если подпираем верх — один раз чуть уменьшаем обе)
        ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
        def _headroom_ok(r): return r[1] > int(HEADROOM_FRAC * H)
        if not _headroom_ok(ra) or not _headroom_ok(rb):
            L = _scale_to_vis_frac(L, min(_vis_frac(L) * 0.96, MAX_VISIBLE_FRAC))
            R = _scale_to_vis_frac(R, min(_vis_frac(R) * 0.96, MAX_VISIBLE_FRAC))
            yl = place_y_for_floor(L); yr = place_y_for_floor(R)
            lx = cxL - L.width // 2; rx = cxR - R.width // 2
            # восстановим требуемый зазор (короткий проход)
            ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)
            for _ in range(40):
                if _inner_gap_px(ra, rb) >= min_gap: break
                lx = max(MARGIN, lx - 2)
                rx = min(W - R.width - MARGIN, rx + 2)
                ra = rect_at(lx, yl, L); rb = rect_at(rx, yr, R)

        # 8) Рисуем
        draw_with_shadow(canvas, L, lx, yl)
        draw_with_shadow(canvas, R, rx, yr)
        try:
            _draw_debug_boxes(canvas, [_rect_at(lx, yl, L), _rect_at(rx, yr, R)])
        except Exception:
            pass

    # --- метрики/сохранение ---
    out = f"uploads/start_{base_id}.png"
    metrics = {"W": W, "H": H, "framing": framing_key}

    def _abs_rect(x, y, img):
        (bx, by, bx1, by1), yb = alpha_metrics(img)
        return [x + bx, y + by, x + bx1, y + by1], yb + y

    if len(cuts) == 1:
        rP, fy = _abs_rect(x, y, P)
        h_px = rP[3] - rP[1]
        w_px = rP[2] - rP[0]
        metrics["L"] = {
            "rect_abs": rP, "height_px": int(h_px), "width_px": int(w_px),
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
            "rect_abs": rL, "height_px": int(hL), "width_px": int(wL),
            "height_frac": float(hL)/H,
            "center_x_frac": float((rL[0]+rL[2])/2)/W,
            "scale": float(L.width)/max(1.0, cuts[0].width),
            "floor_y": int(fyl)
        }
        metrics["R"] = {
            "rect_abs": rR, "height_px": int(hR), "width_px": int(wR),
            "height_frac": float(hR)/H,
            "center_x_frac": float((rR[0]+rR[2])/2)/W,
            "scale": float(R.width)/max(1.0, cuts[1].width),
            "floor_y": int(fyr)
        }
        metrics["gap_px"]  = int(gap_px)
        metrics["gap_frac"]= float(gap_px)/W

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
    """Постобработка видео через ffmpeg (склейка + фон-анимация + водяной знак + музыка). С фолбэком, faststart и портативной копией."""
    import tempfile

    def _escape_concat_path(p: str) -> str:
        # экранируем одинарные кавычки для concat-файла
        return os.path.abspath(p).replace("'", "'\\''")

    temp_dir = "renders/temp"
    os.makedirs(temp_dir, exist_ok=True)

    # 1) Финальный титр (PNG)
    title_img_path = f"{temp_dir}/title.png"
    create_title_image(720, 1280, title_text, title_img_path)

    # 2) 2-секундный ролик из титра
    title_video_path = f"{temp_dir}/title_video.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", title_img_path,
        "-t", "2", "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        title_video_path
    ], check=True, capture_output=True)

    # 3) Файл для concat
    concat_list_path = f"{temp_dir}/concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for vp in video_paths:
            f.write(f"file '{_escape_concat_path(vp)}'\n")
        f.write(f"file '{_escape_concat_path(title_video_path)}'\n")

    # 4) Склейка (попытка без перекодирования)
    concat_video_path = f"{temp_dir}/concat_video.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy", "-movflags", "+faststart",
            concat_video_path
        ], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # Фолбэк: перекодирование под общий профиль
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-r", "24",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart",
            concat_video_path
        ], check=True, capture_output=True)

    # 4.5) Деликатная анимация фона (если есть картинка)
    bg_anim_video_path = concat_video_path
    if bg_overlay_file and os.path.isfile(bg_overlay_file):
        try:
            bg_anim_video_path = f"{temp_dir}/with_bg_anim.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-i", concat_video_path,
                "-loop", "1", "-i", bg_overlay_file,
                "-filter_complex",
                "[1:v]scale=720:1280,boxblur=25:1,format=rgba,colorchannelmixer=aa=0.08,setsar=1[ov];"
                "[0:v][ov]overlay=x='t*2':y=0:shortest=1,format=yuv420p[v]",
                "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
                "-movflags", "+faststart",
                bg_anim_video_path
            ], check=True, capture_output=True)
        except Exception as e:
            print(f"BG overlay skipped: {e}")
    else:
        print("BG overlay disabled (no file)")

    # 5) Водяной знак
    wm_video_path = bg_anim_video_path
    if os.path.isfile(WATERMARK_PATH):
        wm_video_path = f"{temp_dir}/with_watermark.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", bg_anim_video_path, "-i", WATERMARK_PATH,
            "-filter_complex", "[1:v]scale=120:-1[wm];[0:v][wm]overlay=W-w-24:24",
            "-c:a", "copy",
            "-movflags", "+faststart",
            wm_video_path
        ], check=True, capture_output=True)

    # 6) Музыка (или просто сохранить)
    if music_path and os.path.isfile(music_path):
        # зациклить музыку и подложить под видео
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", music_path,     # бесконечная музыка
            "-i", wm_video_path,                         # видео
            "-map", "1:v", "-map", "0:a",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100",
            "-shortest", "-af", "volume=0.6",
            "-movflags", "+faststart",
            save_as
        ], check=True, capture_output=True)
    else:
        # портативная копия + faststart
        import shutil
        shutil.copyfile(wm_video_path, save_as)
        try:
            tmp_fast = f"{temp_dir}/faststart.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", save_as, "-c", "copy", "-movflags", "+faststart", tmp_fast
            ], check=True, capture_output=True)
            shutil.move(tmp_fast, save_as)
        except Exception:
            pass

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
    # Музыка может быть «без музыки», поэтому достаточно, что сцений/формат/фон выбраны
    if not (st["scenes"] and st["format"] and st["bg"]):
        bot.send_message(uid, "Сначала пройдите шаги: Формат → Сюжет(ы) → Фон → (Музыка — можно «Без музыки»).")
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
        f"PREVIEW_START_FRAME: {PREVIEW_START_FRAME}\n"
        f"DEBUG_TO_ADMIN: {DEBUG_TO_ADMIN}\n"
        f"RUNWAY_SEND_JPEG: {RUNWAY_SEND_JPEG}\n"
    )
    bot.reply_to(m, txt)

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
# === HARD-OFF for OpenAI Assistants (safe stub layer) =========================
# Отключаем любые проверки/добавки от Assistant'а и делаем функции-стабы.

try:
    ASSISTANT_GATE_ENABLED = False  # на всякий — принудительно OFF
except NameError:
    pass

def _short_gate(g: dict | None) -> str:  # используется в превью — оставим нейтральный вывод
    return "gate: disabled"

def _normalize_gate(g: dict | None) -> dict | None:
    return None

def oai_upload_image(path: str) -> str | None:
    # не загружаем ничего в Assistants
    return None

def oai_create_thread_with_image(user_text: str, file_id: str) -> str | None:
    # не создаём thread в Assistants
    return None

def oai_gate_check(start_frame_path: str, base_prompt: str, meta: dict, timeout_sec: int = 120) -> dict | None:
    # всегда «без вмешательства»: возвращаем None
    return None

# ==============================================================================
def run_all_and_send(uid: int, st: dict):
    """
    Минимально-устойчивый пайплайн без OpenAI Assistants:
    - строим старт-кадр;
    - собираем промпт (плюс безопасные добавки);
    - отправляем превью (если включено);
    - гоняем Runway (c фолбэком на 5с при 4xx);
    - постобработка и отправка результата.
    """
    framing_text = FORMATS[st["format"]]
    bg_prompt    = BG_TEXT[st["bg"]]                      # мягкая анимация фона
    music_path   = MUSIC.get(st["music"]) if st["music"] else None
    bg_file      = BG_FILES[st["bg"]]                     # «плейт» (картинка фона)

    out_videos = []

    for scene_key in st["scenes"]:
        scene = SCENES[scene_key]

        # 1) Старт-кадр (без ручного layout — он уже внутри make_start_frame делает LEAN-раскладку)
        start_frame = make_start_frame(st["photos"], st["format"], bg_file, layout=None)

        # 2) Базовый промпт + жесткие ограничения на геометрию + «страхующие» добавки (всегда)
        base_prompt = build_prompt(scene["kind"], framing_text, bg_prompt, scene["duration"])
        base_prompt += (
            "; lock geometry exactly as in the provided start frame (positions and scales)"
            "; no zoom, no dolly, no push-in/out, no drift; keep constant relative size"
            "; full-body shot; preserve limb topology; no body/limb deformation; no warping"
            "; do not change background plate geometry; do not crop heads, hands, or feet"
        )
        prompt = compact_prompt(base_prompt + " " + BACKUP_PROMPT_ADDITIONS)

        # 3) Превью (если включено)
        try:
            if PREVIEW_START_FRAME:
                _send_debug_preview(uid, scene_key, start_frame, prompt, gate=None)  # gate отключен
        except Exception as _e:
            print(f"[DBG] preview send err: {_e}")

        # 4) Готовим изображение к отправке в Runway (JPEG по флагу + контроль лимита data URI)
        send_path = ensure_jpeg_copy(start_frame) if RUNWAY_SEND_JPEG else start_frame
        data_uri, used_path = ensure_runway_datauri_under_limit(send_path)
        try:
            fs = os.path.getsize(used_path)
            print(f"[Runway] start_frame path={used_path} size={fs} bytes (jpeg={RUNWAY_SEND_JPEG})")
        except Exception:
            pass
        if not data_uri or len(data_uri) < 64:
            bot.send_message(uid, f"Сцена «{scene_key}»: пустой data URI старт-кадра")
            continue
        if len(data_uri) > 5_000_000:
            print(f"[Runway warn] data URI length {len(data_uri)} > 5MB; consider lower JPEG quality.")

        # 5) Запуск Runway с фолбэком на 5с при 4xx
        try:
            start_resp = runway_start(data_uri, prompt, scene["duration"])
        except RuntimeError as e:
            msg = str(e)
            if "400/4xx" in msg and int(scene["duration"]) > 5:
                bot.send_message(uid, f"⚠️ Runway отказал в {scene['duration']}с для «{scene_key}». Пробую 5с.")
                prompt_short = compact_prompt(
                    build_prompt(scene["kind"], framing_text, bg_prompt, 5) + " " + BACKUP_PROMPT_ADDITIONS
                )
                try:
                    start_resp = runway_start(data_uri, prompt_short, 5)
                    prompt = prompt_short  # фиксируем фактический промпт/длительность в отладке
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

        # 6) Ожидание результата и скачивание
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

        # 7) Отладочный дамп (короткий)
        try:
            if OAI_DEBUG:
                os.makedirs("renders/temp", exist_ok=True)
                dbg_path = os.path.join("renders/temp", f"runway_dbg_{uid}_{uuid.uuid4().hex}.json")
                with open(dbg_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "scene": scene_key,
                        "format": st["format"],
                        "background": st["bg"],
                        "start_frame": used_path,
                        "final_prompt": prompt
                    }, f, ensure_ascii=False, indent=2)
                print(f"[RUNWAY DBG] saved {dbg_path}\n[RUNWAY DBG] final_prompt_len={len(prompt)}")
        except Exception as _e:
            print(f"[RUNWAY DBG] save error: {_e}")

    # --- Финал: постобработка и отправка ---
    if not out_videos:
        bot.send_message(uid, "Ни одна сцена не сгенерировалась. Попробуйте другие фото.")
        users[uid] = new_state()
        show_main_menu(uid)
        return

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    final_path = f"renders/{uid}_{timestamp}_{uuid.uuid4().hex}_FINAL.mp4"
    title_text = "Memory Forever — Память навсегда с вами"

    try:
        postprocess_concat_ffmpeg(out_videos, music_path, title_text, final_path, bg_overlay_file=bg_file)
    except Exception as e:
        print(f"Postprocess error: {e}")
        bot.send_message(uid, f"Постобработка не удалась ({e}). Шлю сырые сцены по отдельности.")
        for i, p in enumerate(out_videos, 1):
            with open(p, "rb") as f:
                bot.send_video(uid, f, caption=f"Сцена {i}")
        cleanup_artifacts(keep_last=10)
        users[uid] = new_state()
        show_main_menu(uid, "Готово! Видео отправлены (постобработка не удалась).")
        return

    with open(final_path, "rb") as f:
        cap = " · ".join(st["scenes"]) + f" · {st['format']}"
        bot.send_video(uid, f, caption=cap)

    cleanup_artifacts(keep_last=10)
    users[uid] = new_state()
    show_main_menu(uid, "Готово! Видео создано успешно.")
# ==============================================================================

# ---------- ОБРАБОТЧИКИ CALLBACK-КНОПОК МУЗЫКИ ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("listen_"))
def on_music_listen(call):
    uid = call.from_user.id
    music_name = call.data.replace("listen_", "")
    music_path = MUSIC_BY_CLEAN.get(music_name)   # ← без find_music_by_name

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
    uid = call.from_user.id
    st = users.setdefault(uid, new_state())

    music_choice = call.data.replace("select_music_", "")

    if music_choice == "none":
        st["music"] = None
        bot.answer_callback_query(call.id, "🔇 Выбрано: Без музыки")
    else:
        if music_choice in MUSIC_BY_CLEAN:
            st["music"] = f"🎵 {music_choice}"        # храним ключ, как в меню
            bot.answer_callback_query(call.id, f"✅ Выбрано: {music_choice}")
        else:
            bot.answer_callback_query(call.id, "Музыка не найдена")
            return

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
