@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "RESTORE_SCRIPT=%~dp0output\windows_migration_20261030\RESTORE_FULL_WINDOWS.ps1"
set "DOWNLOAD_DIR=%~dp0output\windows_migration_20261030\downloaded_delta_parts"

if not exist "%RESTORE_SCRIPT%" (
  echo 未找到迁移恢复脚本：%RESTORE_SCRIPT%
  pause
  exit /b 1
)

echo 此入口只下载、校验并恢复迁移文件，不会启动实验或调用模型。
echo 请确认目标磁盘有足够空间，并已安装 zstd.exe。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RESTORE_SCRIPT%" -Destination "%~dp0" -DownloadDirectory "%DOWNLOAD_DIR%"
set "RESTORE_RESULT=%ERRORLEVEL%"
echo.
if "%RESTORE_RESULT%"=="0" (
  echo Windows 迁移文件已恢复并通过归档校验；本入口未启动实验。
) else (
  echo 恢复未完成，退出码：%RESTORE_RESULT%。可保留已下载分卷后重新运行。
)
pause
exit /b %RESTORE_RESULT%
