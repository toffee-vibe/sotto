"""Поиск и кластеры с поддельными эмбеддингами (без скачивания модели)."""

import time

import numpy as np

from sotto.infrastructure.search import Search
from sotto.infrastructure.sqlite.db import DB
from sotto.jobs.application.cluster import update_clusters
from sotto.jobs.infrastructure.store import JobsStore

TOPICS = {"kafka": 0, "postgres": 1, "frontend": 2, "react": 2, "go": 3, "golang": 3}


class FakeSearch(Search):
    def __init__(self, db):
        super().__init__(db, model="fake", dim=8)

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.full(8, 0.05, dtype=np.float32)
            for word, idx in TOPICS.items():
                if word in t.lower():
                    v[idx] += 1
            out.append(v / np.linalg.norm(v))
        return np.array(out)


class FakeAgents:
    async def run_json(self, prompt, **_):
        return {"name": "Группа", "description": "описание"}, "fake"


def test_semantic_search(isolated_dirs):
    db = DB()
    s = FakeSearch(db)
    call = db.insert("calls", started_at=time.time(), title="Собес про Kafka", summary="Спрашивали про kafka")
    u1 = db.insert("utterances", call_id=call, channel="them", started_at=time.time(), ended_at=time.time(),
                   text="Как работают consumer groups в Kafka?")
    db.insert("utterances", call_id=call, channel="them", started_at=time.time(), ended_at=time.time(),
              text="Расскажите про индексы в Postgres")
    s.index_call(call)
    hits = s.query("kafka партиции")
    assert hits and hits[0]["kind"] in ("call", "utterance")
    grouped = s.search_calls("kafka")
    assert grouped[0]["call_id"] == call
    assert any(m["ref_id"] == u1 for m in grouped[0]["matches"])


async def test_clusters(isolated_dirs):
    db = DB()
    s = FakeSearch(db)
    for i, text in enumerate(["Go разработчик, golang, kafka", "Golang backend, go, kafka",
                              "React frontend developer", "Frontend на react", "Повар в ресторан"]):
        db.insert("vacancies", source="telegram", source_ref=str(i), title=text, text=text, found_at=time.time())
    res = await update_clusters(JobsStore(db), s, FakeAgents())
    assert res["created"] == 2
    go = db.all("SELECT cluster_id FROM vacancies WHERE source_ref IN ('0', '1')")
    assert go[0]["cluster_id"] == go[1]["cluster_id"] is not None
    assert db.one("SELECT cluster_id FROM vacancies WHERE source_ref = '4'")["cluster_id"] is None
    db.insert("vacancies", source="telegram", source_ref="5", title="Senior golang go", text="go golang", found_at=time.time())
    res2 = await update_clusters(JobsStore(db), s, FakeAgents())
    assert res2["assigned"] == 1
