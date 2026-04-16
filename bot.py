"""
Telegram-бот для распознавания заказ-нарядов автосервиса.

Принимает фото доски с работами и скриншоты WhatsApp с запчастями,
распознаёт через Claude API и возвращает структурированные данные.
"""

import os
import logging
import base64
import json

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from anthropic import Anthropic

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

# ============================================================

client = Anthropic(api_key=ANTHROPIC_API_KEY)

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
        "На фото — доска или записка автосервиса с данными по ремонту.\n"
        "Извлеки данные в JSON СТРОГО по схеме:\n"
        "{\n"
        '  "client": "имя клиента или null",\n'
        '  "phone": "телефон или null",\n'
        '  "car": "марка и модель",\n'
        '  "plate": "гос. номер",\n'
        '  "vin": "VIN или null",\n'
        '  "mileage": "пробег или null",\n'
        '  "complaint": "причина обращения или null",\n'
        '  "works": ["работа 1", "работа 2"],\n'
        '  "master": "мастер или null"\n'
        "}\n"
        "Отвечай ТОЛЬКО JSON без комментариев."
    )
    return _call_claude_vision(photo_b64, prompt, max_tokens=1024)


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
        "1. Доска или записка автосервиса с данными ремонта (клиент, авто, работы)\n"
        "2. Скриншот WhatsApp со списком запчастей\n\n"
        "Определи тип и извлеки данные в JSON:\n"
        "Для доски: "
        '{"type": "board", "client": "...", "phone": null, "car": "...", '
        '"plate": "...", "vin": null, "mileage": null, "complaint": "...", '
        '"works": [...], "master": null}\n'
        "Для WhatsApp: "
        '{"type": "whatsapp", "parts": [{"number": "...", "name": "...", '
        '"quantity": 1, "price": null}]}\n'
        "Отвечай ТОЛЬКО JSON."
    )
    return _call_claude_vision(photo_b64, prompt, max_tokens=2048)


# ============================================================
# Форматирование ответа для Telegram
# ============================================================

def format_board(data: dict) -> str:
    lines = []
    if data.get("client"):
        lines.append(f"👤 Клиент: {data['client']}")
    if data.get("phone"):
        lines.append(f"📞 Телефон: {data['phone']}")
    if data.get("car"):
        lines.append(f"🚗 Авто: {data['car']}")
    if data.get("plate"):
        lines.append(f"🔢 Гос.номер: {data['plate']}")
    if data.get("vin"):
        lines.append(f"🆔 VIN: {data['vin']}")
    if data.get("mileage"):
        lines.append(f"📏 Пробег: {data['mileage']}")
    if data.get("complaint"):
        lines.append(f"💬 Жалоба: {data['complaint']}")
    if data.get("works"):
        lines.append("🔧 Работы:")
        for i, w in enumerate(data["works"], 1):
            lines.append(f"  {i}. {w}")
    if data.get("master"):
        lines.append(f"👷 Мастер: {data['master']}")
    return "\n".join(lines) if lines else "(данных не нашёл)"


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

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("show", show_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
