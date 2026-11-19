"""Отклик письмом — с твоего ящика, по одному письму, без массовых рассылок.

Чтобы не попадать в спам: свой реальный ящик (SPF/DKIM настроены у провайдера), обычный текст без
трекинга и ссылок-сокращалок, понятная тема, одно вложение PDF, один получатель, суточный лимит.
Для Gmail/Яндекса нужен «пароль приложения».
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from ...config import Email as EmailCfg


def send(cfg: EmailCfg, to: str, subject: str, body: str, attachment: Path | None) -> None:
    """Лимиты проверяет сценарий до вызова — см. domain/jobs.check_send."""
    if not cfg.smtp_host or not cfg.username:
        raise RuntimeError("Почта не настроена (Вакансии → Настройки источников)")

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name, cfg.username)) if cfg.from_name else cfg.username
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.username.split("@")[-1])
    msg.set_content(body)
    if attachment and attachment.exists():
        msg.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)

    ctx = ssl.create_default_context()
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=30) as s:
            s.login(cfg.username, cfg.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg.username, cfg.password)
            s.send_message(msg)
