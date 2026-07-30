@echo off
REM ---- One-time setup.  ASCII only: cmd.exe reads .bat in the system codepage,
REM ---- so non-ASCII comments here get mangled and can break the lines themselves.
cd /d "%~dp0"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH.  Install 3.10+ from https://python.org
    echo Tick "Add python.exe to PATH" in the installer.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    python -m venv .venv || (echo venv creation failed & pause & exit /b 1)
)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (
    echo pip install failed & pause & exit /b 1
)

REM Never clobber a .env that already holds a key.
if not exist ".env" copy /y ".env.example" ".env" >nul

REM Offline, keyless smoke test: proves the install works before you spend a key on it.
".venv\Scripts\python.exe" agent.py --selfcheck || (echo selfcheck failed & pause & exit /b 1)

echo.
echo Done.  Next: open .env, paste your API key, save.
echo Then double-click talos.bat
pause
