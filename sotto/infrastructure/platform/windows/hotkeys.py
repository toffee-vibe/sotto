"""Разбор строк хоткеев («SUPER + SHIFT + X») в модификаторы и виртуальный код клавиши Windows.

Чистый модуль без ctypes.windll — импортируется и тестируется на любой ОС.
Регистрация (RegisterHotKey и цикл сообщений) — в winsys.py.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000

_KEY_NAMES = {
    "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "PAUSE": 0x13, "PRINTSCREEN": 0x2C, "CAPSLOCK": 0x14,
}

_MOD_NAMES = {
    "SUPER": MOD_WIN, "WIN": MOD_WIN, "META": MOD_WIN, "CMD": MOD_WIN, "LOGO": MOD_WIN,
    "CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL, "STRG": MOD_CONTROL,
    "ALT": MOD_ALT, "OPTION": MOD_ALT,
    "SHIFT": MOD_SHIFT,
}


def parse_hotkey(keys: str) -> tuple[int, int] | None:
    """«SUPER + SHIFT + X» → (модификаторы, виртуальный код клавиши). None — не разобрали."""
    parts = [p.strip().upper() for p in keys.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for part in parts[:-1]:
        if part not in _MOD_NAMES:
            log.warning("Неизвестный модификатор в хоткее «%s»: %s", keys, part)
            return None
        mods |= _MOD_NAMES[part]
    key = parts[-1]
    if key in _KEY_NAMES:
        vk = _KEY_NAMES[key]
    elif len(key) == 1 and (key.isalnum() or key in "`-=[]\\;',./"):
        vk = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        # Последний шанс — спросить у Windows (например, знаки национальных раскладок).
        try:
            import ctypes
            scanned = ctypes.windll.user32.VkKeyScanW(ord(key[0]) if key else 0)
        except (AttributeError, OSError, ImportError):
            scanned = -1
        if scanned == -1:
            log.warning("Неизвестная клавиша в хоткее «%s»: %s", keys, key)
            return None
        vk = scanned & 0xFF
    return mods | MOD_NOREPEAT, vk
