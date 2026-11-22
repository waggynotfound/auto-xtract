@echo off
cd /d "%~dp0"
echo Building AutoExtract.exe ...
if not exist auto_extract_gui.py (echo auto_extract_gui.py not found in this folder & pause & exit /b 1)
if not exist app_icon.ico (echo app_icon.ico not found in this folder & pause & exit /b 1)
python -m PyInstaller --clean --noconfirm --onefile --noconsole --name=AutoExtract --icon=app_icon.ico --add-data=app_icon.ico:. auto_extract_gui.py
if errorlevel 1 (echo. & echo BUILD FAILED - see the messages above & pause & exit /b 1)
echo.
echo Done! Your program is here: %~dp0dist\AutoExtract.exe
pause
