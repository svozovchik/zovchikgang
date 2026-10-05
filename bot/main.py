"""Хендлеры TL;DR бота: текст, ссылки, голосовые, документы."""
from __future__ import annotations

import html
import logging
import re

from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
        await edit_or_delete(context, message.chat_id, progress_id, None)
        await reply_html(message, TOO_SHORT)
        return

    if progress_id is None:
        progress_id = await notify_working(
            message, f"⏳ Читаю{label} на уровне «{level_name(level)}»…"
        )
    else:
        await edit_or_delete(
            context, message.chat_id, progress_id,
            f"⏳ Читаю{label} на уровне «{level_name(level)}»…",
        )
    await context.bot.send_chat_action(**target, action=ChatAction.TYPING)

    try:
        summary = await summarizer.summarize(source_text, level, voice_hint=voice_hint)
    except ValueError as e:
        await edit_or_delete(context, message.chat_id, progress_id, f"😅 {e}")
        return
    except RuntimeError as e:
        await edit_or_delete(context, message.chat_id, progress_id, f"😵 {e}")
        return
    except Exception as e:
        log.exception("summarize failed")
        detail = str(e)[:200]
        await edit_or_delete(
            context, message.chat_id, progress_id,
            f"💥 Что-то сломалось: {detail}\nПопробуй ещё раз.",
        )
        return

    await edit_or_delete(context, message.chat_id, progress_id, None)
    header = f"✂️ <b>TL;DR</b> ({config.LEVELS[level][0]}):\n\n"
    # LLM может вернуть *выделения* (просим в промпте) — превращаем в HTML-жирный,
    # экранируя всё остальное. Непарные звёздочки остаются текстом.
    esc = html.escape(summary)
    esc = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", esc)
    full = header + esc
    await tu.send_long(context, target, full, reply_to=message.message_id)


