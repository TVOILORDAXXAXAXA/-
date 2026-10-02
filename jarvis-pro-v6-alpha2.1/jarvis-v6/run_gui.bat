@echo off
cd /d "%~dp0"
py jarvis_gui.py
if errorlevel 1 echo Джарвис GUI завершился с ошибкой.
echo.
pause
