[bot.py](https://github.com/user-attachments/files/26811596/bot.py)
"""
Telegram-бот для распознавания заказ-нарядов автосервиса.

Принимает фото доски с работами и скриншоты WhatsApp с запчастями,
распознаёт через Claude API и возвращает структурированные данные.
"""

import os
import logging
import base64
import json
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from anthropic import Anthropic

try:
    from rapidfuzz import process, fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

# Расшифровка сокращений для улучшения поиска
ABBREVIATIONS = {
    r"\bАКБ\b": "аккумулятор",
    r"\bАБС\b": "ABS",
    r"\bАВС\b": "ABS",
    r"\bКПП\b": "коробка передач",
    r"\bГУР\b": "гидроусилитель руля",
    r"\bТНВД\b": "топливный насос высокого давления",
    r"\bДВС\b": "двигатель",
    r"\bГБЦ\b": "головка блока цилиндров",
    r"\bТ/О\b": "техническое обслуживание",
    r"\bТО\b": "техническое обслуживание",
    r"\bс/у\b": "снять установить",
    r"\bс/з\b": "снять заменить",
    r"\bп/п\b": "полуприцеп",
    r"\bп/прицеп\b": "полуприцеп",
    r"\bпневма\b": "пневмо",
}


# Русские окончания, отсортированные от длинных к коротким —
# будет отрезаться первое найденное совпадение
RU_SUFFIXES = sorted([
    # 4-5 букв
    "ствии", "ствия", "ствием", "ованию",
    "ения", "ение", "ении", "ением",
    "ания", "ание", "ании", "анием",
    "ющий", "ющая", "ющее", "ющие", "ющих", "ющим", "ющей",
    "ающ", "яющ", "ующ", "ивш", "евш",
    "ового", "овом", "овому", "ового",
    # 2-3 буквы
    "ами", "ями", "лся", "тся", "ими", "ыми",
    "ого", "его", "ому", "ему",
    "ий", "ый", "ой", "ей", "ий", "ая", "яя", "ое", "ее", "ые", "ие",
    "ов", "ев", "ом", "им", "ым",
    "ах", "ях",
    "ть", "ти", "ут", "ют", "ат", "ят",
    "ет", "ём", "ит", "ло", "ла", "ли", "ен", "на", "но", "ны",
    # 1 буква
    "а", "я", "о", "е", "ё", "ы", "и", "у", "ю", "й", "ь", "ъ",
], key=len, reverse=True)


def stem_ru(word: str, min_stem: int = 4) -> str:
    """Отрезает общее русское окончание если основа остаётся длиной ≥ min_stem."""
    w = word.lower()
    for suf in RU_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= min_stem:
            return w[:-len(suf)]
    return w


def normalize_text(s: str) -> str:
    """Приводит текст к виду для fuzzy-поиска:
    - расшифровка сокращений (АКБ → аккумулятор)
    - нижний регистр
    - токенизация и стемминг (обрезка русских окончаний)
    """
    import re
    if not s:
        return ""
    t = s
    for pat, rep in ABBREVIATIONS.items():
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    tokens = re.findall(r"\w+", t.lower(), flags=re.UNICODE)
    stemmed = [stem_ru(tok) for tok in tokens if len(tok) >= 2]
    return " ".join(stemmed)

# ============================================================
# НАСТРОЙКИ - ЗАПОЛНИ ПЕРЕД ЗАПУСКОМ
# ============================================================

# Токен от @BotFather
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА_СЮДА")

# Ключ от console.anthropic.com
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "ВСТАВЬ_CLAUDE_API_КЛЮЧ_СЮДА")

# Telegram ID пользователей, которым разрешено пользоваться ботом.
# Пустой список = доступ всем. Узнать свой ID можно у @userinfobot.
ALLOWED_USER_IDS = []

# Модель Claude с поддержкой зрения
CLAUDE_MODEL = "claude-sonnet-4-5"

# Путь к справочнику работ (works.json — конвертированный из Excel)
WORKS_CATALOG_PATH = os.environ.get("WORKS_CATALOG", "works.json")

# Порог уверенности для автоподстановки названия из справочника (0-100)
FUZZY_MATCH_THRESHOLD = 80

