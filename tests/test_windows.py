"""Windows-платформа: чистая логика без железа — выбор источника звука, разбор хоткеев,
выделение области (Qt, offscreen). Реализация адаптирована из PR «windows-11-support»."""

import sys

import numpy as np
import pytest

from sotto.infrastructure.platform.windows import hotkeys
from sotto.infrastructure.platform.windows.select import LoudestPicker


def block(value: float = 0.2) -> np.ndarray:
    return np.full(512, value, dtype=np.float32)


# --- LoudestPicker: Discord играет в наушники, а устройство по умолчанию — колонки ---

def test_silence_returns_nothing():
    assert LoudestPicker().pick(now=100.0) is None


def test_picks_the_loud_device():
    picker = LoudestPicker()
    picker.observe("quiet", rms=0.0, block=block(0.0), ts=100.0)
    picker.observe("discord", rms=0.2, block=block(0.2), ts=100.0)
    device, picked, ts = picker.pick(now=100.0)
    assert device == "discord"
    assert float(picked[0]) == pytest.approx(0.2)
    assert ts == 100.0


def test_holds_device_through_pauses_between_words():
    picker = LoudestPicker(hold_s=1.0, loud=1e-4)
    picker.observe("discord", rms=0.2, block=block(0.2), ts=100.0)
    picker.observe("music", rms=0.0, block=block(0.0), ts=100.0)
    assert picker.pick(now=100.0)[0] == "discord"
    for step in range(1, 9):  # в Discord пауза, во втором тишина — канал не переключаем
        now = 100.0 + step * 0.03
        picker.observe("discord", rms=0.0, block=block(0.0), ts=now)
        picker.observe("music", rms=0.0, block=block(0.0), ts=now)
        assert picker.pick(now=now)[0] == "discord"


def test_switches_when_other_device_starts_playing():
    picker = LoudestPicker(hold_s=1.0)
    picker.observe("discord", rms=0.0, block=block(0.0), ts=100.0)
    picker.observe("browser", rms=0.0, block=block(0.0), ts=100.0)
    picker.pick(now=100.0)
    picker.observe("discord", rms=0.0, block=block(0.0), ts=101.0)
    picker.observe("browser", rms=0.3, block=block(0.3), ts=101.0)
    assert picker.pick(now=101.0)[0] == "browser"


def test_stale_device_is_ignored():
    picker = LoudestPicker(fresh_s=0.5)
    picker.observe("gone", rms=0.5, block=block(0.5), ts=100.0)   # устройство отвалилось
    picker.observe("live", rms=0.01, block=block(0.01), ts=101.0)
    device, picked, _ts = picker.pick(now=101.0)
    assert device == "live"
    assert float(picked[0]) == pytest.approx(0.01)


def test_forget_drops_device():
    picker = LoudestPicker()
    picker.observe("usb", rms=0.3, block=block(0.3), ts=100.0)
    assert picker.pick(now=100.0)[0] == "usb"
    picker.forget("usb")
    assert picker.pick(now=100.0) is None


def test_does_not_flap_between_similar_devices():
    """Два устройства играют почти одинаково (например, зеркало) — канал не должен дёргаться."""
    picker = LoudestPicker(switch_gain=1.8)
    picker.observe("a", rms=0.10, block=block(0.10), ts=100.0)
    picker.observe("b", rms=0.11, block=block(0.11), ts=100.0)
    chosen = picker.pick(now=100.0)[0]
    for step in range(1, 20):
        now = 100.0 + step * 0.032
        picker.observe("a", rms=0.10 + (step % 2) * 0.01, block=block(0.1), ts=now)
        picker.observe("b", rms=0.11 - (step % 2) * 0.01, block=block(0.1), ts=now)
        assert picker.pick(now=now)[0] == chosen


def test_switches_when_other_is_clearly_louder():
    picker = LoudestPicker(switch_gain=1.8)
    picker.observe("discord", rms=0.05, block=block(0.05), ts=100.0)
    picker.observe("browser", rms=0.001, block=block(0.001), ts=100.0)
    assert picker.pick(now=100.0)[0] == "discord"
    picker.observe("discord", rms=0.05, block=block(0.05), ts=100.1)
    picker.observe("browser", rms=0.30, block=block(0.30), ts=100.1)
    assert picker.pick(now=100.1)[0] == "browser"


# --- разбор хоткеев ---

def test_parse_hotkey():
    mods, vk = hotkeys.parse_hotkey("CTRL + ALT + A")
    assert mods & hotkeys.MOD_CONTROL and mods & hotkeys.MOD_ALT
    assert vk == ord("A")
    assert hotkeys.parse_hotkey("SUPER + SHIFT + F5") == (hotkeys.MOD_WIN | hotkeys.MOD_SHIFT
                                                          | hotkeys.MOD_NOREPEAT, 0x74)
    assert hotkeys.parse_hotkey("") is None
    assert hotkeys.parse_hotkey("SUPER + NONSENSE") is None
    assert hotkeys.parse_hotkey("CTRL + ALT + DEL") is None  # DEL не в таблице и не буква


# --- выделение области (Qt, offscreen) ---

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sotto.interfaces.region import MIN_SIDE, RegionSelector  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv[:1])


def drag(selector, start: QPoint, end: QPoint) -> None:
    QTest.mousePress(selector, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(selector, end)
    QTest.mouseRelease(selector, Qt.MouseButton.LeftButton, pos=end)


def test_selects_rectangle(app):
    selector = RegionSelector(QRect(0, 0, 1200, 800))
    selector.show()
    QTest.qWaitForWindowExposed(selector)
    drag(selector, QPoint(100, 120), QPoint(400, 320))
    assert selector.selection == (100, 120, 301, 201)


def test_tiny_selection_is_cancelled(app):
    selector = RegionSelector(QRect(0, 0, 1200, 800))
    selector.show()
    QTest.qWaitForWindowExposed(selector)
    drag(selector, QPoint(10, 10), QPoint(10 + MIN_SIDE - 2, 10 + MIN_SIDE - 2))
    assert selector.selection is None


def test_escape_cancels(app):
    selector = RegionSelector(QRect(0, 0, 1200, 800))
    selector.show()
    QTest.qWaitForWindowExposed(selector)
    QTest.keyClick(selector, Qt.Key.Key_Escape)
    assert selector.selection is None
    assert not selector.isVisible()


def test_negative_coordinates_of_left_monitor(app):
    selector = RegionSelector(QRect(-1920, 0, 1920, 1080))
    selector.show()
    QTest.qWaitForWindowExposed(selector)
    drag(selector, QPoint(50, 60), QPoint(250, 260))
    assert selector.selection == (-1870, 60, 201, 201)
