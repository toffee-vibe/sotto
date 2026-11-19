"""Оверлей с подсказками (Qt). Отдельный процесс: слушает события демона и рисует.

Задача окна — не отвлекать. Спокойные цвета, ничего не мигает и не меняет размер:
  • строка состояния — мелко и тускло: звонок, кто собеседник (имя из представления);
  • подсказка — главное, обычным текстом; над ней тускло вопрос, на который она отвечает.
    Шрифт сам уменьшается, чтобы подсказка влезла целиком, — прокручивать не нужно;
  • последние реплики разговора — внизу, тускло.

Окно прозрачно для мыши: курсор и клики проходят к окну под ним, курсор не меняет форму — в демонстрации
экрана по курсору нельзя догадаться, что поверх что-то есть. Чтобы передвинуть или растянуть окно, включается
режим настройки (хоткей overlay_edit, по умолчанию SUPER+SHIFT+O, или кнопка на сайте): окно ловит мышь,
двигается за любое место и тянется за края; через 20 с без движения режим выключается сам. Демон запоминает
положение. Фокус окно не берёт, из демонстрации экрана скрыто правилом Hyprland `no_screen_share`
(класс окна `sotto-overlay`).
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import time

import httpx
import markdown as md
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser, QVBoxLayout, QWidget

from .. import config as config_mod
from ..infrastructure.platform.linux.hyprland import OVERLAY_CLASS

TEXT = "#e6e4df"
DIM = "#8d8b85"
CSS = f"""
body {{ color: {TEXT}; }}
p {{ margin: 0 0 6px 0; line-height: 135%; }}
ul {{ margin: 0 0 6px 0; -qt-list-indent: 1; }}
li {{ margin-bottom: 3px; line-height: 135%; }}
b, strong {{ font-weight: normal; }}
code, pre {{ font-family: monospace; background: rgba(255,255,255,0.06); }}
pre {{ padding: 6px; }}
.legend {{ color: #cbbfe0; }}
.q, .slot {{ color: {DIM}; }}
.who {{ color: #b5b2ab; }}
"""
EDGE = 10  # px от края, за которые окно тянется
EDIT_IDLE_MS = 20_000  # режим настройки выключается сам, если мышь не двигалась


class Events(QObject):
    event = Signal(dict)


def listen(base_url: str, events: Events) -> None:
    """Поток: держит SSE-подключение к демону и переподключается при обрыве."""
    while True:
        try:
            with httpx.stream("GET", f"{base_url}/api/events", timeout=None) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        events.event.emit(json.loads(line[6:]))
        except Exception:
            events.event.emit({"type": "disconnected"})
            time.sleep(1.5)


def render_answer(text: str) -> str:
    """Markdown → HTML; придуманные факты [[...]] чуть другим оттенком."""
    text = re.sub(r"\[\[(.+?)\]\]", r'<span class="legend">\1</span>', text, flags=re.S)
    text = text.replace("[[", "")  # незакрытая скобка во время стриминга
    return md.markdown(text, extensions=["fenced_code", "nl2br"])


class Overlay(QWidget):
    def __init__(self, cfg: config_mod.Config):
        super().__init__()
        self.cfg = cfg
        o = cfg.overlay
        self.setWindowTitle("Sotto")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        self.editing = False   # режим настройки: окно ловит мышь, чтобы его передвинули или растянули
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.timeout.connect(lambda: self.set_editing(False))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(o.width, o.height)

        self.setStyleSheet(f"""
            QWidget#root {{ background: rgba(24,24,26,{int(o.opacity * 255)}); border-radius: 10px;
                            border: 1px solid rgba(255,255,255,0.06); }}
            QLabel#status {{ color: {DIM}; }}
            QTextBrowser {{ background: transparent; border: none; color: {TEXT}; }}
            QTextBrowser#transcript {{ border-top: 1px solid rgba(255,255,255,0.06); padding-top: 4px; }}
        """)
        root = QWidget(self)
        root.setObjectName("root")
        root.setMouseTracking(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 10, 22, 10)
        lay.setSpacing(6)

        small = max(o.font_size - 4, 10)
        self.status = QLabel("Sotto: подключение…")
        self.status.setObjectName("status")
        self.status.setFont(QFont(QFont().family(), small))

        self.answer = QTextBrowser()
        self.answer.document().setDefaultStyleSheet(CSS)
        self.answer.document().setDefaultFont(QFont(QFont().family(), o.font_size))
        self.answer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.answer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.transcript = QTextBrowser()
        self.transcript.setObjectName("transcript")
        self.transcript.document().setDefaultStyleSheet(CSS)
        self.transcript.setFont(QFont(QFont().family(), small))
        self.transcript.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.transcript.setFixedHeight(int(small * 2.1 * o.transcript_lines) + 14)

        # Мышь проходит сквозь текст к окну: окно двигается за любое место.
        for w in (self.status, self.answer, self.answer.viewport(), self.transcript, self.transcript.viewport()):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        lay.addWidget(self.status)
        lay.addWidget(self.answer, 1)
        lay.addWidget(self.transcript)

        self.lines: list[tuple[str, int | None, str]] = []  # (канал, id собеседника, текст)
        self.speakers: dict[int, dict] = {}                 # id → {"name", "role"}
        self.slots: dict[str, dict] = {}   # slot → {"id", "text"}
        self.pending: dict[str, str] = {}  # slot → id ответа, который начался, но ещё без текста
        self.question = ""                 # вопрос, на который отвечает подсказка
        self._next_question = ""
        self.thinking = ""                 # «думаю…» пока ждём первые слова (только по хоткею)
        self.state: dict = {}
        self.notice = ""
        self._notice_until = 0.0
        self._dirty = True
        self.overflow = False
        timer = QTimer(self)
        timer.timeout.connect(self._refresh)  # перерисовка не чаще 20 раз в секунду
        timer.start(50)

    # --- мышь: перетаскивание, размер, колесо ---

    def _edges(self, pos: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if pos.x() < EDGE:
            edges |= Qt.Edge.LeftEdge
        if pos.x() > self.width() - EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() < EDGE:
            edges |= Qt.Edge.TopEdge
        if pos.y() > self.height() - EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def set_editing(self, on: bool) -> None:
        """Режим настройки. Вне его окно не получает мышь вовсе: Wayland отдаёт курсор окну под ним."""
        if on == self.editing:
            if on:
                self._edit_timer.start(EDIT_IDLE_MS)
            return
        self.editing = on
        handle = self.windowHandle()
        if handle is not None:
            # Флаг на уже открытом окне: Qt для Wayland меняет только область ввода, окно не пересоздаётся —
            # иначе Hyprland применил бы к «новому» окну правило с исходным положением и перетаскивание пропало бы.
            handle.setFlag(Qt.WindowType.WindowTransparentForInput, not on)
        else:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, not on)
        if not on:
            self.unsetCursor()
        if on:
            self._edit_timer.start(EDIT_IDLE_MS)
        else:
            self._edit_timer.stop()
        self._dirty = True

    def mousePressEvent(self, e) -> None:  # noqa: N802 — имя метода Qt
        if not self.editing or e.button() != Qt.MouseButton.LeftButton or self.windowHandle() is None:
            return
        edges = self._edges(e.position().toPoint())
        if edges:
            self.windowHandle().startSystemResize(edges)
        else:
            self.windowHandle().startSystemMove()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if not self.editing:
            return
        self._edit_timer.start(EDIT_IDLE_MS)
        edges = self._edges(e.position().toPoint())
        horiz = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vert = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horiz and vert:
            diag = edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
            shape = Qt.CursorShape.SizeFDiagCursor if diag else Qt.CursorShape.SizeBDiagCursor
        elif horiz:
            shape = Qt.CursorShape.SizeHorCursor
        elif vert:
            shape = Qt.CursorShape.SizeVerCursor
        else:
            shape = Qt.CursorShape.SizeAllCursor
        self.setCursor(QCursor(shape))

    def wheelEvent(self, e) -> None:  # noqa: N802 — на случай, если подсказка всё же не влезла
        if not self.editing:
            return
        self._edit_timer.start(EDIT_IDLE_MS)
        bar = self.answer.verticalScrollBar()
        bar.setValue(bar.value() - e.angleDelta().y())

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._dirty = True

    # --- события ---

    def on_event(self, e: dict) -> None:
        t = e.get("type")
        if t == "status":
            if e.get("call_id") != self.state.get("call_id"):
                self.speakers.clear()  # новый звонок — новые собеседники
            self.state = e
            self.setVisible(bool(e.get("overlay_visible", True)))
        elif t == "disconnected":
            self.state = {}
        elif t == "speaker":
            sp = self.speakers.setdefault(e["id"], {})
            if e.get("name") is not None:
                sp["name"] = e["name"] or None
                sp["role"] = e.get("role")
            elif e.get("label") and not sp.get("name"):
                sp["label"] = e["label"]
            self._render_transcript()
        elif t == "utterance":
            sid = e.get("speaker_id") if e["channel"] == "them" else None
            if sid is not None and e.get("speaker"):
                self.speakers.setdefault(sid, {}).setdefault("name", e["speaker"])
            self.lines.append((e["channel"], sid, e["text"]))
            self.lines = self.lines[-20:]
            self._render_transcript()
        elif t == "answer.start":
            self.pending[e["slot"]] = e["id"]
            self._next_question = e.get("question", "")
            if e["kind"] != "auto":  # авто-режим молчит, пока не ясно, нужен ли ответ
                self.thinking = {"screen": "смотрю на экран…", "questions": "подбираю вопросы…"}.get(e["kind"], "думаю…")
                self._dirty = True
        elif t == "answer.delta":
            slot = e["slot"]
            if self.pending.get(slot) == e["id"]:
                # Первый кусок нового ответа — только сейчас заменяем старую подсказку.
                self.pending.pop(slot)
                if slot == "main":
                    self.slots = {}
                else:  # режим dual: короткий и подробный живут рядом, обычный ответ убираем
                    self.slots.pop("main", None)
                self.slots[slot] = {"id": e["id"], "text": ""}
                self.question = self._next_question
                self.thinking = ""
            if slot in self.slots and self.slots[slot]["id"] == e["id"]:
                self.slots[slot]["text"] += e["text"]
                self._dirty = True
        elif t == "answer.done":
            if e["slot"] in self.slots and self.slots[e["slot"]]["id"] == e["id"]:
                self.slots[e["slot"]]["text"] = e["text"]
            self.thinking = ""
            self._dirty = True
        elif t in ("answer.skip", "answer.error"):
            self.pending.pop(e["slot"], None)
            if self.thinking:
                self.thinking = ""
                self._dirty = True
            if t == "answer.error":
                self._show_notice("ошибка: " + e["error"], 8)
        elif t == "notice":
            self._show_notice(e["text"], 4)
        elif t == "overlay.scroll_up":
            bar = self.answer.verticalScrollBar()
            bar.setValue(bar.value() - self.answer.height() // 2)
        elif t == "overlay.scroll_down":
            bar = self.answer.verticalScrollBar()
            bar.setValue(bar.value() + self.answer.height() // 2)
        elif t == "overlay.edit":
            self.set_editing(not self.editing)
        elif t == "overlay.clear":
            self.slots.clear()
            self.lines.clear()
            self.question = ""
            self._render_transcript()
            self._dirty = True

    def _show_notice(self, text: str, seconds: float) -> None:
        self.notice = text
        self._notice_until = time.time() + seconds

    # --- отрисовка ---

    def _who(self, channel: str, sid: int | None) -> str:
        if channel == "me":
            return "Я"
        sp = self.speakers.get(sid, {}) if sid is not None else {}
        return sp.get("name") or sp.get("label") or "Собеседник"

    def _render_transcript(self) -> None:
        n = self.cfg.overlay.transcript_lines
        rows = [f'<span class="who">{html.escape(self._who(ch, sid))}:</span> {html.escape(text)}'
                for ch, sid, text in self.lines[-n:]]
        self.transcript.setHtml(f'<div style="color:{DIM}">' + "<br>".join(rows) + "</div>")
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def answer_html(self) -> str:
        parts = []
        if self.question:
            q = self.question if len(self.question) < 140 else self.question[:137] + "…"
            parts.append(f'<p class="q"><small>{html.escape(q)}</small></p>')
        if len(self.slots) > 1:
            for k in ("short", "detailed"):
                if k in self.slots:
                    parts.append(f'<p class="slot"><small>{"кратко" if k == "short" else "подробно"}</small></p>')
                    parts.append(render_answer(self.slots[k]["text"]))
        else:
            parts += [render_answer(v["text"]) for v in self.slots.values()]
        if self.thinking:
            parts.append(f'<p class="q">{self.thinking}</p>')
        if not self.slots and not self.thinking:
            parts.append('<p class="q">Подсказка появится здесь, когда собеседник задаст вопрос.</p>')
        return "".join(parts)

    def _show_answer(self, body: str) -> None:
        """Шрифт один и тот же: подгонка размера под длину заставляла текст прыгать во время чтения.
        Если подсказка не влезла — в строке состояния появляется «колесо мыши — дальше»."""
        self.answer.setHtml(body)
        doc = self.answer.document()
        doc.setTextWidth(self.answer.viewport().width())
        self.overflow = doc.size().height() > self.answer.viewport().height() + 2

    def _status_text(self) -> str:
        if self.editing:
            return "Настройка: тащи окно, края — размер, колесо — прокрутка. Повтор хоткея — готово"
        s = self.state
        if not s:
            return "Sotto не запущен"
        parts = ["звонок" if s.get("in_call") else "нет звонка"]
        if s.get("mode") != "auto":
            parts.append("только по хоткею")
        if s.get("record_only"):
            parts.append("только запись")
        if not s.get("api_key"):
            parts.append("нет ключа OpenRouter")
        people = []
        for sp in self.speakers.values():
            if sp.get("name"):
                people.append(sp["name"] + (f", {sp['role']}" if sp.get("role") else ""))
        if people:
            parts.append(" · ".join(people))
        text = "  ·  ".join(parts)
        if self.overflow:
            text += "  ·  не влезло — прокрутка в режиме настройки"
        if self.notice and time.time() < self._notice_until:
            text += "  ·  " + self.notice
        return text

    def _refresh(self) -> None:
        status = self._status_text()
        if status != self.status.text():
            self.status.setText(status)
        if self._dirty:
            self._dirty = False
            self._show_answer(self.answer_html())


def main() -> None:
    cfg = config_mod.load()
    # Класс окна для правил Hyprland задаётся через app id (Wayland).
    QGuiApplication.setDesktopFileName(OVERLAY_CLASS)
    app = QApplication(sys.argv)
    app.setApplicationName(OVERLAY_CLASS)
    overlay = Overlay(cfg)
    events = Events()
    events.event.connect(overlay.on_event)
    base = f"http://{cfg.web.host}:{cfg.web.port}"
    threading.Thread(target=listen, args=(base, events), daemon=True).start()
    overlay.show()
    if sys.platform == "win32":
        # Пометить своё окно: демон найдёт его (даже через процесс-посредник venv-питона)
        # и исключит из демонстрации экрана.
        from ..infrastructure.platform.windows import winsys
        winsys.set_dpi_awareness()
        winsys.set_app_user_model_id()
        winsys.mark_overlay(int(overlay.winId()))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
