@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  set "PY_CMD=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo 未找到 Python。请先安装 Python 3.9 或更高版本，并勾选 Add Python to PATH。
    pause
    exit /b 1
  )
  set "PY_CMD=python"
)

echo 正在检查并准备项目虚拟环境……
%PY_CMD% setup_env.py --locked
if errorlevel 1 goto :failed

:menu
echo.
echo ===== Windows 实验入口 =====
echo 1. 路线层传统基线（seed 0，100x100，离线）
echo 2. 调度层 D0/D1 基线（seed 0，离线）
echo 0. 退出
set "CHOICE="
set /p "CHOICE=请选择要启动的实验: "
if "%CHOICE%"=="1" goto :route
if "%CHOICE%"=="2" goto :dispatch
if "%CHOICE%"=="0" exit /b 0
goto :menu

:route
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_ID=%%I"
if not defined RUN_ID set "RUN_ID=route_seed0"
echo 启动路线层离线基线；结果写入 outputs\windows_runs\traditional_%RUN_ID%
".venv\Scripts\python.exe" scripts\run_traditional_experiment.py --seeds 0 --size 100 --output-dir "outputs\windows_runs\traditional_%RUN_ID%"
set "RUN_RESULT=%ERRORLEVEL%"
goto :finished

:dispatch
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_ID=%%I"
if not defined RUN_ID set "RUN_ID=dispatch_seed0"
echo 启动调度层 D0/D1 离线基线；结果写入 outputs\windows_runs\dispatch_%RUN_ID%
".venv\Scripts\python.exe" scripts\run_dispatch_matrix.py --seeds 0 --methods D0_greedy D1_optimizer --feedback-mode none --output-dir "outputs\windows_runs\dispatch_%RUN_ID%"
set "RUN_RESULT=%ERRORLEVEL%"
goto :finished

:finished
echo.
if "%RUN_RESULT%"=="0" (
  echo 实验进程已正常结束。请检查输出目录中的 manifest 和结果文件。
) else (
  echo 实验进程退出码：%RUN_RESULT%。请保存此窗口中的错误信息。
)
pause
exit /b %RUN_RESULT%

:failed
echo 环境准备失败。请保留错误信息并检查 Python 版本及网络连接。
pause
exit /b 1
