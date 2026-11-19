<#
.SYNOPSIS
    Установка Sotto на Windows 11: окружение, зависимости, ffmpeg, первый запуск.

.DESCRIPTION
    Создаёт .venv рядом с проектом, ставит Sotto и зависимости, проверяет ffmpeg
    (без него не будет записи экрана), подсказывает следующие шаги.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Autostart
#>
[CmdletBinding()]
param(
    [switch]$Autostart,
    [switch]$NoVenv
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Push-Location $root
try {
    Write-Host "Sotto: установка в $root" -ForegroundColor Cyan

    # --- Python ---
    $python = $null
    foreach ($candidate in @('python', 'py')) {
        $exe = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        try {
            $version = & $exe.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ([version]$version -ge [version]'3.12') {
            $python = $exe.Source
            break
        }
    }
    if (-not $python) {
        throw "Нужен Python 3.12+ (python.org/downloads). После установки открой новое окно терминала."
    }
    Write-Host "Python: $python ($version)" -ForegroundColor Green

    # --- окружение ---
    if ($NoVenv) {
        $runner = $python
        Write-Host "Ставлю в системный Python (без .venv) по вашему выбору."
    } else {
        if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
            Write-Host "Создаю .venv…"
            & $python -m venv "$root\.venv"
        }
        $runner = "$root\.venv\Scripts\python.exe"
    }
    & $runner -m pip install --upgrade pip setuptools wheel --quiet

    # Windows-setuptools падает с «файл уже существует», если остался build\ от прошлой сборки,
    # поэтому чистим хвосты перед каждой попыткой установки.
    function Install-Sotto([string[]]$ExtraArgs) {
        foreach ($junk in @("$root\build", "$root\sotto.egg-info")) {
            if (Test-Path $junk) { Remove-Item -Recurse -Force $junk -ErrorAction SilentlyContinue }
        }
        & $runner -m pip install --quiet @ExtraArgs '.[windows]'
        return $LASTEXITCODE
    }

    Write-Host "Ставлю Sotto и зависимости (первый раз качается ~1 ГБ: PySide6, модели, onnxruntime)…"
    $code = Install-Sotto @()
    if ($code -ne 0) {
        Write-Host "Обычная установка не прошла — повторяю без изолированной сборки…"
        $code = Install-Sotto @('--no-build-isolation')
    }
    if ($code -ne 0) { throw "pip install завершился с ошибкой (текст ошибки — выше)" }
    Write-Host "Sotto установлен." -ForegroundColor Green

    # --- ffmpeg ---
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ffmpeg) {
        Write-Host "ffmpeg: $($ffmpeg.Source)" -ForegroundColor Green
    } else {
        Write-Warning "ffmpeg не найден — без него не будет записи экрана и звуковых дорожек."
        Write-Host "  Установить: winget install --id Gyan.FFmpeg -e   (потом открой новое окно терминала)"
    }

    # --- автозапуск ---
    $sotto = if ($NoVenv) { 'sotto' } else { "$root\.venv\Scripts\sotto.exe" }
    if ($Autostart) {
        & $sotto autostart on
    }

    Write-Host ""
    Write-Host "Дальше:" -ForegroundColor Cyan
    Write-Host "  1) $sotto setup      # ключ OpenRouter, локальные модели, профиль"
    Write-Host "  2) $sotto run        # суфлёр и панель http://127.0.0.1:8765"
    Write-Host "  3) $sotto autostart on   # запускать при входе в систему"
    Write-Host ""
    Write-Host "Хоткеи (Windows): CTRL+ALT+A — подсказка, CTRL+ALT+X — по скриншоту,"
    Write-Host "                  CTRL+ALT+SHIFT+X — по области, CTRL+ALT+H — скрыть оверлей,"
    Write-Host "                  CTRL+ALT+O — передвинуть оверлей."
}
finally {
    Pop-Location
}
