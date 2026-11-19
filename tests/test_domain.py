"""Правила предметной области — без базы, сети и ОС."""

from __future__ import annotations

import pytest

from sotto.domain import hints, legend, training
from sotto.domain import interviews as iv
from sotto.domain.calls import Call
from sotto.domain.errors import DomainError
from sotto.domain.interviews import Interview, Status
from sotto.domain.legend import LegendItem
from sotto.jobs.domain import SendLimit, check_send, letter_for, override, resume_for

NOW = 1_800_000_000.0


def item(i, **kw):
    return LegendItem(id=i, claim=kw.pop("claim", f"факт {i}"), created_at=kw.pop("created_at", NOW), **kw)


# --- легенда ---

def test_legend_stays_within_company():
    items = [item(1, call_id=10, company="Acme"), item(2, company=None), item(3, call_id=11, company="Globex"),
             item(4, call_id=12, company=" acme "), item(5, active=0)]
    acme = Call(id=20, started_at=NOW, company="ACME")
    assert [i.id for i in legend.visible_in(acme, items)] == [1, 2, 4]
    assert [i.id for i in legend.visible_in(Call(id=11, started_at=NOW), items)] == [2, 3]  # свой звонок
    assert legend.visible_in(Call(id=10, started_at=NOW, app="training", company="Acme"), items) == []


def test_legend_expiry():
    calls = {1: Call(id=1, started_at=NOW, app="training"), 2: Call(id=2, started_at=NOW)}
    items = [item(1, call_id=1, company="X"), item(2, call_id=2, created_at=NOW - 2 * 86400),
             item(3, call_id=2, created_at=NOW - 3600), item(4, company="Acme"), item(5, company="Globex")]
    assert legend.expired(items, calls, {"acme"}, NOW) == [1, 2, 4]


def test_legend_claims_and_edit():
    assert legend.new_claims("A [[x]] B [[y]] [[x]]", [item(1, claim="X")]) == ["y"]
    it = item(1)
    it.edit("  новый   факт ", " ", active=False)
    assert (it.claim, it.note, it.active) == ("новый факт", None, 0)
    with pytest.raises(DomainError):
        it.edit("   ", None, True)


# --- собеседования ---

def test_call_window_and_company_match():
    a = Interview(id=1, company="Acme", created_at=NOW, scheduled_at=NOW + 20 * 60)
    b = Interview(id=2, company="Globex", created_at=NOW, scheduled_at=NOW + 3 * 3600)
    assert iv.for_call([a, b], NOW) is a
    assert iv.for_call([b], NOW) is None
    assert iv.by_company([a, b], "acme inc", NOW) is a
    a.status = Status.REJECTED
    assert iv.by_company([a, b], "acme", NOW) is None


def test_interview_behaviour():
    x = Interview.new("  Acme ", NOW, stage="HR", scheduled_at=NOW + 600)
    assert x.company == "Acme" and x.is_planned
    x.mark_reminded()
    renamed = x.edit(company="Acme", role=None, stage="HR", scheduled_at=NOW + 7200, duration_min=60, link=None,
                     vacancy="Python", brief=None, notes=None)
    assert not renamed and x.reminded == 0 and x.needs_brief  # новое время — новое напоминание
    x.call_happened()
    assert x.status == Status.DONE
    nxt = x.next_stage(NOW)
    assert x.status == Status.PASSED and nxt.company == "Acme" and nxt.vacancy == "Python" and nxt.is_planned
    with pytest.raises(DomainError):
        x.change_status("maybe")
    with pytest.raises(DomainError):
        Interview.new(" ", NOW)


def test_closed_companies_use_latest_stage():
    hr = Interview(id=1, company="Acme", created_at=NOW, scheduled_at=NOW, status=Status.PASSED)
    tech = Interview(id=2, company="acme", created_at=NOW, scheduled_at=NOW + 86400, status=Status.REJECTED)
    assert iv.closed_companies([hr, tech]) == {"acme"}
    tech.status = Status.PLANNED
    assert iv.closed_companies([hr, tech]) == set()