# ---------- хендлеры сообщений ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    level = current_level(context)
    await update.effective_message.reply_html(
        WELCOME.format(level=config.LEVELS[level][0]),
        reply_markup=tu.level_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = (context.args or [""])[0].lower()
    aliases = {"1": "one", "off": "one", "кратко": "short", "2": "short",
               "подробно": "detail", "3": "detail"}
    key = aliases.get(args, args)
    if key in config.LEVELS:
        set_level(context, key)
        await reply_html(
            update.effective_message,
            f"✅ Теперь жму до уровня «{config.LEVELS[key][0]}». Кидай контент!",
        )
    else:
        await reply_html(
            update.effective_message,
            "Уровни: <code>/level one</code> — одно предложение, "
            "<code>/level short</code> — кратко, <code>/level detail</code> — подробно.",
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    if data.startswith("lvl:"):
        key = data.split(":", 1)[1]
        if key in config.LEVELS:
            set_level(context, key)
            await q.answer(f"Уровень: {config.LEVELS[key][0]}")
            await q.edit_message_text(
                f"✅ Уровень краткости: {config.LEVELS[key][0]}. Теперь кидай контент!"
            )
        else:
            await q.answer("Неизвестный уровень")
    else:
        await q.answer()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or ""

    # ссылка?
    urls = URL_RE.findall(text)
    if urls:
        url = urls[0]
        progress_id = await notify_working(message, f"🔗 Скачиваю статью: {url}")
        try:
            page_text = await fetch_url(url)
        except Exception as e:
            log.warning("fetch_url failed: %s", e)
            fallback = (
                "😕 Не получилось прочитать ссылку "
                f"({str(e)[:120] or e.__class__.__name__}). "
                "Скорее всего сайт закрыт от роботов (YouTube, соцсети, платные статьи). "
                "Попробуй скопировать текст и прислать сообщением."
            )
            await edit_or_delete(context, message.chat_id, progress_id, fallback)
            return
        await process_text(
            update, context, page_text, label=" статью по ссылке",
            progress_id=progress_id,
        )
        return

    # пересланное сообщение без текста — достаём текст из origin
    if not text.strip():
        fo = getattr(message, "forward_origin", None)
        fwd_text = getattr(fo, "text", None) if fo is not None else None
        if fwd_text and str(fwd_text).strip():
            await process_text(update, context, str(fwd_text), label=" пересылку")
            return

    # обычный текст (в т.ч. reply цитатой — берём из entity quote)
    quoted = getattr(message, "quote", None)
    source = (quoted.text if quoted and quoted.text else text)
    await process_text(update, context, source)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    voice = message.voice or message.audio
    if not voice:
        return
    size = getattr(voice, "file_size", 0) or 0
    if size > config.MAX_VOICE_BYTES:
        await reply_html(message, "😅 Файл больше 25 МБ — Telegram-боты такие не потянут.")
        return

    target = tu.get_chat_kwargs(message)
    duration = getattr(voice, "duration", 0) or 0
    progress_id = await notify_working(
        message, f"🎙 Расшифровываю ({duration} сек)… это займёт немного времени."
    )
    try:
        data = await tu.download_file(context, voice.file_id)
        filename = "voice.ogg" if message.voice else (
            message.audio.file_name or "audio.mp3"
        )
        transcript = await summarizer.transcribe(data, filename=filename)
    except Exception as e:
        log.exception("transcribe failed")
        await edit_or_delete(
            context, message.chat_id, progress_id,
            f"💥 Не удалось расшифровать аудио: {str(e)[:150]}",
        )
        return

    if not transcript:
        await edit_or_delete(context, message.chat_id, progress_id,
                             "🤐 В аудио не распознал ни слова (тишина или шум).")
        return

    await edit_or_delete(context, message.chat_id, progress_id, None)
    note = (
        f"📝 <b>Расшифровка</b> ({duration} сек):\n"
        f"<blockquote>{html.escape(transcript[:1500])}</blockquote>\n\n"
    )
    await tu.send_long(context, target, note, reply_to=message.message_id)
    # process_text создаст своё «Читаю…» поверх уже отправленной расшифровки
    await process_text(update, context, transcript, voice_hint=True, label=" голосовое")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    doc = message.document
    if not doc:
        return
    if (doc.file_size or 0) > config.MAX_FILE_BYTES:
        await reply_html(message, "😅 Файл больше 20 МБ — не смогу скачать.")
        return

    progress_id = await notify_working(message, f"📑 Открываю «{doc.file_name or 'документ'}»…")
    try:
        data = await tu.download_file(context, doc.file_id)
        text = extract.extract_text(doc.file_name or "doc.txt", data)
    except (ValueError, RuntimeError) as e:
        await edit_or_delete(context, message.chat_id, progress_id, f"😅 {e}")
        return
    except Exception as e:
        log.exception("document processing failed")
        await edit_or_delete(
            context, message.chat_id, progress_id,
            f"💥 Ошибка при обработке файла: {str(e)[:150]}",
        )
        return

    await edit_or_delete(context, message.chat_id, progress_id, None)
    await process_text(update, context, text, label=f" документ «{doc.file_name or ''}»")


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_html(
        update.effective_message,
        "🤷 Такую штуку я не умею. Кидай текст, ссылку, голосовое или PDF/DOCX/TXT.",
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "💥 Упс, внутренняя ошибка. Попробуй ещё раз чуть позже."
            )
        except Exception:
            pass


# ---------- сборка приложения ----------

def build_application(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("level", cmd_level))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^lvl:"))

    app.add_handler(Message(filters.VOICE | filters.AUDIO, block=True), on_voice)
    app.add_handler(Message(filters.Document.ALL, block=True), on_document)
    app.add_handler(Message(filters.TEXT & ~filters.COMMAND, block=True), on_text)
    app.add_handler(
        Message(
            filters.PHOTO | filters.Story.ALL | filters.Contact.USER
            | filters.VIDEO | filters.VideoNote.ALL,
            block=True,
        ),
        on_unsupported,
    )

    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.BOT_TOKEN:
        raise SystemExit("❌ Переменная окружения BOT_TOKEN не задана.")
    if not config.OPENAI_API_KEY:
        raise SystemExit("❌ Переменная окружения OPENAI_API_KEY не задана.")

    app = build_application(config.BOT_TOKEN)
    log.info("TL;DR bot starting (model=%s)", config.LLM_MODEL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
