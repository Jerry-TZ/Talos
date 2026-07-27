@echo off
REM 双击即用。所有配置都在这里改,改完保存就行,不用碰 PowerShell。
cd /d "%~dp0"

REM 工作目录:Talos 只能读写这个目录里的文件。留空 = 就在 talos 自己的目录里干活。
set "TALOS_WORKSPACE="

REM 模型:glm-5.2(最强) / glm-4.6(便宜一半) / glm-4.5-air(额度最多) / glm-4.7-flash(免费但限流)
set "TALOS_MODEL=glm-5.2"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" agent.py %*
) else (
    echo 没找到 .venv,先建虚拟环境:python -m venv .venv 然后 .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
pause
