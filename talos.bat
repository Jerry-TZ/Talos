@echo off
REM ---- Talos launcher.  ASCII only: cmd.exe reads .bat in the system codepage,
REM ---- so non-ASCII comments here get mangled and can break the lines themselves.
cd /d "%~dp0"

REM Workspace: the only directory Talos may read or write. Empty = this folder.
set "TALOS_WORKSPACE="

REM Model: glm-5.2 | glm-4.6 | glm-4.5-air | glm-4.7-flash
set "TALOS_MODEL=glm-5.2"

REM Run after every .py edit, so a break is reported by the step that caused it.
REM Empty = off. Only this line can set it; an installed skill cannot.
set "TALOS_AUTOTEST="

chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    echo .venv not found. Run:  python -m venv .venv
    echo then:  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" agent.py %*
pause
