@echo off
cd /d "%~dp0"
py -m pip install pyinstaller
if errorlevel 1 goto error
py -m PyInstaller --noconfirm --clean --onedir --windowed --name Jarvis jarvis_gui.py
if errorlevel 1 goto error
if exist "vosk-model-small-ru-0.22" xcopy "vosk-model-small-ru-0.22" "dist\Jarvis\vosk-model-small-ru-0.22" /E /I /Y >nul
echo.
echo Build ready in dist\Jarvis\Jarvis.exe
pause
exit /b 0
:error
echo Build failed. Check the messages above.
pause
exit /b 1
