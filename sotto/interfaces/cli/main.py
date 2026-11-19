"""Команда `sotto`.

  sotto                      — сайт (127.0.0.1:8765); голосовой суфлёр запускается кнопкой на сайте
  sotto run                  — сайт и сразу голосовой суфлёр (звук, звонки, запись, подсказки, оверлей)
  sotto ctl <действие>       — то, что вызывают хоткеи: ask, screen, toggle_mode…
  sotto overlay              — только окно оверлея (обычно его запускает демон)
  sotto stage on|off         — виртуальный монитор «сцена» для демонстрации
  sotto setup                — первичная настройка: ключ, профиль, промпты агентом
  sotto bench                — замер моделей OpenRouter и выбор лучших по задержке/цене
  sotto sim [сценарий]       — тестовый интервьюер: проверка и тренажёр работы с суфлёром
  sotto trainer knowledge|english [тема] — голосовые тренеры
  sotto jobs fetch|cluster   — сбор вакансий и кластеризация
  sotto status               — что сейчас происходит
  sotto translate run|enroll|say — ЭКСПЕРИМЕНТ: голосовой переводчик звонка
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

import httpx

from ... import config as config_mod
from ... import paths


def setup_logging(verbose: bool) -> None:
    paths.ensure_dirs()
    handlers: list[logging.Handler] = [RotatingFileHandler(paths.LOG_FILE, maxBytes=5_000_000, backupCount=3)]
    handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "telethon", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def base_url() -> str:
    cfg = config_mod.load()
    return f"http://{cfg.web.host}:{cfg.web.port}"


def cmd_ctl(args: argparse.Namespace) -> int:
    try:
        r = httpx.post(f"{base_url()}/api/ctl/{args.action}", timeout=10)
        print(r.json())
        return 0
    except httpx.HTTPError:
        print("Демон не запущен: sotto run", file=sys.stderr)
        return 1


def cmd_status(_args: argparse.Namespace) -> int:
    try:
        for k, v in httpx.get(f"{base_url()}/api/status", timeout=5).json().items():
            print(f"{k:16} {v}")
        return 0
    except httpx.HTTPError:
        print("Демон не запущен")
        return 1


def cmd_stage(args: argparse.Namespace) -> int:
    from ...infrastructure.platform import detect
    cfg = config_mod.load()
    wm = detect(cfg.stage.name, cfg.stage.mirror_on).wm
    print(wm.stage_on() if args.state == "on" else wm.stage_off())
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # Консоль Windows без UTF-8 (cp1252/cp866) падает на русском тексте справки и сообщений.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 — перенаправленный поток может не уметь reconfigure
                pass
    p = argparse.ArgumentParser(prog="sotto", description="Невидимый ИИ-суфлёр для собеседований")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    w = sub.add_parser("web", help="сайт; голосовой суфлёр запускается на нём (то же, что просто `sotto`)")
    w.add_argument("--no-browser", action="store_true")

    r = sub.add_parser("run", help="запустить демон")
    r.add_argument("--no-overlay", action="store_true")
    r.add_argument("--no-hyprland", action="store_true", help="не регистрировать хоткеи и правила окон")
    c = sub.add_parser("ctl", help="действие (для хоткеев)")
    c.add_argument("action")
    sub.add_parser("overlay", help="окно подсказок")
    sub.add_parser("panel", help="то же, что `sotto web`")
    s = sub.add_parser("stage", help="виртуальный монитор для демонстрации")
    s.add_argument("state", choices=["on", "off"])
    sub.add_parser("setup", help="первичная настройка")
    b = sub.add_parser("bench", help="замер моделей")
    b.add_argument("--apply", action="store_true", help="записать лучшие модели в конфиг")
    b.add_argument("--quick", action="store_true", help="меньше кандидатов")
    sm = sub.add_parser("sim", help="тестовый интервьюер / тренажёр")
    sm.add_argument("scenario", nargs="?", default="tutorial",
                    help="tutorial — тренировочное собеседование голосом; остальные — проверка суфлёра")
    sm.add_argument("--role", default=None, help="роль для собеседования (иначе спросит)")
    sm.add_argument("--lang", choices=["ru", "en"], default=None, help="язык собеседования (иначе спросит)")
    sm.add_argument("--list", action="store_true")
    t = sub.add_parser("trainer", help="голосовой тренер")
    t.add_argument("kind", choices=["knowledge", "english"])
    t.add_argument("topic", nargs="?", default="")
    t.add_argument("--mode", choices=["interview", "lesson"], default="interview")
    j = sub.add_parser("jobs", help="вакансии")
    j.add_argument("action", choices=["fetch", "cluster", "login-telegram", "login-linkedin"])
    sub.add_parser("status", help="состояние демона")
    sh = sub.add_parser("shot", help="снимок экрана или выделенной области (вызывает демон отдельным процессом)")
    sh.add_argument("out", nargs="?", default="")
    sh.add_argument("--region", action="store_true")
    au = sub.add_parser("autostart", help="запуск при входе в систему (Windows)")
    au.add_argument("state", choices=["on", "off"])
    tr = sub.add_parser("translate", help="ЭКСПЕРИМЕНТ: голосовой переводчик звонка")
    tr.add_argument("action", choices=["run", "enroll", "say"])
    tr.add_argument("text", nargs="?", default="")

    args = p.parse_args(argv)
    setup_logging(args.verbose and args.cmd != "ctl")

    if args.cmd in (None, "web", "panel"):
        from ...bootstrap import run
        run(voice=False, open_browser=not getattr(args, "no_browser", False))
        return 0
    if args.cmd == "run":
        from ...bootstrap import run
        run(with_overlay=not args.no_overlay, with_desktop=not args.no_hyprland)
        return 0
    if args.cmd == "ctl":
        return cmd_ctl(args)
    if args.cmd == "overlay":
        from ..overlay import main as overlay_main
        overlay_main()
        return 0
    if args.cmd == "stage":
        return cmd_stage(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "shot":
        from .shot import main as shot_main
        return shot_main(args.out, args.region)
    if args.cmd == "autostart":
        if sys.platform != "win32":
            print("На Linux автозапуск — через home-manager (services.sotto.enable) или systemd; см. README.")
            return 1
        from ...infrastructure.platform.windows import winsys
        winsys.set_autostart(args.state == "on")
        print(f"Автозапуск: {'включён' if args.state == 'on' else 'выключен'}")
        return 0
    if args.cmd == "setup":
        from .setup import run_setup
        return asyncio.run(run_setup())
    if args.cmd == "bench":
        from .sim.bench import run_bench
        return asyncio.run(run_bench(apply=args.apply, quick=args.quick))
    if args.cmd == "sim" and args.scenario in ("tutorial", "interview"):
        from .sim.interview import run_interview
        return run_interview(role=args.role, lang=args.lang)
    if args.cmd == "sim":
        from .sim.simulator import list_scenarios, run_scenario
        if args.list:
            for name, title in list_scenarios():
                print(f"{name:20} {title}")
            return 0
        return run_scenario(args.scenario)
    if args.cmd == "trainer":
        from .trainer import run_trainer
        return asyncio.run(run_trainer(args.kind, args.topic, args.mode))
    if args.cmd == "translate":
        from ...experimental.translator.cli import main as translate_main
        return translate_main(args.action, args.text)
    if args.cmd == "jobs":
        from .jobs import run_jobs
        return asyncio.run(run_jobs(args.action))
    return 1


if __name__ == "__main__":
    sys.exit(main())
