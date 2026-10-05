"""Настройки TL;DR бота. Всё читается из переменных окружения."""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- Telegram ---
BOT_TOKEN = _env("BOT_TOKEN")

# --- LLM (OpenAI-совместимый API) ---
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")

# --- Whisper (распознавание голосовых) ---
WHISPER_MODEL = _env("WHISPER_MODEL", "whisper-1")

# --- Лимиты ---
MAX_TEXT_CHARS = int(_env("MAX_TEXT_CHARS", "60000"))      # сколько текста отправляем в LLM
MAX_FILE_BYTES = int(_env("MAX_FILE_BYTES", "20971520"))   # 20 МБ — лимит Telegram Bot API
MAX_VOICE_BYTES = int(_env("MAX_VOICE_BYTES", "26214400")) # 25 МБ для аудио
TELEGRAM_MSG_LIMIT = 4096                                  # лимит длины сообщения Telegram

# --- Тексты уровней краткости ---
LEVELS = {
    "one": ("1️⃣ Одно предложение", "сформулируй ГЛАВНУЮ мысль в ОДНОМ предложении (максимум 30 слов)"),
    "short": ("2️⃣ Кратко", "сделай краткую выжимку: 3–5 предложений или до 5 маркированных пунктов, только суть"),
    "detail": ("3️⃣ Подробно", "сделай подробный конспект: структурируй по темам, сохрани важные факты, цифры, имена и выводы"),
}

STYLE_NOTES = (
    "Пиши по-русски (если исходник на другом языке — переводи суть на русский). "
    "Тон живой и дружелюбный, без канцелярита. Не добавляй пояснений от себя, "
    "только выжимку. Важные слова выделяй *звёздочками*, списки начинай с «—». "
    "Не используй другие символы разметки (никаких <тегов>, _подчёркиваний_ и `код`)."
)
