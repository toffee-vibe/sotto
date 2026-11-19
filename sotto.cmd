@echo off
rem Sotto launcher for Windows.
rem Uses the local .venv if it exists, otherwise python from PATH.
rem Usage:  sotto.cmd setup    sotto.cmd run    sotto.cmd status
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" -m sotto %*
) else (
  python -m sotto %*
)
endlocal
