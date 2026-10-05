"""Telegram-обёртки: загрузка файлов, разбивка длинных ответов, кнопки."""
from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import config

log = logging.getLogger("tldr.telegram_utils")


def level_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, (label, _) in config.LEVELS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"lvl:{key}")])
    return InlineKeyboardMarkup(rows)


async def download_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


def split_message(text: str, limit: int = config.TELEGRAM_MSG_LIMIT) -> list[str]:
    """Разбить длинный текст на части <= limit по границам абзацев/слов. Без потерь."""
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current:
            parts.append(current)
            current = ""

    for para in text.split("\n\n"):
        block = para.strip()
        if not block:
            continue
        # слишком длинные строки внутри блока — режем по словам
        while len(block) > limit:
            room = limit - (len(current) + 2 if current else 0)
            if room < 100:
                flush()
                room = limit
            cut = block.rfind(" ", 0, room)
            cut = room if cut <= 0 else cut
            chunk, block = block[:cut], block[cut:].lstrip()
            current = f"{current}\n\n{chunk}" if current else chunk
            if len(current) >= limit:
                flush()
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            flush()
            current = block
    flush()
    return parts or [text[:limit]]


async def send_long(
    context: ContextTypes.DEFAULT_TYPE,
    target: dict,
    text: str,
    reply_to: int | None = None,
) -> None:
    """Отправить длинный текст частями; при сбое Markdown — обычным текстом."""
    parts = split_message(text)
    for i, part in enumerate(parts):
        prefix = f"_[продолжение {i}]_\n\n" if i > 0 and len(parts) > 1 else ""
        body = prefix + part
        kwargs = dict(target, text=body)
        if i == 0 and reply_to:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to)
        try:
            await context.bot.send_message(**kwargs, parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("HTML parse failed, sending as plain text", exc_info=True)
            await context.bot.send_message(**kwargs)


def get_chat_kwargs(message: Message) -> dict:
    """Параметры отправки в тот же чат/тред, что и входящее сообщение."""
    kwargs = {"chat_id": message.chat_id}
    if getattr(message, "is_topic_message", False) and message.message_thread_id:
        kwargs["message_thread_id"] = message.message_thread_id
    return kwargs
