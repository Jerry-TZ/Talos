@echo off
REM ---- Talos launcher.  ASCII only: cmd.exe reads .bat in the system codepage,
REM ---- so non-ASCII comments here get mangled and can break the lines themselves.
cd /d "%~dp0"

REM Workspace: the only directory Talos may read or write.  Point it anywhere
REM (set "TALOS_WORKSPACE=D:\my_project").  Leaving it EMPTY means this folder,
REM which lets the agent edit its own source -- fine while hacking on Talos,
REM surprising otherwise.  Default is a subfolder, so the two stay separate.
set "TALOS_WORKSPACE=%~dp0workspace"
if not exist "%TALOS_WORKSPACE%" mkdir "%TALOS_WORKSPACE%"

REM Model.  Leave EMPTY so .env decides -- a value here is a real env var and
REM .env uses setdefault, so anything set below silently wins over .env and you
REM get to wonder why editing .env changed nothing.  Set it here only to pin
REM one launcher to one model.  glm-5.2 | glm-4.6 | glm-4.5-air | glm-4.7-flash
set "TALOS_MODEL="

REM Run after every .py edit, so a break is reported by the step that caused it.
REM Empty = off. Only this line can set it; an installed skill cannot.
set "TALOS_AUTOTEST="

REM Auto-commit each edited file after it passes (needs a git repo). Empty/0 = off.
REM Commits ONE file per edit, never `git add -A`.
set "TALOS_AUTOCOMMIT="

chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Run:  python -m venv .venv
    echo then:  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" agent.py %*
pause
