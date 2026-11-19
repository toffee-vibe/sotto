"""Telegram: чтение каналов с вакансиями и отправка отклика по клику — от твоего аккаунта.

Как не получить бан/ограничение («спамблок»):
* читаем только каналы из списка и редкий глобальный поиск (не больше 5 запросов за запуск, паузы);
* соблюдаем FloodWait от Telegram — ждём или прекращаем, не долбим;
* пишем только тем, кто в посте сам указал «пишите @…»; одному человеку — один раз;
* не больше `daily_send_limit` сообщений в сутки и не чаще раза в `min_send_interval_s`;
* текст у каждого получателя свой (должность/компания), никаких массовых одинаковых рассылок.
Нужны api_id/api_hash: https://my.telegram.org → API development tools.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
from collections.abc import Callable
from pathlib import Path

from ... import paths
from ...config import Telegram as TelegramCfg
from ...domain.jobs import RawPost

log = logging.getLogger(__name__)

SESSION = paths.JOBS_DIR / "telegram"


def _client(cfg: TelegramCfg):
    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise RuntimeError("Для вакансий из Telegram нужны зависимости: pip install sotto[jobs] "
                           "(в nix-окружении они уже есть)") from e
    if not cfg.api_id or not cfg.api_hash:
        raise RuntimeError("Не заданы api_id/api_hash Telegram (Вакансии → Настройки источников)")
    return TelegramClient(str(SESSION), cfg.api_id, cfg.api_hash, device_model="Sotto",
                          system_version="Windows" if sys.platform == "win32" else "Linux")


async def login(cfg: TelegramCfg) -> None:
    """Интерактивный вход в терминале (номер, код, пароль 2FA). Один раз."""
    client = _client(cfg)
    await client.start()
    me = await client.get_me()
    print(f"Вошли как {me.first_name} (@{me.username})")
    await client.disconnect()


def logged_in() -> bool:
    return Path(str(SESSION) + ".session").exists()


async def fetch(cfg: TelegramCfg, cursor: Callable[[str], str | None], queries: list[str],
                per_channel: int = 150) -> tuple[list[RawPost], dict[str, str]]:
    """Возвращает (сообщения, новые курсоры каналов). Курсоры сохраняет вызывающий — после разбора,
    чтобы при сбое агента сообщения не потерялись."""
    try:
        from telethon import errors, functions, types
    except ImportError as e:
        raise RuntimeError("Для вакансий из Telegram нужны зависимости: pip install sotto[jobs]") from e

    client = _client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram: сначала войди — sotto jobs login-telegram")
    posts: list[RawPost] = []
    cursors: dict[str, str] = {}
    try:
        # 1) Каналы и чаты из списка — с места, где остановились в прошлый раз.
        for ch in cfg.channels:
            key = f"tg_last:{ch}"
            last_id = int(cursor(key) or 0)
            try:
                entity = await client.get_entity(ch)
                newest = last_id
                async for m in client.iter_messages(entity, min_id=last_id, limit=per_channel):
                    newest = max(newest, m.id)
                    if not m.message:
                        continue
                    username = getattr(entity, "username", None)
                    posts.append(RawPost(
                        source="telegram", source_ref=f"{entity.id}:{m.id}", text=m.message,
                        url=f"https://t.me/{username}/{m.id}" if username else None,
                        posted_at=m.date.timestamp() if m.date else None))
                cursors[key] = str(newest)
            except errors.FloodWaitError as e:
                log.warning("Telegram просит подождать %s с — прекращаю чтение", e.seconds)
                break
            except (ValueError, errors.RPCError) as e:
                log.warning("Канал %s: %s", ch, e)
            await asyncio.sleep(random.uniform(2, 5))

        # 2) Немного глобального поиска по публичным каналам.
        for q in queries[:5]:
            try:
                res = await client(functions.messages.SearchGlobalRequest(
                    q=q, filter=types.InputMessagesFilterEmpty(), min_date=None, max_date=None,
                    offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0, limit=40,
                    broadcasts_only=True))
            except errors.FloodWaitError as e:
                log.warning("Глобальный поиск: подождать %s с — пропускаю", e.seconds)
                break
            except (errors.RPCError, TypeError) as e:
                log.warning("Глобальный поиск «%s»: %s", q, e)
                continue
            chats = {c.id: c for c in res.chats}
            for m in res.messages:
                if not getattr(m, "message", None):
                    continue
                peer_id = getattr(m.peer_id, "channel_id", None)
                chat = chats.get(peer_id)
                username = getattr(chat, "username", None)
                posts.append(RawPost(
                    source="telegram", source_ref=f"{peer_id}:{m.id}", text=m.message,
                    url=f"https://t.me/{username}/{m.id}" if username else None,
                    posted_at=m.date.timestamp() if m.date else None))
            await asyncio.sleep(random.uniform(8, 15))
    finally:
        await client.disconnect()
    return posts, cursors


async def send(cfg: TelegramCfg, target: str, text: str, attachment: Path | None) -> None:
    """Лимиты (сутки, интервал, повтор) проверяет сценарий до вызова — см. domain/jobs.check_send."""
    client = _client(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram: сначала войди — sotto jobs login-telegram")
        await client.send_message(target, text, link_preview=False)
        if attachment and attachment.exists():
            await asyncio.sleep(random.uniform(1.5, 3))
            await client.send_file(target, str(attachment))
    finally:
        await client.disconnect()
