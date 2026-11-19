"""Sotto — невидимый ИИ-суфлёр для собеседований (Linux/Hyprland и Windows)."""

import os as _os


def _localhost_off_proxy() -> None:
    """localhost — всегда мимо прокси.

    Системный прокси Windows (VPN) отвечает даже на закрытые порты 127.0.0.1, и тогда проверка
    «демон уже запущен?» получает чужой ответ вместо ошибки соединения, а ctl/события/оверлей
    ломаются. Внешние запросы (OpenRouter, DeepSeek) через прокси ходить должны — их не трогаем.
    """
    existing = _os.environ.get("NO_PROXY") or _os.environ.get("no_proxy") or ""
    hosts = [h.strip() for h in existing.split(",") if h.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in hosts:
            hosts.append(host)
    _os.environ["NO_PROXY"] = ",".join(hosts)


_localhost_off_proxy()

__version__ = "0.1.0"
