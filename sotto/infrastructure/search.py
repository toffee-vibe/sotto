"""Семантический поиск по звонкам, репликам и вакансиям.

Эмбеддинги считаются локально (fastembed, мультиязычная модель — русский и английский в одном
пространстве: запрос «расскажи про кафку» найдёт «tell me about Kafka»). Векторы лежат в той же
SQLite через sqlite-vec. Ничего не уходит наружу и ничего не стоит.
"""

from __future__ import annotations

import logging
import threading
from functools import cached_property
from typing import Any

import numpy as np

from ..application.ports import SearchIndex
from .sqlite.db import DB

log = logging.getLogger(__name__)

MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
DIM = 768


class Search(SearchIndex):
    def __init__(self, db: DB, model: str = MODEL, dim: int = DIM):
        self.db = db
        self.model_name = model
        self.dim = dim
        self._lock = threading.Lock()
        db.ensure_vectors(dim, model)

    @cached_property
    def _model(self):
        from fastembed import TextEmbedding

        from .. import paths
        return TextEmbedding(self.model_name, cache_dir=str(paths.MODELS_DIR / "fastembed"))

    def embed(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            vecs = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)

    # --- индексация ---

    def index(self, kind: str, ref_id: int, text: str) -> None:
        self.index_many([(kind, ref_id, text)])

    def index_many(self, items: list[tuple[str, int, str]]) -> None:
        items = [(k, r, t.strip()) for k, r, t in items if t and t.strip()]
        if not items or not self.db.vec_enabled:
            return
        vecs = self.embed([t for _, _, t in items])
        for (kind, ref_id, text), vec in zip(items, vecs, strict=True):
            old = self.db.one("SELECT id FROM search_items WHERE kind = ? AND ref_id = ?", [kind, ref_id])
            if old:
                self.db.execute("DELETE FROM vectors WHERE rowid = ?", [old["id"]])
                self.db.execute("DELETE FROM search_items WHERE id = ?", [old["id"]])
            item_id = self.db.insert("search_items", kind=kind, ref_id=ref_id, text=text)
            self.db.execute("INSERT INTO vectors(rowid, kind, embedding) VALUES (?, ?, ?)",
                            [item_id, kind, vec.tobytes()])

    def remove(self, kind: str, ref_ids: list[int]) -> None:
        for ref_id in ref_ids:
            old = self.db.one("SELECT id FROM search_items WHERE kind = ? AND ref_id = ?", [kind, ref_id])
            if old:
                self.db.execute("DELETE FROM vectors WHERE rowid = ?", [old["id"]])
                self.db.execute("DELETE FROM search_items WHERE id = ?", [old["id"]])

    def index_call(self, call_id: int) -> None:
        call = self.db.one("SELECT * FROM calls WHERE id = ?", [call_id])
        if not call:
            return
        items = [("call", call_id, f"{call.get('title') or ''}. {call.get('summary') or ''}")]
        for u in self.db.all("SELECT id, text FROM utterances WHERE call_id = ?", [call_id]):
            if len(u["text"]) >= 12:
                items.append(("utterance", u["id"], u["text"]))
        self.index_many(items)

    # --- поиск ---

    def query(self, text: str, kinds: tuple[str, ...] = ("call", "utterance"), limit: int = 30) -> list[dict[str, Any]]:
        if not text.strip():
            return []
        results: list[dict[str, Any]] = []
        if self.db.vec_enabled:
            vec = self.embed([text])[0]
            for kind in kinds:  # KNN внутри каждого вида, иначе тысячи реплик вытеснят вакансии
                results += self.db.all(
                    """SELECT s.kind, s.ref_id, s.text, v.distance
                       FROM (SELECT rowid, distance FROM vectors
                             WHERE embedding MATCH ? AND k = ? AND kind = ?) v
                       JOIN search_items s ON s.id = v.rowid""",
                    [vec.tobytes(), limit * 2, kind])
            results.sort(key=lambda r: r["distance"])
        # Точное совпадение слов (имена, названия компаний) — поднимаем наверх.
        like = self.db.all(
            "SELECT kind, ref_id, text, 0.0 AS distance FROM search_items WHERE text LIKE ? LIMIT ?",
            [f"%{text.strip()}%", limit])
        seen = {(r["kind"], r["ref_id"]) for r in like}
        merged = [r for r in like if r["kind"] in kinds] + [r for r in results if (r["kind"], r["ref_id"]) not in seen]
        return merged[:limit]

    def search_calls(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Результаты, сгруппированные по звонкам: звонок + лучшие совпавшие реплики."""
        hits = self.query(text, limit=limit * 4)
        calls: dict[int, dict[str, Any]] = {}
        for h in hits:
            if h["kind"] == "call":
                call_id = h["ref_id"]
            else:
                u = self.db.one("SELECT call_id, started_at FROM utterances WHERE id = ?", [h["ref_id"]])
                if not u:
                    continue
                call_id = u["call_id"]
                h = {**h, "started_at": u["started_at"]}
            entry = calls.setdefault(call_id, {"call_id": call_id, "score": h["distance"], "matches": []})
            entry["score"] = min(entry["score"], h["distance"])
            if h["kind"] == "utterance" and len(entry["matches"]) < 3:
                entry["matches"].append(h)
        out = []
        for entry in sorted(calls.values(), key=lambda e: e["score"])[:limit]:
            call = self.db.one("SELECT * FROM calls WHERE id = ?", [entry["call_id"]])
            if call:
                out.append({**entry, "call": call})
        return out
