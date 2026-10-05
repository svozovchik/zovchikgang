"""Хендлеры TL;DR бота: текст, ссылки, голосовые, документы."""
from __future__ import annotations

import html
import logging
import re

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,  # ✅ Импортирован настоящий класс-хендлер
    filters,
)

from . import config, extract, summarizer, telegram_utils as tu

log = logging.getLogger("tldr.handlers")

URL_RE = re.compile(
    r"https?://[^\s<>\"']+", re.IGNORECASE
)

WELCOME = (
    "🤖 <b>TL;DR бот</b> — экономит твоё время.\n\n"
    "Присылай:\n"
    "• 📄 длинный пост / статью / новость текстом\n"
    "• 🔗 ссылку на статью — заберу текст с страницы\n"
    "• 🎙 голосовое или аудиофайл — расшифрую и перескажу\n"
    "• 📑 PDF / DOCX / TXT — выжму главное\n\n"
    "Выбери уровень краткости кнопками ниже 👇 (сейчас: {level})\n\n"
    "<i>Совет: в группах можно просто ответить (reply) на любое сообщение — "
    "перескажу его.</i>"
)

TOO_SHORT = "🤔 Слишком короткое сообщение — тут и так всё ясно. Кидай то, что реально долго читать!"


# ---------- утилиты состояния ----------

def current_level(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.chat_data.get("level", "short")


def set_level(context: ContextTypes.DEFAULT_TYPE, level: str) -> None:
    context.chat_data["level"] = level


def level_name(key: str) -> str:
    """«Кратко» без цифры-эмодзи."""
    return config.LEVELS[key][0].split("️⃣", 1)[-1].strip()


async def reply_html(message, text: str) -> None:
    """Ответ с HTML; при битой разметке — обычным текстом."""
    try:
        await message.reply_text(text, parse_mode=ParseMode.HTML)
    except BadRequest:
        log.warning("HTML parse failed for: %r", text[:120])
        await message.reply_text(re.sub(r"<[^>]+>", "", text))


async def notify_working(message, text: str = "⏳ Читаю…") -> int | None:
    try:
        m = await message.reply_text(text)
        return m.message_id
    except Exception:
        return None


async def edit_or_delete(context, chat_id: int, msg_id: int | None, new_text: str | None) -> None:
    """Обновить «Читаю…» результат-заглушкой или удалить."""
    if msg_id is None:
        return
    try:
        if new_text is None:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=new_text
            )
    except Exception:
        pass  # не критично


# ---------- загрузка исходника ----------

async def fetch_url(url: str) -> str:
    """Скачать страницу и вытащить из неё читаемый текст (trafilatura -> readability)."""
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=20
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")

        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            return extract.extract_text("file.pdf", resp.content)

        text = ""
        try:
            import trafilatura
            text = trafilatura.extract(
                resp.text, include_comments=False, favor_precision=True
            ) or ""
        except ImportError:
            pass

        if len(text.strip()) < 200:
            from readability import Document
            doc = Document(resp.text)
            raw = doc.summary()
            raw = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", raw, flags=re.I)
            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = html.unescape(raw)
            raw = re.sub(r"[ \t]+", " ", raw)
            text = re.sub(r"\n\s*\n+", "\n\n", raw).strip()

        if len(text) < 200:
            raise ValueError("Со страницы почти не удалось вытащить текст.")
        return text


# ---------- ядро: взять текст -> суммаризировать -> отправить ----------

async def process_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source_text: str,
    voice_hint: bool = False,
    label: str = "",
    progress_id: int | None = None,
) -> None:
    message = update.effective_message
    target = tu.get_chat_kwargs(message)
    level = current_level(context)

    if len(source_text.strip()) < 80 and not voice_hint:
        await edit_or_delete(context, message.chat_id, progress
