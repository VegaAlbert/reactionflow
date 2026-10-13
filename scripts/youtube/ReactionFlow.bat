@echo off
rem Arranca desde la carpeta donde esta este .bat, este donde este el proyecto
rem (sirve igual en el PC que en el portatil).
cd /d "%~dp0"
python menu.py
pause
