@echo off
REM Quick build of just the executable. For a full release (installer, zip,
REM checksums) use build_release.ps1 instead.
cd /d "%~dp0"
echo Building AutoXtract.exe ...
if not exist auto_extract_gui.py (echo auto_extract_gui.py not found in this folder & pause & exit /b 1)
if not exist app_icon.ico (echo app_icon.ico not found in this folder & pause & exit /b 1)
python -m PyInstaller --clean --noconfirm --onefile --noconsole --name=AutoXtract --icon=app_icon.ico --add-data=app_icon.ico:. --add-data=assets\icon-48.png:assets --add-data=VERSION:. auto_extract_gui.py
if errorlevel 1 (echo. & echo BUILD FAILED - see the messages above & pause & exit /b 1)
echo.
echo Done! Your program is here: %~dp0dist\AutoXtract.exe
pause
