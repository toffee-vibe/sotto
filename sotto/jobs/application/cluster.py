"""Кластеры похожих вакансий: одно резюме и одно письмо на кластер, а не на каждую вакансию.

Алгоритм простой и предсказуемый:
* вакансия → эмбеддинг (та же локальная модель, что и для поиска);
* если похожа на центр существующего кластера (косинус ≥ ASSIGN) — в него;
* оставшиеся жадно группируются между собой; группа из ≥ MIN_SIZE становится новым кластером,
  которому агент даёт имя и описание; одиночки ждут, пока появятся похожие.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from ...application.ports import AgentError, Agents
from ..domain import centroid, group, nearest


def _texts(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{r['title'] or ''}. {r['text'][:1500]}" for r in rows]


async def update_clusters(store: Any, search: Any, agents: Agents) -> dict[str, int]:
    rows = store.unclustered()
    if not rows:
        return {"assigned": 0, "created": 0}
    vecs: np.ndarray = await asyncio.to_thread(search.embed, _texts(rows))
    clusters = store.centroids()
    assigned = 0
    leftovers: list[int] = []
    for i, r in enumerate(rows):
        cid = nearest(vecs[i], clusters)
        if cid is not None:
            store.assign(r["id"], cid)
            assigned += 1
        else:
            leftovers.append(i)

    created = 0
    for g in group(vecs, leftovers):
        sample = "\n\n".join(f"- {rows[i]['title'] or ''}: {rows[i]['text'][:600]}" for i in g[:8])
        prompt = ("Это группа похожих вакансий. Дай ей короткое название (роль + ключевой стек/домен, 2–6 слов) и "
                  "описание: общие требования, уровень, типичные задачи (3–4 строки). Верни ТОЛЬКО JSON "
                  '{"name": "...", "description": "..."}\n\n' + sample)
        try:
            meta, _ = await agents.run_json(prompt, task="название кластера")
            name, desc = str(meta.get("name") or "Кластер"), str(meta.get("description") or "")
        except (AgentError, AttributeError):
            name, desc = rows[g[0]]["title"] or "Кластер", ""
        cid = store.add_cluster(name, desc, centroid(vecs[g]))
        for i in g:
            store.assign(rows[i]["id"], cid)
        created += 1

    await asyncio.to_thread(_recompute_centroids, store, search)
    return {"assigned": assigned, "created": created}


def _recompute_centroids(store: Any, search: Any) -> None:
    for cid in store.cluster_ids():
        vs = store.cluster_vacancies(cid)
        if vs:
            store.update_cluster(cid, centroid=centroid(search.embed(_texts(vs))))