# ============================================================

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Загрузка справочника работ
WORKS_CATALOG: list[dict] = []
WORKS_NAMES_NORMALIZED: list[str] = []  # нормализованные названия для поиска


def load_works_catalog():
    """Загружает works.json в память и нормализует названия для поиска."""
    global WORKS_CATALOG, WORKS_NAMES_NORMALIZED
    p = Path(WORKS_CATALOG_PATH)
    if not p.exists():
        logging.warning(f"Справочник работ не найден: {p.absolute()}")
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            WORKS_CATALOG = json.load(f)
        WORKS_NAMES_NORMALIZED = [normalize_text(w["n"]) for w in WORKS_CATALOG]
        logging.info(f"Загружен справочник: {len(WORKS_CATALOG)} работ")
    except Exception as e:
        logging.error(f"Ошибка загрузки справочника: {e}")


def match_work(raw_name: str) -> dict | None:
    """
    Ищет лучшее совпадение для сырого названия работы.
    Возвращает {name, code, group, score} или None если совпадений нет.
    """
    if not FUZZY_AVAILABLE or not WORKS_NAMES_NORMALIZED or not raw_name:
        return None
    # Чистим входящую строку от времени/номеров и нормализуем сокращения
    import re
    cleaned = re.sub(r"\d{1,2}[.:]\d{2}", "", raw_name)  # убрать время
    cleaned = re.sub(r"[^\w\s/-]", " ", cleaned, flags=re.UNICODE)  # убрать знаки
    query = normalize_text(cleaned)
    if len(query) < 3:
        return None
    # Комбинированный скоринг: среднее между token_sort_ratio (строгий) и
    # token_set_ratio (гибкий). Это даёт баланс точности и полноты.
    def combined_scorer(q, n, **kwargs):
        return (fuzz.token_sort_ratio(q, n) + fuzz.token_set_ratio(q, n)) / 2

    match = process.extractOne(
        query, WORKS_NAMES_NORMALIZED,
        scorer=combined_scorer,
        score_cutoff=FUZZY_MATCH_THRESHOLD,
    )
    if match is None:
        return None
    _, score, idx = match
    entry = WORKS_CATALOG[idx]
    return {
        "name": entry.get("n"),
        "code": entry.get("c"),
        "group": entry.get("g"),
        "score": round(score),
    }

