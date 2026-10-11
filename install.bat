@echo off
rem Double-click to add Launchpad to the Start menu and Settings > Apps.
rem To remove it: uninstall it from Settings > Apps, or run: python launchpad.py uninstall
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python launchpad.py install %*) else (py -3 launchpad.py install %*)
echo.
pause
