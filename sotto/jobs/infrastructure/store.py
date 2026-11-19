"""Хранилище вакансий, кластеров, поисковых запросов и откликов (запись и чтение для страниц)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

import numpy as np

from ...infrastructure.sqlite.db import DB


class JobsStore:
    def __init__(self, db: DB):
        self.db = db

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.db.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.db.set_meta(key, value)

    # --- вакансии ---

    def vacancy(self, vid: int) -> dict[str, Any] | None:
        v = self.db.one("SELECT * FROM vacancies WHERE id = ?", [vid])
        if v:
            v["contacts_list"] = json.loads(v["contacts"] or "[]")
        return v

    def known(self, source: str, ref: str) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM vacancies WHERE source = ? AND source_ref = ?", [source, ref]))

    def seen_text(self, text_hash: str) -> bool:
        return bool(self.db.get_meta(f"vh:{text_hash}"))

    def mark_seen(self, text_hash: str, ref: str) -> None:
        self.db.set_meta(f"vh:{text_hash}", ref)

    def add_vacancy(self, **values: Any) -> int:
        return self.db.insert("vacancies", found_at=time.time(), **values)

    def update_vacancy(self, vid: int, **values: Any) -> None:
        self.db.update("vacancies", vid, **values)

    def set_status(self, vid: int, status: str) -> None:
        self.db.update("vacancies", vid, status=status)

    def unclustered(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT id, title, text FROM vacancies WHERE cluster_id IS NULL AND status != 'hidden'")

    def mentions(self, word: str) -> int:
        return (self.db.one("SELECT COUNT(*) AS n FROM vacancies WHERE text LIKE ?", [f"%{word}%"]) or {"n": 0})["n"]

    def good_titles(self, limit: int = 40) -> list[str]:
        return [r["title"] or "" for r in self.db.all(
            "SELECT title FROM vacancies WHERE status != 'hidden' AND COALESCE(fit, 0.5) >= 0.6 "
            "ORDER BY found_at DESC LIMIT ?", [limit])]

    # --- кластеры ---

    def cluster(self, cid: int | None) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM clusters WHERE id = ?", [cid]) if cid else None

    def centroids(self) -> list[tuple[int, np.ndarray | None]]:
        return [(c["id"], np.frombuffer(c["centroid"], dtype=np.float32) if c["centroid"] else None)
                for c in self.db.all("SELECT id, centroid FROM clusters")]

    def add_cluster(self, name: str, description: str, centroid: np.ndarray) -> int:
        return self.db.insert("clusters", name=name[:120], description=description,
                              centroid=centroid.astype(np.float32).tobytes(), updated_at=time.time())

    def update_cluster(self, cid: int, **values: Any) -> None:
        if "centroid" in values and isinstance(values["centroid"], np.ndarray):
            values["centroid"] = values["centroid"].astype(np.float32).tobytes()
        self.db.update("clusters", cid, **values)

    def assign(self, vid: int, cid: int | None) -> None:
        self.db.update("vacancies", vid, cluster_id=cid)

    def cluster_vacancies(self, cid: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self.db.all("SELECT title, company, text, language FROM vacancies WHERE cluster_id = ? LIMIT ?",
                           [cid, limit])

    def cluster_ids(self) -> list[int]:
        return [c["id"] for c in self.db.all("SELECT id FROM clusters")]

    # --- поисковые запросы ---

    def add_queries(self, queries: Iterable[str], source: str) -> int:
        n = 0
        for q in queries:
            q = q.strip()
            if q:
                cur = self.db.execute("INSERT OR IGNORE INTO search_queries(query, source, created_at) VALUES(?, ?, ?)",
                                      [q, source, time.time()])
                n += cur.rowcount
        return n

    def active_queries(self) -> list[str]:
        return [r["query"] for r in self.db.all("SELECT query FROM search_queries WHERE active = 1 ORDER BY hits DESC, id")]

    def all_queries(self) -> list[str]:
        return [r["query"] for r in self.db.all("SELECT query FROM search_queries")]

    def set_hits(self, query: str, hits: int) -> None:
        self.db.execute("UPDATE search_queries SET hits = ? WHERE query = ?", [hits, query])

    def toggle_query(self, qid: int) -> None:
        self.db.execute("UPDATE search_queries SET active = 1 - active WHERE id = ?", [qid])

    # --- отклики ---

    def sent_since(self, channel: str, since: float) -> int:
        return (self.db.one("SELECT COUNT(*) AS n FROM outreach WHERE channel = ? AND status = 'sent' AND sent_at > ?",
                            [channel, since]) or {"n": 0})["n"]

    def last_sent(self, channel: str) -> float | None:
        return (self.db.one("SELECT MAX(sent_at) AS t FROM outreach WHERE channel = ? AND status = 'sent'",
                            [channel]) or {}).get("t")

    def already_sent(self, channel: str, target: str) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM outreach WHERE channel = ? AND lower(target) = lower(?) "
                                "AND status = 'sent'", [channel, target]))

    def log_outreach(self, vid: int, channel: str, target: str, text: str, status: str) -> None:
        self.db.insert("outreach", vacancy_id=vid, channel=channel, target=target, text=text, sent_at=time.time(),
                       status=status)

    # --- чтение для страниц ---

    def page(self, cluster: int | None, status: str, ids: list[int] | None) -> list[dict[str, Any]]:
        where, params = ["1=1"], []
        if cluster is not None:
            where.append("v.cluster_id = ?")
            params.append(cluster)
        if status == "active":
            where.append("v.status NOT IN ('hidden')")
        elif status:
            where.append("v.status = ?")
            params.append(status)
        if ids is not None:
            where.append(f"v.id IN ({','.join('?' * len(ids)) or 'NULL'})")
            params += ids
        rows = self.db.all(f"""SELECT v.*, c.name AS cluster_name FROM vacancies v
                               LEFT JOIN clusters c ON c.id = v.cluster_id WHERE {' AND '.join(where)}
                               ORDER BY COALESCE(v.fit, 0.5) DESC, v.found_at DESC LIMIT 300""", params)
        for v in rows:
            v["contacts_list"] = json.loads(v["contacts"] or "[]")
        return rows

    def clusters_with_counts(self) -> list[dict[str, Any]]:
        return self.db.all("""SELECT c.*, (SELECT COUNT(*) FROM vacancies v WHERE v.cluster_id = c.id) AS n
                              FROM clusters c ORDER BY n DESC""")

    def cluster_names(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT id, name FROM clusters ORDER BY name")

    def queries_list(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM search_queries ORDER BY active DESC, hits DESC, id")

    def outreach(self, vid: int) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM outreach WHERE vacancy_id = ? ORDER BY sent_at DESC", [vid])
