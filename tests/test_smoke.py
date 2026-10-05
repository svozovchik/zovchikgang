"""Смоук-тесты логики TL;DR бота без сети и реальных API."""
import asyncio
import os
import sys
import types
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import main as m  # noqa: E402
from bot import telegram_utils as tu  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeCtxBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)

    async def send_chat_action(self, **kw):
        pass

    async def delete_message(self, **kw):
        pass

    async def edit_message_text(self, **kw):
        self.sent.append(kw)


class FakeCtx:
    def __init__(self):
        self.chat_data = {}
        self.bot = FakeCtxBot()
        self.args = []


class FakeMsg:
    chat_id = 555
    message_id = 10
    text = None
    voice = None
    audio = None
    document = None
    forward_origin = None
    quote = None
    is_topic_message = False
    message_thread_id = None

    def __init__(self):
        self.replies = []

    async def reply_text(self, t, **kw):
        self.replies.append(t)

    async def reply_html(self, t, **kw):
        self.replies.append(t)


def make_update(msg):
    u = mock.Mock()
    u.effective_message = msg
    u.effective_chat = mock.Mock(id=msg.chat_id)
    return u


LONG = "Это очень длинная статья про котов, в которой много воды. " * 15


def test_split_message():
    parts = tu.split_message("a" * 300 + "\n\n" + "b" * 5000, limit=1000)
    assert all(len(p) <= 1000 for p in parts), [len(p) for p in parts]
    assert "".join(parts).count("b") == 5000
    assert tu.split_message("короткий текст") == ["короткий текст"]


def test_summarize_levels():
    async def fake_summarize(text, level_key="short", voice_hint=False):
        return f"[{level_key}|voice={voice_hint}] сжато"

    m.summarizer = types.SimpleNamespace(summarize=fake_summarize, transcribe=None)

    for lvl in ("one", "short", "detail"):
        ctx = FakeCtx()
        ctx.chat_data["level"] = lvl
        msg = FakeMsg(); msg.text = LONG
        run(m.on_text(make_update(msg), ctx))
        sent = ctx.bot.sent[-1]["text"]
        assert f"[{lvl}|voice=False]" in sent, sent
        assert "TL;DR" in sent


def test_too_short_rejected():
    async def fake_summarize(*a, **k):
        raise AssertionError("не должен вызываться")

    m.summarizer = types.SimpleNamespace(summarize=fake_summarize, transcribe=None)
    ctx = FakeCtx()
    msg = FakeMsg(); msg.text = "ок"
    run(m.on_text(make_update(msg), ctx))
    assert any("Слишком короткое" in r for r in msg.replies)


def test_voice_flow():
    async def fake_summarize(text, level_key="short", voice_hint=False):
        assert voice_hint is True
        return "3 минуты нытья про работу. Суть: бесит начальник."

    async def fake_transcribe(data, filename="voice.ogg"):
        return LONG

    m.summarizer = types.SimpleNamespace(summarize=fake_summarize, transcribe=fake_transcribe)

    async def fake_download(context, file_id):
        return b"ogg-bytes"

    orig_dl = tu.download_file
    tu.download_file = fake_download
    try:
        ctx = FakeCtx()
        msg = FakeMsg()
        msg.voice = mock.Mock(file_id="f", duration=180, file_size=1_000_000)
        msg.audio = None
        run(m.on_voice(make_update(msg), ctx))
        texts = [s.get("text", "") for s in ctx.bot.sent]
        assert any("Расшифровка" in t for t in texts), texts
        assert any("бесит начальник" in t for t in texts), texts
    finally:
        tu.download_file = orig_dl


def test_document_txt():
    async def fake_summarize(text, level_key="short", voice_hint=False):
        return "выжимка из файла"

    async def fake_download(context, file_id):
        return LONG.encode()

    m.summarizer = types.SimpleNamespace(summarize=fake_summarize, transcribe=None)
    orig_dl = tu.download_file
    tu.download_file = fake_download
    try:
        ctx = FakeCtx()
        msg = FakeMsg()
        msg.document = mock.Mock(file_id="f", file_name="article.txt", file_size=999)
        run(m.on_document(make_update(msg), ctx))
        assert any("выжимка из файла" in s.get("text", "") for s in ctx.bot.sent)
    finally:
        tu.download_file = orig_dl


def test_unsupported_format():
    from bot import extract
    try:
        extract.extract_text("movie.mp4", b"...")
        raise AssertionError("должен был быть ValueError")
    except ValueError as e:
        assert "не поддерживается" in str(e)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    sys.exit(1 if failed else 0)
