import time

import numpy as np
import pytest

from sotto import config as config_mod
from sotto.application.legend import LegendService
from sotto.application.prompting import system_prompt
from sotto.domain.legend import LegendItem, claims
from sotto.domain.speech import INTRO, is_hallucination, name_from_intro
from sotto.domain.transcript import format_transcript
from sotto.infrastructure.audio.speakers import VoiceRegistry
from sotto.infrastructure.llm.agents import extract_json
from sotto.infrastructure.prompts import prompts
from sotto.infrastructure.sqlite.db import DB
from sotto.infrastructure.sqlite.repositories import Repositories
from sotto.interfaces.cli.sim.bench import cer, understood_task
from sotto.jobs.domain import contacts, looks_like_job, text_hash


def test_config_roundtrip(isolated_dirs):
    cfg = config_mod.Config()
    cfg.answer.mode = "auto"
    cfg.jobs.telegram.channels = ["@jobs_it", "@golang_jobs"]
    config_mod.save(cfg)
    loaded = config_mod.load()
    assert loaded.answer.mode == "auto"
    assert loaded.jobs.telegram.channels == ["@jobs_it", "@golang_jobs"]
    config_mod.set_value(loaded, "answer.allow_invention", "false")
    config_mod.set_value(loaded, "recording.fps_idle", "3")
    config_mod.set_value(loaded, "agents.order", "claude\ncodex\n")
    assert loaded.answer.allow_invention is False and loaded.recording.fps_idle == 3
    assert loaded.agents.order == ["claude", "codex"]


def test_legend_extraction():
    text = "Да, [[в 2023 переводил биллинг на Kafka]] и ещё [[вёл команду из 4 человек]]. [[в 2023 переводил биллинг на Kafka]]"
    assert claims(text) == ["в 2023 переводил биллинг на Kafka", "вёл команду из 4 человек"]


def test_system_prompt_contains_profile_legend_and_rule():
    sp = system_prompt(prompts.load("answer"), allow_invention=True, profile="Go-разработчик",
                       legend=[LegendItem(id=1, claim="работал в Ozon", note="2 года", created_at=0)], brief="Компания X")
    assert "Go-разработчик" in sp and "работал в Ozon" in sp and "2 года" in sp and "Компания X" in sp
    assert "[[" in sp  # правило о пометке придуманного подставилось
    sp2 = system_prompt(prompts.load("answer"), allow_invention=False, profile="", legend=[], brief=None)
    assert "Не придумывай опыт" in sp2 and "{invention_rule}" not in sp2


def test_prompt_override(isolated_dirs):
    assert not prompts.is_customized("answer")
    prompts.save("answer", "мой промпт {invention_rule}")
    assert prompts.load("answer").startswith("мой промпт")
    prompts.reset("answer")
    assert "суфлёр" in prompts.load("answer")


def test_transcript_format():
    rows = [{"channel": "them", "speaker": "Анна", "speaker_role": "HR", "started_at": time.time(), "text": "Привет"},
            {"channel": "me", "speaker": "Я", "started_at": time.time(), "text": "Здравствуйте"}]
    out = format_transcript(rows)
    assert "Анна, HR: Привет" in out and "Я (кандидат): Здравствуйте" in out


@pytest.mark.parametrize("text,expected", [
    ("Здравствуйте, меня зовут Анна, я тимлид", True),
    ("Hi, I'm Sarah, an engineering manager", True),
    ("My name is John", True),
    ("Расскажите про индексы", False),
    ("Как вас зовут?", False),
])
def test_intro_detection(text, expected):
    assert bool(INTRO.search(text)) is expected


def test_hallucination_filter():
    assert is_hallucination("Продолжение следует...")
    assert is_hallucination("Субтитры сделал DimaTorzok")
    assert not is_hallucination("Расскажите о себе")


def test_voice_registry_groups_same_speaker():
    reg = VoiceRegistry(threshold=0.6)
    a = np.array([1, 0, 0], dtype=np.float32)
    b = np.array([0, 1, 0], dtype=np.float32)
    assert reg.match(a, 3.0)[0] is None
    reg.add(1, a)
    assert reg.match(a * 0.99 + b * 0.05 / np.linalg.norm(a), 3.0)[0] == 1
    assert reg.match(b, 3.0)[0] is None          # другой голос
    assert reg.match(b, 0.5)[0] == 1             # слишком короткая реплика — не заводим нового


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Вот ответ: [{"n": 0}] спасибо') == [{"n": 0}]
    with pytest.raises(ValueError):
        extract_json("нет json")


def test_contacts_and_filters():
    text = ("Ищем Go-разработчика в финтех! Зарплата 300-400к. Пишите @hr_anna или на jobs@company.ru, "
            "подробнее https://company.ru/careers и t.me/tech_lead_ivan. Бот @vacancy_bot не отвечает.")
    cs = contacts(text)
    values = {c["value"] for c in cs}
    assert "@hr_anna" in values and "jobs@company.ru" in values and "@tech_lead_ivan" in values
    assert "@vacancy_bot" not in values and "@company" not in values
    assert looks_like_job(text)
    assert not looks_like_job("Короткий текст про погоду")
    assert text_hash(text) == text_hash(text.upper())


def test_cer_and_vision_check():
    assert cer("Привет мир", "привет, мир!") == 0
    assert 0 < cer("abcdef", "abcxef") < 0.2
    assert understood_task("Экран: Яндекс Контест — задача про скобки\nИспользуем стек: кладём открывающие скобки…")
    assert not understood_task("Экран: редактор\nВключите подсветку синтаксиса и проверьте stack и скобки")


def test_db_and_legend(isolated_dirs):
    db = DB()
    call = db.insert("calls", started_at=time.time())
    sp = db.insert("speakers", call_id=call, label="Собеседник 1", name="Анна", role="HR")
    db.insert("utterances", call_id=call, speaker_id=sp, channel="them", started_at=time.time(),
              ended_at=time.time(), text="Расскажите о себе")
    repos = Repositories(db)
    rows = repos.calls.transcript(call)
    assert rows[0]["speaker"] == "Анна" and rows[0]["speaker_role"] == "HR"
    db.insert("legend", claim="X", created_at=time.time())
    db.insert("legend", claim="Y", created_at=time.time(), active=0)
    legend = LegendService(repos.calls, repos.legend, repos.interviews)
    assert [x.claim for x in legend.for_call(None)] == ["X"]


@pytest.mark.parametrize("text,name", [
    ("Добрый день! Меня зовут Анна, я HR в компании.", "Анна"),
    ("Привет, я Дмитрий, техлид бэкенда.", "Дмитрий"),
    ("Hi, I'm Sarah, an engineering manager.", "Sarah"),
    ("Я рада вас видеть", None),
])
def test_name_from_intro(text, name):
    assert name_from_intro(text) == name
