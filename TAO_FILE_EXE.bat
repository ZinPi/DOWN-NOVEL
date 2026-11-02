@echo off
chcp 65001 >nul
title Tao file EXE - Truyen Nhat TXT Tool
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    echo Chay INSTALL_AND_RUN.bat truoc de tao moi truong .venv.
    pause
    exit /b 1
)

echo Dang cai PyInstaller va thu vien...
%PY% -m pip install pyinstaller requests beautifulsoup4 playwright
if errorlevel 1 (
    echo [LOI] Khong cai duoc thu vien build.
    pause
    exit /b 1
)

echo.
echo Dang tao EXE...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all playwright --name Truyen_Nhat_TXT_Tool truyen_nhat_txt_tool.py
if errorlevel 1 (
    echo [LOI] Tao EXE that bai.
    pause
    exit /b 1
)

echo.
echo HOAN TAT.
echo File EXE nam trong thu muc: dist\Truyen_Nhat_TXT_Tool.exe
echo GameLive van can Chrome/Edge cai san tren may.
pause
