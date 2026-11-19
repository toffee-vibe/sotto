"""Все страницы панели открываются; основные формы работают. Без звука, Hyprland и окон."""

import time

from fastapi.testclient import TestClient

from sotto import config as config_mod
from sotto.bootstrap import Sotto
from sotto.interfaces.web.app import build_app


def make_client(**kw):
    cfg = config_mod.Config()
    cfg.recording.enabled = False
    d = Sotto(cfg, with_overlay=False, with_desktop=False, **kw)
    return d, TestClient(build_app(d), base_url=f"http://127.0.0.1:{cfg.web.port}")


def test_pages_render(isolated_dirs):
    d, c = make_client()
    call = d.db.insert("calls", started_at=time.time() - 600, ended_at=time.time(), title="Тест", summary="Описание")
    sp = d.db.insert("speakers", call_id=call, label="Собеседник 1")
    d.db.insert("utterances", call_id=call, speaker_id=sp, channel="them", started_at=time.time() - 500,
                ended_at=time.time() - 495, text="Вопрос")
    d.db.insert("answers", call_id=call, kind="manual", text="Ответ [[придумано]]", created_at=time.time())
    vid = d.db.insert("vacancies", source="telegram", source_ref="1", title="Go dev", text="Ищем Go",
                      contacts='[{"type": "telegram", "value": "@hr"}]', found_at=time.time())
    d.db.insert("clusters", name="Go", letter_md="{greeting}! Хочу на {position} в {company}.")
    for path in ["/", "/calls", f"/calls/{call}", "/calls?q=", "/legend", "/profile", "/prompts", "/settings",
                 "/recordings", "/trainer", "/jobs", f"/jobs/{vid}", "/resume"]:
        r = c.get(path)
        assert r.status_code == 200, path
    assert "придумано" in c.get(f"/calls/{call}").text


def test_forms(isolated_dirs):
    d, c = make_client()
    c.post("/profile", data={"text": "# Иван\nGo-разработчик"})
    assert "Go-разработчик" in c.get("/profile").text
    c.post("/legend/add", data={"claim": "Вёл миграцию на Kafka", "note": ""})
    assert d.legend.for_call(None)[0].claim == "Вёл миграцию на Kafka"
    c.post("/settings", data={"answer.mode": "auto", "openrouter.api_key": "",
                              "_checkboxes": "answer.allow_invention"})
    assert d.cfg.answer.mode == "auto" and d.cfg.answer.allow_invention is False
    assert config_mod.load().answer.mode == "auto"
    assert c.post("/api/ctl/toggle_mode").json()["ok"]
    assert d.cfg.answer.mode == "manual"


def test_letter_substitution(isolated_dirs):
    from sotto.jobs.domain import letter_for
    cluster = {"letter_md": "{greeting}! Хочу на позицию {position} в {company}."}
    assert letter_for({"title": "Go dev", "company": "Ozon"}, cluster) == "Здравствуйте! Хочу на позицию Go dev в Ozon."
    assert letter_for({"letter_override": "своё"}, cluster) == "своё"


def test_foreign_sites_blocked(isolated_dirs):
    d, c = make_client()
    # CSRF: чужой сайт отправляет форму в панель
    r = c.post("/settings", data={"answer.mode": "manual"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and d.cfg.answer.mode == "auto"
    # DNS rebinding: чужой Host
    assert c.get("/calls", headers={"Host": "evil.example:8765"}).status_code == 403
    # sotto ctl / хоткеи — без Origin, проходят
    assert c.post("/api/ctl/toggle_mode").json()["ok"]


def test_settings_cannot_change_base_url(isolated_dirs):
    d, c = make_client()
    c.post("/settings", data={"openrouter.base_url": "https://evil.example", "web.port": "1"})
    assert d.cfg.openrouter.base_url.startswith("https://openrouter.ai") and d.cfg.web.port == 8765


def test_brief_from_trainer_reaches_next_call(isolated_dirs):
    """Тренажёр шлёт выжимку вакансии без Origin, как httpx; она должна дойти до следующего звонка."""
    d, c = make_client()
    r = c.post("/brief", data={"brief": "Middle Python в Acme"}, follow_redirects=False)
    assert r.status_code == 303
    assert d.db.get_meta("next_call_brief") == "Middle Python в Acme"
    d.voice.call_started("training")
    assert d.db.one("SELECT brief FROM calls WHERE id = ?", [d.voice.call_id])["brief"] == "Middle Python в Acme"
