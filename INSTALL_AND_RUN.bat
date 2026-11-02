@echo off
chcp 65001 >nul
setlocal
title Cai dat - Truyen Nhat TXT Tool v0.9.4
cd /d "%~dp0"
echo =====================================================
echo TRUYEN NHAT TXT TOOL v0.9.4 - CAI DAT DOC LAP
echo =====================================================
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo [LOI] Chua co Python 3.11+ trong PATH.
        pause
        exit /b 1
    )
)
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 (echo [LOI] Khong tao duoc .venv & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (echo [LOI] Cai requirements that bai & pause & exit /b 1)
echo.
echo Hoan tat. Dang mo tool...
echo Neu Auto Web khong mo duoc Chrome/Edge, dung lenh:
echo   .venv\Scripts\python.exe -m playwright install chromium
".venv\Scripts\python.exe" truyen_nhat_txt_tool.py
if errorlevel 1 (echo [LOI] Tool dung dot ngot & pause)
endlocal
