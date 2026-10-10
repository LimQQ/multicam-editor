@echo off
setlocal
cd /d "%~dp0"

rem Use the interpreter that setup.bat actually installed the packages into.
set PY=
set PYARGS=
if exist "%~dp0python_path.txt" for /f "usebackq delims=" %%i in ("%~dp0python_path.txt") do set "PY=%%i"
if not defined PY goto :fallback

rem Prefer pythonw so no console window stays behind the app.
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" set "PY=%PYW%"
goto :launch

:fallback
where pythonw >nul 2>&1 && set PY=pythonw
if "%PY%"=="" where python >nul 2>&1 && set PY=python
if "%PY%"=="" where py >nul 2>&1 && (set PY=py) & (set PYARGS=-3)

:launch
if "%PY%"=="" (
  echo.
  echo [ERROR] Python not found.
  echo Please run setup.bat first.
  echo.
  pause
  exit /b 1
)

start "" "%PY%" %PYARGS% "%~dp0multicam_editor.py"
exit /b 0
