@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (set PY=E:\Anaconda\python.exe) else (set PY=python)

echo ============================================================
echo   文档问答 Agent - 交互提问
echo   输入问题后回车；输入 exit 退出
echo   语料目录: corpus
echo ============================================================
echo.

:loop
set "Q="
set /p Q=问题: 
if /i "%Q%"=="exit" goto end
if "%Q%"=="" goto loop
echo.
"%PY%" agent.py "%Q%" --corpus corpus --echo
echo.
echo ------------------------------------------------------------
goto loop

:end
echo 已退出。
pause
