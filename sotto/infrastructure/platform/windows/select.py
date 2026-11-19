"""Выбор источника системного звука, когда устройств вывода несколько.

На Windows звук приложения играет в конкретное устройство вывода, и оно не обязано совпадать
с устройством по умолчанию: Discord, Zoom и браузер выбирают его сами (а ещё есть виртуальные
кабели вроде NVIDIA Broadcast). Поэтому loopback пишется со всех выходов сразу, а слушателю
отдаются блоки того устройства, где сейчас громче.

Класс не знает ничего про звуковые API — только наблюдения вида «от устройства X пришёл блок
с такой громкостью», поэтому проверяется тестом без настоящего железа.
"""

from __future__ import annotations

import threading

import numpy as np

# Громкость (RMS), выше которой считаем, что в устройстве что-то играет. -80 дБ.
LOUD = 1e-4
# Сколько держим выбранное устройство, если звук стих: тишина между словами не должна
# переключать канал на другое устройство.
HOLD_S = 1.0
# Наблюдение старше этого возраста уже не считается — устройство могло отвалиться.
FRESH_S = 0.5
# Переключаемся на другое устройство, только если там заметно громче (×1.8 ≈ +5 дБ):
# иначе два устройства с похожим сигналом «перетягивали» бы канал каждый блок.
SWITCH_GAIN = 1.8


class LoudestPicker:
    """Пишет наблюдения по каждому устройству и выбирает, чьи блоки отдавать."""

    def __init__(self, hold_s: float = HOLD_S, loud: float = LOUD, fresh_s: float = FRESH_S,
                 switch_gain: float = SWITCH_GAIN):
        self.hold_s = hold_s
        self.loud = loud
        self.fresh_s = fresh_s
        self.switch_gain = switch_gain
        self._latest: dict[str, tuple[float, float, np.ndarray]] = {}  # id → (время, RMS, блок)
        self._active: str | None = None
        self._active_until = 0.0
        self._lock = threading.Lock()

    @property
    def active(self) -> str | None:
        return self._active

    def observe(self, device_id: str, rms: float, block: np.ndarray, ts: float) -> None:
        with self._lock:
            self._latest[device_id] = (float(ts), float(rms), block)

    def forget(self, device_id: str) -> None:
        with self._lock:
            self._latest.pop(device_id, None)
            if self._active == device_id:
                self._active = None

    def pick(self, now: float) -> tuple[str, np.ndarray, float] | None:
        """(id устройства, блок, когда он записан) — или None, если данных нет вовсе."""
        with self._lock:
            fresh = {dev: value for dev, value in self._latest.items() if now - value[0] <= self.fresh_s}
            if not fresh:
                return None
            loudest = max(fresh, key=lambda dev: fresh[dev][1])
            current = fresh.get(self._active) if self._active else None
            if current is not None and current[1] >= self.loud and loudest != self._active:
                # выбранное устройство играет — уступаем, только если в другом явно громче
                if fresh[loudest][1] < current[1] * self.switch_gain:
                    loudest = self._active
            if fresh[loudest][1] >= self.loud:  # кто-то играет — слушаем его
                self._active = loudest
                self._active_until = now + self.hold_s
            elif self._active not in fresh:  # выбранное устройство замолчало совсем
                self._active = max(fresh, key=lambda dev: fresh[dev][0])
                self._active_until = now + self.hold_s
            ts, _rms, block = fresh[self._active]
            return self._active, block, ts
