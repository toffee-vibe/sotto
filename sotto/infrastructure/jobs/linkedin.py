"""LinkedIn: раздел Jobs и посты о найме — через твой браузерный профиль, медленно.

У LinkedIn нет API для этого, а автоматизация аккаунта — риск ограничений. Поэтому:
* отдельный профиль браузера Sotto, вход — один раз руками (`sotto jobs login-linkedin`);
* чтение без окна, с человеческими паузами, не больше `daily_page_limit` страниц в сутки;
* разметку страниц не парсим жёстко: берём текст карточек, поля раскладывает агент — переживёт редизайн;
* отправку НЕ автоматизируем вообще: Sotto открывает страницу (вакансию или профиль автора поста)
  и кладёт готовый текст в буфер обмена — «Отправить» ты нажимаешь сам на LinkedIn.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time
from typing import Any
from urllib.parse import quote

from ... import paths
from ...config import LinkedIn as LinkedInCfg
from ...domain.jobs import RawPost

log = logging.getLogger(__name__)

PROFILE_DIR = paths.JOBS_DIR / "linkedin-browser"
# User-Agent обязан совпадать с платформой: LinkedIn сравнивает его с TLS/рендер-отпечатком,
# и вход на Windows под Linux-строкой — прямой путь на чекпоинт или бан.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36" if sys.platform == "win32"
      else "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")


class PageBudget:
    """Суточный бюджет открытых страниц — главный предохранитель от бана."""

    def __init__(self, db: Any, limit: int):
        self.db, self.limit = db, limit

    def _key(self) -> str:
        return "li_pages:" + time.strftime("%Y-%m-%d")

    def left(self) -> int:
        return self.limit - int(self.db.get_meta(self._key()) or 0)

    def spend(self) -> bool:
        used = int(self.db.get_meta(self._key()) or 0)
        if used >= self.limit:
            return False
        self.db.set_meta(self._key(), str(used + 1))
        return True


async def _context(p, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless, user_agent=UA, locale="ru-RU",
        viewport={"width": 1366, "height": 900})


async def login() -> None:
    """Открывает окно браузера для ручного входа. Закрой окно, когда увидишь ленту."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError("Для LinkedIn нужны зависимости: pip install sotto[jobs] && playwright install chromium") from e
    async with async_playwright() as p:
        ctx = await _context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.linkedin.com/login")
        print("Войди в LinkedIn в открывшемся окне. Как появится лента — закрой окно.")
        try:
            await page.wait_for_url(re.compile(r"linkedin\.com/feed"), timeout=600_000)
            await asyncio.sleep(3)
            print("Готово, сессия сохранена.")
        finally:
            await ctx.close()


def logged_in() -> bool:
    return (PROFILE_DIR / "Default").exists()


async def _human_scroll(page, times: int) -> None:
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(500, 900))
        await asyncio.sleep(random.uniform(1.2, 2.8))


async def fetch(cfg: LinkedInCfg, db: Any, queries: list[str]) -> list[RawPost]:
    """db — хранилище вакансий: суточный бюджет страниц (meta) и «уже видели» (known)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError("Для LinkedIn нужны зависимости: pip install sotto[jobs] && playwright install chromium") from e

    budget = PageBudget(db, cfg.daily_page_limit)
    posts: list[RawPost] = []
    async with async_playwright() as p:
        ctx = await _context(p, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            for q in queries:
                # 1) Посты: многие ищут людей в ленте, не покупая размещение вакансий.
                if not budget.spend():
                    break
                url = ("https://www.linkedin.com/search/results/content/?keywords="
                       + quote(q) + "&datePosted=%22past-week%22&sortBy=%22date_posted%22")
                await page.goto(url, wait_until="domcontentloaded")
                if "login" in page.url or "checkpoint" in page.url:
                    raise RuntimeError("LinkedIn: сессия истекла или проверка — sotto jobs login-linkedin")
                await asyncio.sleep(random.uniform(3, 6))
                await _human_scroll(page, 4)
                for item in await page.evaluate(POSTS_JS):
                    posts.append(RawPost(source="linkedin_posts", source_ref=item["id"], text=item["text"],
                                         url=item.get("url"), extra={"author": item.get("author")}))
                await asyncio.sleep(random.uniform(cfg.page_delay_s * 0.7, cfg.page_delay_s * 1.3))

                # 2) Раздел Jobs: список + описания нескольких вакансий.
                if not budget.spend():
                    break
                await page.goto("https://www.linkedin.com/jobs/search/?keywords=" + quote(q) + "&f_TPR=r604800",
                                wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 6))
                await _human_scroll(page, 2)
                jobs = await page.evaluate(JOBS_JS)
                for job in jobs[:6]:
                    if db.known("linkedin_jobs", job["id"]):
                        continue
                    if not budget.spend():
                        break
                    await page.goto(job["url"], wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(4, 8))
                    text = await page.evaluate(DESCRIPTION_JS)
                    posts.append(RawPost(source="linkedin_jobs", source_ref=job["id"], url=job["url"],
                                         text=f"{job['card']}\n\n{text}"))
                    await asyncio.sleep(random.uniform(cfg.page_delay_s * 0.7, cfg.page_delay_s * 1.3))
        finally:
            await ctx.close()
    log.info("LinkedIn: собрано %s, осталось страниц на сегодня %s", len(posts), budget.left())
    return posts


def open_for_reply(url: str, text: str) -> None:
    """Открыть страницу в обычном браузере и положить текст в буфер — отправишь сам."""
    import webbrowser

    from ..platform import current
    current().copy_text(text)
    webbrowser.open(url)


# Извлечение текста — максимально «тупое» и устойчивое к смене вёрстки.
POSTS_JS = r"""
() => {
  const out = [];
  const nodes = document.querySelectorAll('[data-urn*="activity"], [data-id*="activity"], div.feed-shared-update-v2');
  for (const n of nodes) {
    const urn = n.getAttribute('data-urn') || n.getAttribute('data-id') || '';
    const text = (n.innerText || '').trim();
    if (!urn || text.length < 150) continue;
    const a = n.querySelector('a[href*="/in/"], a[href*="/company/"]');
    out.push({id: urn, text: text.slice(0, 4000),
              url: 'https://www.linkedin.com/feed/update/' + urn.replace(/^.*?(urn:li:activity:\d+).*$/, '$1') + '/',
              author: a ? a.href.split('?')[0] : null});
  }
  return out;
}
"""

JOBS_JS = r"""
() => {
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href*="/jobs/view/"]')) {
    const m = a.href.match(/\/jobs\/view\/(\d+)/); if (!m || seen.has(m[1])) continue; seen.add(m[1]);
    const card = a.closest('li') || a.parentElement;
    out.push({id: m[1], url: 'https://www.linkedin.com/jobs/view/' + m[1] + '/', card: (card ? card.innerText : a.innerText).trim().slice(0, 600)});
  }
  return out;
}
"""

DESCRIPTION_JS = r"""
() => {
  const el = document.querySelector('#job-details, .jobs-description__content, .jobs-box__html-content, main');
  return (el ? el.innerText : document.body.innerText).trim().slice(0, 6000);
}
"""
