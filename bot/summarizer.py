"""Ядро суммаризации: обращение к LLM через OpenAI-совместимый API."""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from . import config

log = logging.getLogger("tldr.summarizer")

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    return _client


def _build_prompt(text: str, level_key: str, voice_hint: bool = False) -> tuple[str, str]:
    _, level_instr = config.LEVELS[level_key]
    system = (
        "Ты — мастер коротких пересказов «TL;DR». Умеешь сжимать статьи, посты, новости, "
        "документы и расшифровки голосовых сообщений до самой сути. " + config.STYLE_NOTES
    )
    intro = ""
    if voice_hint:
        intro = (
            "Это расшифровка голосового сообщения. Определи по тону и содержанию, "
            "что это было (например: «3 минуты нытья про работу», «рабочая задача», "
            "«новость»), начни ответ с одной такой характеризующей фразы, "
            "а затем дай суть.\n\n"
        )
    user = (
        f"Задание: {level_instr}.\n\n{intro}"
        f"--- НАЧАЛО ИСХОДНИКА ---\n{text[:config.MAX_TEXT_CHARS]}\n--- КОНЕЦ ИСХОДНИКА ---"
    )
    return system, user


async def summarize(text: str, level_key: str = "short", voice_hint: bool = False) -> str:
    """Вернуть выжимку текста на выбранном уровне краткости."""
    system, user = _build_prompt(text, level_key, voice_hint)
    resp = await get_client().chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
        max_tokens=1200,
    )
    out = (resp.choices[0].message.content or "").strip()
    if not out:
        raise RuntimeError("LLM вернула пустой ответ")
    log.info("summarized %d chars -> %d chars (level=%s)", len(text), len(out), level_key)
    return out


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровать аудио (голосовое/аудиофайл) через Whisper."""
    resp = await get_client().audio.transcriptions.create(
        model=config.WHISPER_MODEL,
        file=(filename, audio_bytes),
    )
    return (getattr(resp, "text", "") or "").strip()
