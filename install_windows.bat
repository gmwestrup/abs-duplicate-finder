@echo off
cd /d "%~dp0"
echo Installing packages for ABS Duplicate Finder...
py -m pip install --upgrade -r requirements.txt
if errorlevel 1 python -m pip install --upgrade -r requirements.txt
echo.
echo Done. Double-click abs_dupes.pyw to start.
pause