# Простое хранилище данных в памяти (сбрасывается при перезапуске)
user_data_store: dict[int, dict] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def check_access(update: Update) -> bool:
    """Проверяет разрешён ли пользователю доступ к боту."""
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        await update.message.reply_text(
            f"Нет доступа. Твой ID: {update.effective_user.id}"
        )
        return
    await update.message.reply_text(
        "Привет! Я помогу создавать заказ-наряды.\n\n"
        "Как пользоваться:\n"
        "1. Кидай фото доски с работами (можно с подписью 'доска')\n"
        "2. Кидай скриншот WhatsApp с запчастями (подпись 'whatsapp')\n"
        "3. Я распознаю и соберу данные\n\n"
        "✏️ Правка работ:\n"
        "Напиши в чат: 'Н.М новый текст'\n"
        "где Н = номер наряда, М = номер работы\n"
        "Пример: 1.3 замена перемычки аккумулятора\n\n"
        "Команды:\n"
        "/start — показать эту справку\n"
        "/show — показать собранные данные\n"
        "/clear — очистить текущие данные"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        return
    user_id = update.effective_user.id
    user_data_store.pop(user_id, None)
    await update.message.reply_text("Данные очищены. Кидай новые фото.")


async def show_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        return
    user_id = update.effective_user.id
    data = user_data_store.get(user_id, {})
    if not data:
        await update.message.reply_text("Пока ничего нет. Кидай фото!")
        return

    parts = []
    if "board" in data:
        parts.append("🪧 С ДОСКИ:\n" + format_board(data["board"]))
    if "whatsapp" in data:
        parts.append("📱 ИЗ WHATSAPP:\n" + format_whatsapp(data["whatsapp"]))

    await update.message.reply_text("\n\n".join(parts))


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Правка отдельной работы в сохранённом заказ-наряде.
    Формат: 'N.M новый текст' или 'N M новый текст'.
    """
    if not check_access(update):
        return

    import re
    text = (update.message.text or "").strip()
    m = re.match(r"^\s*(\d+)\s*[.\s]\s*(\d+)\s+(.+)$", text, flags=re.DOTALL)
    if not m:
        return  # не похоже на правку — игнорируем

    order_no = int(m.group(1))
    work_no = int(m.group(2))
    new_text = m.group(3).strip()

    user_id = update.effective_user.id
    data = user_data_store.get(user_id, {})
    board = data.get("board")
    if not board:
        await update.message.reply_text(
            "Сначала пришли фото доски — нечего править."
        )
        return

    orders = board.get("orders") or [board]  # поддержка старого формата
    if order_no < 1 or order_no > len(orders):
        await update.message.reply_text(
            f"Нет заказ-наряда №{order_no}. Всего нарядов: {len(orders)}."
        )
        return

    order = orders[order_no - 1]
    works = order.get("works", [])
    if work_no < 1 or work_no > len(works):
        await update.message.reply_text(
            f"В наряде {order_no} нет работы №{work_no}. Всего работ: {len(works)}."
        )
        return

    old_text = works[work_no - 1]
    works[work_no - 1] = new_text

    matched = match_work(new_text)
    if matched:
        code = f"[{matched['code']}] " if matched.get("code") else ""
        reply = (
            f"✏️ Исправил в наряде {order_no}, работа {work_no}:\n"
            f"Было: {old_text}\n"
            f"Стало: {new_text}\n\n"
            f"✅ Найдено в справочнике:\n"
            f"{code}{matched['name']} ({matched['score']}%)"
        )
    else:
        reply = (
            f"✏️ Исправил в наряде {order_no}, работа {work_no}:\n"
            f"Было: {old_text}\n"
            f"Стало: {new_text}\n\n"
            f"⚠️ В справочнике совпадения не нашёл. "
            f"Попробуй другую формулировку или используй /show"
        )
    await update.message.reply_text(reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update):
        return

    user_id = update.effective_user.id
    photo = update.message.photo[-1]  # самое большое
    caption = (update.message.caption or "").lower()

    await update.message.reply_text("📷 Получил фото, распознаю (10-15 сек)...")

    try:
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = bytes(await file.download_as_bytearray())
        photo_b64 = base64.standard_b64encode(photo_bytes).decode("utf-8")
    except Exception as e:
        logger.exception("Ошибка скачивания фото")
        await update.message.reply_text(f"Не смог скачать фото: {e}")
        return

    # Определяем тип фото по подписи, иначе — авто-определение
    is_whatsapp = any(w in caption for w in ("whatsapp", "вотсап", "запчаст"))
    is_board = any(w in caption for w in ("доск", "работ"))

    try:
        if is_whatsapp:
            data = recognize_whatsapp(photo_b64)
            user_data_store.setdefault(user_id, {})["whatsapp"] = data
            await update.message.reply_text(
                "📱 Распознал запчасти:\n\n" + format_whatsapp(data)
            )
        elif is_board:
            data = recognize_board(photo_b64)
            user_data_store.setdefault(user_id, {})["board"] = data
            await update.message.reply_text(
                "🪧 Распознал с доски:\n\n" + format_board(data)
                + "\n\n✏️ Правка: напиши 'Н.М новый текст' "
                  "(Н=номер наряда, М=номер работы)"
            )
        else:
            data = recognize_auto(photo_b64)
            photo_type = data.get("type")
            if photo_type == "whatsapp":
                user_data_store.setdefault(user_id, {})["whatsapp"] = data
                await update.message.reply_text(
                    "📱 Распознал запчасти:\n\n" + format_whatsapp(data)
                )
            elif photo_type == "board":
                user_data_store.setdefault(user_id, {})["board"] = data
                await update.message.reply_text(
                    "🪧 Распознал с доски:\n\n" + format_board(data)
                    + "\n\n✏️ Правка: напиши 'Н.М новый текст' "
                      "(Н=номер наряда, М=номер работы)"
                )
            else:
                await update.message.reply_text(
                    "Не понял что на фото. Добавь подпись: 'доска' или 'whatsapp'."
                )
    except json.JSONDecodeError as e:
        logger.exception("Claude вернул невалидный JSON")
        await update.message.reply_text(
            f"Claude вернул непонятный ответ. Попробуй другое фото.\n{e}"
        )
    except Exception as e:
        logger.exception("Ошибка распознавания")
        await update.message.reply_text(f"Ошибка: {e}")


# ============================================================
# Распознавание через Claude
# ============================================================

def _call_claude_vision(photo_b64: str, prompt: str, max_tokens: int = 2048) -> dict:
    """Отправляет фото и промпт в Claude, парсит JSON-ответ."""
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": photo_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    text = message.content[0].text.strip()

    # Снимаем обёртку ```json ... ``` если есть
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


def recognize_board(photo_b64: str) -> dict:
    prompt = (
        "На фото — доска автосервиса. На ней может быть НЕСКОЛЬКО заказ-нарядов "
        "(несколько строк/записей, по одной на каждую машину).\n"
        "Каждый заказ-наряд обычно содержит: номер или гос.номер в начале, "
        "список работ, иногда время.\n\n"
        "КРИТИЧЕСКИ ВАЖНО про список работ:\n"
        "- В рукописном тексте работы ВСЕГДА разделены запятой ',' или точкой с запятой ';'\n"
        "- РАЗБЕЙ каждую составную строку на отдельные элементы массива works по этим разделителям\n"
        "- Сохраняй порядок как на доске\n"
        "- Союз 'и' между существительными означает связанное понятие — НЕ разбивай по нему "
        "(например 'трапеция и дворник' — одна работа)\n"
        "- Не объединяй отдельные работы в одну строку\n"
        "- Не добавляй время/номера/цифры в работы — они идут отдельно в поле time\n\n"
        "Примеры разбиения:\n"
        "  Ввод: 'чистка, мойка АКБ, замена перемычка'\n"
        "  Вывод: [\"чистка\", \"мойка АКБ\", \"замена перемычка\"]\n\n"
        "  Ввод: 'проводка АКБ, диагностика, удаление ошибок'\n"
        "  Вывод: [\"проводка АКБ\", \"диагностика\", \"удаление ошибок\"]\n\n"
        "  Ввод: 'замена трапеции и дворников'\n"
        "  Вывод: [\"замена трапеции и дворников\"]  // 'и' связывает — не разбивать\n\n"
        "Извлеки ВСЕ заказ-наряды с доски в JSON СТРОГО по схеме:\n"
        "{\n"
        '  "orders": [\n'
        "    {\n"
        '      "order_no": "номер заказа или null",\n'
        '      "plate": "гос.номер или null",\n'
        '      "car": "марка и модель или null",\n'
        '      "client": "клиент или null",\n'
        '      "phone": "телефон или null",\n'
        '      "vin": "VIN или null",\n'
        '      "mileage": "пробег или null",\n'
        '      "complaint": "причина обращения или null",\n'
        '      "works": ["работа 1", "работа 2"],\n'
        '      "master": "мастер или null",\n'
        '      "time": "время или null"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Отвечай ТОЛЬКО JSON без комментариев."
    )
    return _call_claude_vision(photo_b64, prompt, max_tokens=4096)


def recognize_whatsapp(photo_b64: str) -> dict:
    prompt = (
        "На фото — скриншот переписки WhatsApp со списком запчастей для автосервиса.\n"
        "Извлеки все запчасти в JSON СТРОГО по схеме:\n"
        "{\n"
        '  "parts": [\n'
        '    {"number": "артикул или null", "name": "название", "quantity": 1, "price": null}\n'
        "  ]\n"
        "}\n"
        "Отвечай ТОЛЬКО JSON."
    )
    return _call_claude_vision(photo_b64, prompt, max_tokens=2048)


def recognize_auto(photo_b64: str) -> dict:
    prompt = (
        "На фото может быть одно из:\n"
        "1. Доска автосервиса со списком заказ-нарядов (может быть несколько машин)\n"
        "2. Скриншот WhatsApp со списком запчастей\n\n"
        "Определи тип и извлеки ВСЕ данные в JSON:\n\n"
        "Для доски (извлеки ВСЕ строки): "
        '{"type": "board", "orders": [{"order_no": "...", "plate": "...", '
        '"car": null, "client": null, "works": [...], "time": null}]}\n\n'
        "Для WhatsApp: "
        '{"type": "whatsapp", "parts": [{"number": "...", "name": "...", '
        '"quantity": 1, "price": null}]}\n\n'
        "Отвечай ТОЛЬКО JSON."
    )
    return _call_claude_vision(photo_b64, prompt, max_tokens=4096)


# ============================================================
# Форматирование ответа для Telegram
# ============================================================

def format_board(data: dict) -> str:
    # Новый формат — список заказ-нарядов
    orders = data.get("orders")
    if orders is None and any(k in data for k in ("works", "plate", "client")):
        # Обратная совместимость со старым форматом (один заказ-наряд)
        orders = [data]

    if not orders:
        return "(заказ-нарядов не нашёл)"

    chunks = [f"Всего: {len(orders)} заказ-нарядов"]
    for i, order in enumerate(orders, 1):
        header = f"\n━━━ Заказ-наряд {i} ━━━"
        if order.get("order_no"):
            header += f"  №{order['order_no']}"
        chunks.append(header)
        if order.get("plate"):
            chunks.append(f"🔢 Гос.номер: {order['plate']}")
        if order.get("car"):
            chunks.append(f"🚗 Авто: {order['car']}")
        if order.get("client"):
            chunks.append(f"👤 Клиент: {order['client']}")
        if order.get("phone"):
            chunks.append(f"📞 Телефон: {order['phone']}")
        if order.get("vin"):
            chunks.append(f"🆔 VIN: {order['vin']}")
        if order.get("mileage"):
            chunks.append(f"📏 Пробег: {order['mileage']}")
        if order.get("complaint"):
            chunks.append(f"💬 Жалоба: {order['complaint']}")
        if order.get("works"):
            chunks.append("🔧 Работы:")
            for j, w in enumerate(order["works"], 1):
                matched = match_work(w)
                if matched and matched["score"] >= FUZZY_MATCH_THRESHOLD:
                    code = f"[{matched['code']}] " if matched.get("code") else ""
                    chunks.append(
                        f"  {j}. {code}{matched['name']} ({matched['score']}%)"
                    )
                    chunks.append(f"      ↳ ориг.: {w}")
                else:
                    chunks.append(f"  {j}. {w} ⚠️ не нашёл в справочнике")
        if order.get("master"):
            chunks.append(f"👷 Мастер: {order['master']}")
        if order.get("time"):
            chunks.append(f"🕐 Время: {order['time']}")
    return "\n".join(chunks)


def format_whatsapp(data: dict) -> str:
    parts = data.get("parts", [])
    if not parts:
        return "(запчастей не нашёл)"
    lines = [f"Всего: {len(parts)} позиций"]
    for i, p in enumerate(parts, 1):
        line = f"{i}. "
        if p.get("number"):
            line += f"[{p['number']}] "
        line += p.get("name", "—")
        if p.get("quantity") and p["quantity"] != 1:
            line += f" × {p['quantity']}"
        if p.get("price"):
            line += f" — {p['price']} ₽"
        lines.append(line)
    return "\n".join(lines)


# ============================================================
# Запуск
# ============================================================

def main():
    if TELEGRAM_TOKEN.startswith("ВСТАВЬ") or ANTHROPIC_API_KEY.startswith("ВСТАВЬ"):
        raise SystemExit(
            "Заполни TELEGRAM_TOKEN и ANTHROPIC_API_KEY в начале файла "
            "или установи переменные окружения."
        )

    load_works_catalog()
    if not FUZZY_AVAILABLE:
        logging.warning(
            "rapidfuzz не установлен — подстановка работ из справочника "
            "отключена. Установите: pip3 install rapidfuzz"
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("show", show_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Правка работ: "N.M новый текст"
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\s*\d+\s*[.\s]\s*\d+\s+\S"),
            handle_edit,
        )
    )

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
