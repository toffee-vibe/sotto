"""Репозитории: запись и чтение сущностей для сценариев (реализации портов из application/ports.py)."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from ...application.ports import (
    CallRepository,
    InterviewRepository,
    LegendRepository,
    Meta,
    RecordingRepository,
    TrainerRepository,
)
from ...domain.calls import TRAINING_APP, Call
from ...domain.companies import norm_company
from ...domain.interviews import Interview
from ...domain.legend import LegendItem
from ...domain.training import RAW_SOURCES
from .db import DB

_RAW = ", ".join(f"'{s}'" for s in RAW_SOURCES)
CURATED = f"source NOT IN ({_RAW})"   # темы тренера без сырья для агента


class Calls(CallRepository):
    def __init__(self, db: DB):
        self.db = db

    def get(self, call_id: int) -> Call | None:
        row = self.db.one("SELECT * FROM calls WHERE id = ?", [call_id])
        return Call.from_row(row) if row else None

    def start(self, app: str, record_only: bool = False, **values: Any) -> int:
        return self.db.insert("calls", started_at=values.pop("started_at", time.time()), app=app,
                              record_only=int(record_only), **values)

    def update(self, call_id: int, **values: Any) -> None:
        self.db.update("calls", call_id, **values)

    def delete(self, call_id: int) -> list[int]:
        """Удаляет звонок со всем содержимым; возвращает id его реплик (их надо убрать из поиска)."""
        utt = [u["id"] for u in self.db.all("SELECT id FROM utterances WHERE call_id = ?", [call_id])]
        self.db.execute("DELETE FROM calls WHERE id = ?", [call_id])
        return utt

    def set_company(self, call_id: int, company: str | None, interview_id: int | None = None) -> None:
        values: dict[str, Any] = {"company": (company or "").strip() or None}
        if interview_id is not None:
            values["interview_id"] = interview_id
        self.db.update("calls", call_id, **values)

    def unlink_interview(self, interview_id: int) -> None:
        self.db.execute("UPDATE calls SET interview_id = NULL WHERE interview_id = ?", [interview_id])

    def for_interview(self, iv: Interview) -> list[Call]:
        rows = self.db.all("""SELECT * FROM calls WHERE app IS NOT ?
                              AND (interview_id = ? OR lower(trim(company)) = ?) ORDER BY started_at""",
                           [TRAINING_APP, iv.id, iv.company_key])
        return [Call.from_row(r) for r in rows]

    def by_ids(self, ids: Iterable[int]) -> dict[int, Call]:
        ids = sorted({i for i in ids if i is not None})
        if not ids:
            return {}
        rows = self.db.all(f"SELECT * FROM calls WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: Call.from_row(r) for r in rows}

    def unfinished_since(self, since: float, min_utterances: int) -> list[int]:
        """Закончившиеся звонки без итогов (демон остановили сразу после звонка)."""
        return [r["id"] for r in self.db.all(
            """SELECT c.id FROM calls c WHERE c.ended_at IS NOT NULL AND c.ended_at > ?
               AND (c.title IS NULL OR c.title = '')
               AND (SELECT COUNT(*) FROM utterances u WHERE u.call_id = c.id) >= ?""", [since, min_utterances])]

    def transcript(self, call_id: int, since: float = 0.0) -> list[dict[str, Any]]:
        return self.db.all(
            """SELECT u.*, COALESCE(s.name, s.label) AS speaker, s.role AS speaker_role
               FROM utterances u LEFT JOIN speakers s ON s.id = u.speaker_id
               WHERE u.call_id = ? AND u.started_at >= ? ORDER BY u.started_at""", [call_id, since])

    def hints(self, call_id: int) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM answers WHERE call_id = ? ORDER BY created_at", [call_id])

    def add_utterance(self, **values: Any) -> int:
        return self.db.insert("utterances", **values)

    def recent_them(self, call_id: int, after: float, limit: int = 3) -> list[str]:
        return [r["text"] for r in self.db.all(
            "SELECT text FROM utterances WHERE call_id = ? AND channel = 'them' AND ended_at > ? "
            "ORDER BY id DESC LIMIT ?", [call_id, after, limit])]

    def add_answer(self, **values: Any) -> int:
        return self.db.insert("answers", **values)

    def add_speaker(self, call_id: int, label: str, is_me: bool = False, embedding: bytes | None = None,
                    name: str | None = None) -> int:
        return self.db.insert("speakers", call_id=call_id, label=label, is_me=int(is_me), embedding=embedding,
                              name=name)

    def speaker(self, speaker_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT id, call_id, label, COALESCE(name, label) AS display, name, role, is_me "
                           "FROM speakers WHERE id = ?", [speaker_id])

    def other_speakers(self, call_id: int) -> list[int]:
        return [r["id"] for r in self.db.all("SELECT id FROM speakers WHERE call_id = ? AND is_me = 0 ORDER BY id",
                                             [call_id])]

    def rename_speaker(self, speaker_id: int, name: str | None, role: str | None) -> None:
        self.db.update("speakers", speaker_id, name=name, role=role)

    def name_speaker(self, speaker_id: int, name: str) -> None:
        """Только имя: роль, которую ввели руками, не трогаем."""
        self.db.update("speakers", speaker_id, name=name)


class Legend(LegendRepository):
    def __init__(self, db: DB):
        self.db = db

    def all(self) -> list[LegendItem]:
        return [LegendItem.from_row(r) for r in self.db.all("SELECT * FROM legend ORDER BY created_at")]

    def add(self, claim: str, *, note: str | None = None, company: str | None = None,
            call_id: int | None = None, answer_id: int | None = None) -> int:
        return self.db.insert("legend", claim=claim, note=note, company=company, call_id=call_id,
                              answer_id=answer_id, created_at=time.time())

    def get(self, item_id: int) -> LegendItem | None:
        row = self.db.one("SELECT * FROM legend WHERE id = ?", [item_id])
        return LegendItem.from_row(row) if row else None

    def save(self, item: LegendItem) -> None:
        self.db.update("legend", item.id, claim=item.claim, note=item.note, active=item.active, company=item.company)

    def delete(self, ids: Iterable[int]) -> None:
        for i in ids:
            self.db.execute("DELETE FROM legend WHERE id = ?", [i])

    def move_call(self, call_id: int, company: str | None) -> None:
        self.db.execute("UPDATE legend SET company = ? WHERE call_id = ?", [(company or "").strip() or None, call_id])

    def delete_company(self, company: str) -> None:
        self.db.execute("DELETE FROM legend WHERE lower(trim(company)) = ?", [norm_company(company)])


class Interviews(InterviewRepository):
    def __init__(self, db: DB):
        self.db = db

    def get(self, iid: int) -> Interview | None:
        row = self.db.one("SELECT * FROM interviews WHERE id = ?", [iid])
        return Interview.from_row(row) if row else None

    def all(self) -> list[Interview]:
        return [Interview.from_row(r) for r in self.db.all(
            "SELECT * FROM interviews ORDER BY COALESCE(scheduled_at, created_at), id")]

    def add(self, iv: Interview) -> int:
        iv.id = self.db.insert("interviews", **iv.to_row())
        return iv.id

    def save(self, iv: Interview) -> None:
        iv.updated_at = time.time()
        self.db.update("interviews", iv.id, **iv.to_row())

    def delete(self, iid: int) -> None:
        self.db.execute("DELETE FROM interviews WHERE id = ?", [iid])


class Trainer(TrainerRepository):
    def __init__(self, db: DB):
        self.db = db

    def topics(self, kind: str, limit: int = 60) -> list[dict[str, Any]]:
        return self.db.all(f"""SELECT * FROM trainer_items WHERE kind = ? AND {CURATED}
                               ORDER BY due_at ASC, score ASC LIMIT ?""", [kind, limit])

    def raw_signals(self, kind: str, limit: int = 80) -> list[str]:
        return [r["item"] for r in self.db.all(
            f"""SELECT item FROM trainer_items WHERE kind = ? AND source IN ({_RAW})
                ORDER BY source = 'rejections' DESC, id DESC LIMIT ?""", [kind, limit])]

    def by_score(self, kind: str, weak: bool, limit: int) -> list[str]:
        cond = "score < 0.6 ORDER BY score" if weak else "score >= 0.8"
        return [r["item"] for r in self.db.all(
            f"SELECT item FROM trainer_items WHERE kind = ? AND seen > 0 AND {cond} LIMIT ?", [kind, limit])]

    def add_items(self, kind: str, items: Iterable[str], source: str, due_at: float, bump: bool = False) -> None:
        """bump — если пункт уже есть, поднять его в очереди (новое due_at)."""
        tail = "ON CONFLICT(kind, item) DO UPDATE SET due_at = excluded.due_at" if bump else "ON CONFLICT DO NOTHING"
        for item in items:
            self.db.execute(f"INSERT INTO trainer_items(kind, item, source, due_at) VALUES(?, ?, ?, ?) {tail}",
                            [kind, item, source, due_at])

    def replace_agent_topics(self, kind: str, topics: list[str], now: float) -> None:
        """Прошлый список агента, к которому ещё не приступали, заменяется новым — список не разрастается.
        Порядок агента = очерёдность."""
        self.db.execute("DELETE FROM trainer_items WHERE kind = ? AND source = 'agent' AND seen = 0", [kind])
        for i, topic in enumerate(topics):
            self.db.execute("INSERT OR IGNORE INTO trainer_items(kind, item, source, due_at) VALUES(?, ?, 'agent', ?)",
                            [kind, topic, now + i])

    def has_topics(self, kind: str) -> bool:
        return bool(self.db.one(f"SELECT 1 AS x FROM trainer_items WHERE kind = ? AND {CURATED} LIMIT 1", [kind]))

    def next_topic(self, kind: str) -> str | None:
        row = self.db.one(f"SELECT item FROM trainer_items WHERE kind = ? AND {CURATED} "
                          "ORDER BY due_at ASC, score ASC LIMIT 1", [kind])
        return row["item"] if row else None

    def item(self, kind: str, text: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM trainer_items WHERE kind = ? AND item = ?", [kind, text])

    def update_item(self, item_id: int, **values: Any) -> None:
        self.db.update("trainer_items", item_id, **values)

    def delete_item(self, item_id: int) -> None:
        self.db.execute("DELETE FROM trainer_items WHERE id = ?", [item_id])

    def start_session(self, kind: str, topic: str, now: float) -> int:
        return self.db.insert("trainer_sessions", kind=kind, topic=topic, started_at=now)

    def add_turn(self, session_id: int, role: str, text: str, now: float) -> int:
        return self.db.insert("trainer_turns", session_id=session_id, role=role, text=text, created_at=now)

    def set_last_feedback(self, session_id: int, feedback: str) -> None:
        self.db.execute("UPDATE trainer_turns SET feedback = ? WHERE id = (SELECT MAX(id) FROM trainer_turns "
                        "WHERE session_id = ? AND role = 'user')", [feedback, session_id])

    def finish_session(self, session_id: int, now: float, feedback: str | None) -> None:
        self.db.update("trainer_sessions", session_id, ended_at=now, feedback=feedback)


class Recordings(RecordingRepository):
    def __init__(self, db: DB):
        self.db = db

    def open(self, path: str, started_at: float, fps: int) -> int:
        return self.db.insert("recordings", path=path, started_at=started_at, fps=fps)

    def close(self, rec_id: int, path: str, ended_at: float, size: int) -> None:
        self.db.update("recordings", rec_id, path=path, ended_at=ended_at, size_bytes=size)

    def delete(self, rec_id: int) -> None:
        self.db.execute("DELETE FROM recordings WHERE id = ?", [rec_id])

    def get(self, rec_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM recordings WHERE id = ?", [rec_id])

    def unfinished(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM recordings WHERE ended_at IS NULL")


class Repositories(Meta):
    """Все репозитории одной базы — то, что получают сценарии."""

    def __init__(self, db: DB):
        self.db = db
        self.calls = Calls(db)
        self.legend = Legend(db)
        self.interviews = Interviews(db)
        self.trainer = Trainer(db)
        self.recordings = Recordings(db)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.db.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.db.set_meta(key, value)
