"""Запуск вспомогательных процессов (захват звука, запись экрана, ffmpeg, CLI-агенты, оверлей).

Linux:
* Своя сессия: Ctrl+C в терминале получает только демон, а он сам корректно гасит детей.
* PR_SET_PDEATHSIG: если демон упал или убит (kill -9), дети получают SIGINT и завершаются.

Windows:
* CREATE_NO_WINDOW: у ffmpeg и CLI-агентов не мигают чёрные консоли.
* Job Object с KILL_ON_JOB_CLOSE: если демон умрёт (в том числе аварийно), Windows сама
  закроет все дочерние процессы — запись экрана не останется писать вечно.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"


def sotto_argv() -> list[str]:
    """Как запустить Sotto в дочернем процессе (оверлей, выделение области).

    Запускаем модулем из того же интерпретатора: консольный `sotto.exe` в Windows
    открыл бы лишнее чёрное окно, а `pythonw.exe` (автозапуск) — не открывает."""
    return [sys.executable, "-m", "sotto"]


def child_env() -> dict[str, str]:
    """Окружение для `python -m sotto` из исходников: без PYTHONPATH модуль не найдётся."""
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent.parent)
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root + (os.pathsep + current if current else "")
    return env


def _linux_child_setup() -> None:
    import ctypes
    os.setsid()
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGINT)  # PR_SET_PDEATHSIG


if WINDOWS:  # pragma: no cover - выбирается на Windows
    CREATE_NO_WINDOW = 0x08000000
    # Дети без консоли: ffmpeg, тренер, CLI-агенты.
    CHILD: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    # Дети со своим окном (оверлей, выделение области): консоль не нужна, окно — нужно.
    DETACHED: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    CHILD = {"preexec_fn": _linux_child_setup} if sys.platform.startswith("linux") else {"start_new_session": True}
    DETACHED = {"start_new_session": True}


if WINDOWS:  # pragma: no cover - выбирается на Windows
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimitInformation),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _job: int | None = None

    def _job_handle() -> int | None:
        """Один Job Object на процесс: при выходе демона Windows гасит всех детей."""
        global _job
        if _job is not None:
            return _job
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            log.debug("CreateJobObject: ошибка %s", ctypes.get_last_error())
            return None
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                                         ctypes.byref(info), ctypes.sizeof(info))
        _job = handle
        return handle

    def attach(proc: subprocess.Popen) -> subprocess.Popen:
        """Привязать процесс к Job Object. Без этого аварийно убитый демон оставит ffmpeg жить."""
        try:
            handle = getattr(proc, "_handle", None)
            job = _job_handle()
            if handle and job and not kernel32.AssignProcessToJobObject(job, int(handle)):
                log.debug("AssignProcessToJobObject: ошибка %s", ctypes.get_last_error())
        except Exception:  # noqa: BLE001 — не критично для работы
            log.debug("Не удалось привязать процесс к Job Object", exc_info=True)
        return proc

    def graceful_stop(proc: subprocess.Popen, quit_bytes: bytes | None = None) -> None:
        """Попросить ffmpeg дописать файл: «q» в stdin, потом terminate."""
        try:
            if quit_bytes and proc.stdin is not None and proc.poll() is None:
                proc.stdin.write(quit_bytes)
                proc.stdin.flush()
                proc.wait(timeout=8)
                return
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            proc.terminate()

    def terminate_tree(proc: subprocess.Popen) -> None:
        """Погасить процесс вместе с потомками: python-обёртка может держать настоящий интерпретатор
        отдельным процессом, и одного terminate() не хватит — оверлей остался бы висеть на экране."""
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        except (OSError, subprocess.SubprocessError):
            if proc.poll() is None:
                proc.terminate()

    def kill_pid(pid: int) -> None:
        """Погасить процесс по pid вместе с потомками (процессы-посредники, осиротевший оверлей)."""
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        except (OSError, subprocess.SubprocessError):
            log.debug("Не удалось погасить процесс %s", pid)

else:
    def attach(proc: subprocess.Popen) -> subprocess.Popen:
        return proc

    def graceful_stop(proc: subprocess.Popen, quit_bytes: bytes | None = None) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)

    def terminate_tree(proc: subprocess.Popen) -> None:
        """Гасим всю группу процессов, а не только первого: у него своя сессия."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            if proc.poll() is None:
                proc.terminate()

    def kill_pid(pid: int) -> None:
        """Погасить процесс по pid (в Linux он уже в своей сессии — хватит SIGTERM)."""
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            log.debug("Не удалось погасить процесс %s", pid)
