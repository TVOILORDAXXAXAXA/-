@echo off
cd /d "%~dp0"
py jarvis.py
if errorlevel 1 echo Джарвис завершился с ошибкой.
echo.
pause
