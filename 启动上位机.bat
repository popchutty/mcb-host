@echo off
setlocal
if exist "%~dp0SCIScope.exe" (
    start "" "%~dp0SCIScope.exe" %*
    exit /b 0
)
if exist "%~dp0dist\SCIScope\SCIScope.exe" (
    start "" "%~dp0dist\SCIScope\SCIScope.exe" %*
    exit /b 0
)
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py" %*
    exit /b 0
)
echo Python environment not found. Run install.ps1 first, or download a release.
pause
exit /b 1
