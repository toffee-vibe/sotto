"""`sotto shot` — снимок экрана или выделенной области.

Отдельный процесс: Qt-цикл не должен жить внутри демона. Демон запускает его, когда для подсказки
нужен экран (хоткей «ответ по скриншоту / по выделенной области»).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ... import paths
from ...infrastructure import platform as platform_mod


def default_dest() -> Path:
    return paths.SCREENSHOTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.jpg"


def main(out: str = "", region: bool = False) -> int:
    dest = Path(out) if out else default_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from ...infrastructure.platform.windows import winsys
        winsys.set_dpi_awareness()
        if not region:  # снимок всего экрана — без окна выделения
            return 0 if winsys.screenshot(dest) else 1
        from ..region import select_region
        rect = select_region()
        if rect is None:  # человек нажал Esc — подсказку по скриншоту не делаем
            return 1
        winsys.grab_rect(*rect).convert("RGB").save(dest, "JPEG", quality=85)
        return 0
    shot = platform_mod.current().screenshot(dest, None, region)
    return 0 if shot else 1
