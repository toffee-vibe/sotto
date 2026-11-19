"""Выделение области экрана мышью (Windows).

Отдельное окно на весь рабочий стол: фон — снимок экрана, поверх затемнение, тянем
прямоугольник мышью, отпускаем — область выбрана, Esc — отмена. Живёт в своём процессе
(`sotto shot --region`): Qt-цикл не должен работать внутри демона.
"""

from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

MIN_SIDE = 8  # px: меньше — это уже случайный щелчок, а не выделение


def desktop_rect() -> QRect:
    """Прямоугольник, покрывающий все мониторы (в координатах Qt)."""
    rect = QRect()
    for screen in QGuiApplication.screens():
        rect = rect.united(screen.geometry())
    return rect


def pillow_to_pixmap(image: Any) -> QPixmap:
    """Pillow Image → QPixmap без временных файлов."""
    from PySide6.QtGui import QImage

    if image.mode != "RGB":
        image = image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())  # копия: данные Python-объекта живут не дольше вызова


class RegionSelector(QWidget):
    """Окно выделения области. `selection` — выбранный прямоугольник в координатах экрана (или None)."""

    def __init__(self, virtual: QRect, background: QPixmap | None = None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setGeometry(virtual)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Sotto: выделите область")
        self.virtual = QRect(virtual)
        self.background = background
        self.selection: tuple[int, int, int, int] | None = None
        self._start: QPoint | None = None
        self._band = QRect()

    # --- отрисовка ---

    def paintEvent(self, _event) -> None:  # noqa: N802 — имя метода Qt
        painter = QPainter(self)
        if self.background is not None:
            painter.drawPixmap(self.rect(), self.background)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._band.isValid():
            band = self._band
            if self.background is not None:
                painter.drawPixmap(band, self.background, QRect(band).translated(-self.virtual.topLeft()))
            painter.setPen(QPen(QColor(230, 228, 223), 1))
            painter.drawRect(band.adjusted(0, 0, -1, -1))

    # --- мышь и клавиши ---

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._start = event.position().toPoint()
        self._band = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._start is not None:
            self._band = QRect(self._start, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        band = QRect(self._start, event.position().toPoint()).normalized()
        self._start = None
        if band.width() >= MIN_SIDE and band.height() >= MIN_SIDE:
            self.selection = (band.x() + self.virtual.x(), band.y() + self.virtual.y(),
                              band.width(), band.height())
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()


def select_region() -> tuple[int, int, int, int] | None:
    """Показать окно выделения и вернуть (left, top, width, height) в координатах экрана."""
    app = QApplication.instance() or QApplication(sys.argv)
    virtual = desktop_rect()
    background = None
    try:  # фон — снимок рабочего стола; без него выделять тоже можно
        from ..infrastructure.platform.windows import winsys
        background = pillow_to_pixmap(winsys.grab_rect(virtual.x(), virtual.y(),
                                                       virtual.width(), virtual.height()))
    except Exception:  # noqa: BLE001
        background = None
    selector = RegionSelector(virtual, background)
    selector.show()
    selector.raise_()
    selector.activateWindow()
    app.exec()
    return selector.selection
