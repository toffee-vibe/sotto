"""Границы слоёв (Clean Architecture): зависимости направлены только внутрь.

  domain          ← ни от чего (только стандартная библиотека и numpy)
  application     ← domain (+ config, paths)
  infrastructure  ← application, domain
  interfaces      ← всё
  bootstrap       ← всё (корень композиции)

Тест разбирает import'ы каждого файла, ничего не запуская.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "sotto"

FORBIDDEN = {
    "domain": ("sotto.application", "sotto.infrastructure", "sotto.interfaces", "sotto.bootstrap", "sotto.config",
               "sotto.paths"),
    "application": ("sotto.infrastructure", "sotto.interfaces", "sotto.bootstrap"),
    "infrastructure": ("sotto.interfaces", "sotto.bootstrap"),
}
THIRD_PARTY_IN_DOMAIN = {"numpy"}
# Сценарии, которым пока нужен звук или модели напрямую (локальный Whisper, VAD, синтез). Это адаптеры,
# которые ещё не вынесены за порт; список может только сокращаться.
KNOWN_EXCEPTIONS = {
    ("application/listener.py", "sotto.infrastructure.audio"),
    ("application/trainer.py", "sotto.infrastructure.audio"),
    ("application/voice.py", "sotto.infrastructure.audio"),
    ("application/voice.py", "sotto.infrastructure.process"),
}
# Изолированные контексты со своими слоями domain/application/infrastructure/interfaces внутри.
CONTEXTS = ("jobs",)


def _module(path: Path) -> str:
    return "sotto." + ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = _module(path).rsplit(".", 1)[0] if path.name != "__init__.py" else _module(path).removesuffix(".__init__")
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append(mod)
            out += [f"{mod}.{a.name}" for a in node.names]
    return out


def test_layers_point_inward():
    # Контексты (sotto/jobs/) повторяют те же слои внутри себя.
    roots = [ROOT] + [ROOT / ctx for ctx in CONTEXTS]
    violations = []
    for root in roots:
        for layer, banned in FORBIDDEN.items():
            for path in (root / layer).rglob("*.py") if (root / layer).exists() else []:
                rel = str(path.relative_to(ROOT))
                for imp in _imports(path):
                    hit = next((b for b in banned if imp == b or imp.startswith(b + ".")), None)
                    if hit and not any(rel == f and imp.startswith(prefix) for f, prefix in KNOWN_EXCEPTIONS):
                        violations.append(f"{rel}: {imp}")
    assert not violations, "Зависимость против слоёв:\n" + "\n".join(sorted(set(violations)))


def test_domain_has_no_io():
    banned = ("sqlite3", "subprocess", "httpx", "socket", "fastapi", "os", "shutil", "pathlib", "asyncio", "threading")
    bad = []
    for path in (ROOT / "domain").rglob("*.py"):
        for imp in _imports(path):
            top = imp.split(".")[0]
            if top in banned:
                bad.append(f"{path.name}: {imp}")
            if not imp.startswith(("sotto", "__future__")) and top not in THIRD_PARTY_IN_DOMAIN and top not in _stdlib():
                bad.append(f"{path.name}: внешняя библиотека {imp}")
    assert not bad, bad


def test_sql_only_in_infrastructure():
    """SQL пишется только в infrastructure/sqlite: сценарии и сайт работают через репозитории и запросы."""
    bad = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "infrastructure" or rel.parts[0] == "experimental":
            continue
        if len(rel.parts) > 1 and rel.parts[1] == "infrastructure":  # инфраструктура контекста (sotto/jobs/…)
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bdb\.(all|one|execute|insert|update)\(|\b(SELECT|INSERT INTO|DELETE FROM)\b", text):
            bad.append(str(rel))
    assert not bad, bad


def test_only_platform_touches_the_desktop():
    """Команды Linux-рабочего стола — только в infrastructure/platform/linux (остальное — через порт Platform)."""
    tools = ("hyprctl", "pw-record", "pw-play", "pw-dump", "wf-recorder", "notify-send", "wl-paste", "grim", "slurp")
    bad = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[:3] == ("infrastructure", "platform", "linux") or rel.parts[0] == "experimental":
            continue
        if str(rel) in ("config.py", "infrastructure/platform/__init__.py"):  # имена программ, а не вызовы:
            continue  # список «не звонок» в настройках и выбор платформы по наличию PipeWire
        text = path.read_text(encoding="utf-8")
        bad += [f"{rel}: {t}" for t in tools if f'"{t}"' in text]
    assert not bad, bad


def _stdlib() -> set[str]:
    import sys
    return set(sys.stdlib_module_names)


def test_site_changes_data_only_through_use_cases():
    """Сайт меняет данные через сценарии, а не через репозитории напрямую (чтение — через Queries)."""
    bad = []
    for path in (ROOT / "interfaces").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        bad += [f"{path.relative_to(ROOT)}: {m.group(0)}" for m in re.finditer(r"\.repos\.\w+", text)]
    assert not bad, bad


def test_application_starts_no_processes():
    """Процессы ОС и файлы настроек — за портами; сценарии их не трогают."""
    bad = [str(p.relative_to(ROOT)) for p in (ROOT / "application").rglob("*.py")
           if any(i.split(".")[0] in ("subprocess", "shutil") or i.startswith("sotto.config.") and "save" in i
                  for i in _imports(p))]
    assert not bad, bad
