@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" truyen_nhat_txt_tool.py
) else (
    echo Chua cai dat - hay mo INSTALL_AND_RUN.bat truoc.
    pause
)