def test_reminders():
    soon = Interview(id=1, company="Acme", created_at=NOW, scheduled_at=NOW + 10 * 60, stage="HR")
    later = Interview(id=2, company="B", created_at=NOW, scheduled_at=NOW + 3600)
    assert iv.due_reminders([soon, later], NOW) == [soon]
    assert iv.reminder_text(soon, NOW) == "Через 10 мин собеседование: Acme (HR)"


# --- подсказки ---

@pytest.mark.parametrize("text,speaking,ok", [
    ("Как бы вы шардировали эту таблицу?", False, True),
    ("Угу, понятно", False, False),
    ("Tell me about the", False, False),
    ("Кхм.", True, False),
])
def test_speculation_rules(text, speaking, ok):
    assert hints.worth_speculating(text, speaking) is ok


RU_PROFILE = "Русский — родной. Английский — B2."


@pytest.mark.parametrize("profile,native", [
    (RU_PROFILE, "русский"),
    ("English — native speaker.", "english"),
    ("без указания языка", ""),
])
def test_native_language(profile, native):
    assert hints.native_language(profile) == native


def test_trim_nonnative():
    long_hint = "Yes, I built it at my last job. It watched resources and retried failed updates. " \
                "I also set timeouts on every external call to avoid stuck workers."
    assert len(long_hint.split()) > 25
    trimmed = hints.trim_nonnative(long_hint, RU_PROFILE, 25)
    assert len(trimmed.split()) <= 25
    assert trimmed.startswith("Yes, I built it")
    # короткую и русскую подсказку не трогаем
    short = "Yes, I can see your screen."
    assert hints.trim_nonnative(short, RU_PROFILE, 25) == short
    assert hints.trim_nonnative("Да, писал на Go в продакшене.", RU_PROFILE, 25) == "Да, писал на Go в продакшене."
    # английский родной или лимит выключен — не обрезаем
    assert hints.trim_nonnative(long_hint, "English — native.", 25) == long_hint
    assert hints.trim_nonnative(long_hint, RU_PROFILE, 0) == long_hint
    # одно длинное предложение оставляем целым — резать посреди фразы хуже
    one_sentence = " ".join(["word"] * 30) + "."
    assert hints.trim_nonnative(one_sentence, RU_PROFILE, 25) == one_sentence


# --- тренер ---

def test_spaced_repetition():
    assert training.next_due(1.0, NOW) - NOW > training.next_due(0.0, NOW) - NOW
    assert training.blend_score(0.5, 0, 0.9) == 0.9
    assert training.needs_refresh(NOW, False, 0, NOW + 10, force=False) is False
    assert training.needs_refresh(NOW - 4 * 86400, False, 0, NOW, force=False) is True
    assert training.wants_to_stop("Стоп!") and not training.wants_to_stop("стоп-кран")


# --- вакансии ---

def test_send_limits():
    ok = dict(sent_today=0, daily_limit=8, last_sent_at=None, min_interval_s=240, already_sent=False, now=NOW)
    check_send("telegram", "@hr", **ok)
    for bad in ({"sent_today": 8}, {"last_sent_at": NOW - 10}, {"already_sent": True}):
        with pytest.raises(SendLimit):
            check_send("telegram", "@hr", **{**ok, **bad})


def test_letter_and_resume_choice():
    cluster = {"letter_md": "{greeting}! {position} в {company}.", "resume_md": "CV кластера"}
    assert letter_for({"title": "Go dev", "company": "Ozon", "language": "en"}, cluster) == "Hi! Go dev в Ozon."
    assert resume_for({}, cluster, "base") == "CV кластера" and resume_for({}, None, "base") == "base"
    assert override(" то же ", "то же") is None and override("другое", "то же") == "другое"
