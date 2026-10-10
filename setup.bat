@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   Multicam Editor - Setup
echo ============================================
echo.

echo [1/4] Checking Python...

rem Find a Python that ACTUALLY runs. PATH entries can be broken stubs or
rem Store aliases, so every candidate is tested by making it write its own
rem sys.executable to python_path.txt. Whatever succeeds is what we use,
rem and run.bat reads the same file so it uses the exact same interpreter.
set PYEXE=
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try_python "%%D\python.exe"
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do call :try_python "%%D\python.exe"
for /d %%D in ("%ProgramFiles%\Python3*") do call :try_python "%%D\python.exe"
for /d %%D in ("C:\Python3*") do call :try_python "%%D\python.exe"
if not defined PYEXE call :try_python py
if not defined PYEXE call :try_python python

if not defined PYEXE (
  echo   No working Python found. Installing via winget...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo   Python installed. Please CLOSE this window and run setup.bat again.
  pause
  exit /b 0
)
echo   Using: %PYEXE%
"%PYEXE%" --version
echo.

echo [2/4] Installing Python packages ^(numpy, pillow, sounddevice^)...
"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install --upgrade numpy pillow sounddevice
echo.

echo [3/4] Verifying packages...
"%PYEXE%" -c "import numpy, PIL, sounddevice; print('  numpy', numpy.__version__); print('  pillow', PIL.__version__); print('  sounddevice', sounddevice.__version__)"
if errorlevel 1 (
  echo.
  echo   [ERROR] Some packages failed to install.
  echo   Scroll up to see the pip error, then try running this by hand:
  echo     "%PYEXE%" -m pip install numpy pillow sounddevice
  echo.
  pause
  exit /b 1
)
echo.

echo [4/4] Checking ffmpeg...
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo   ffmpeg not found. Installing via winget...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo   ffmpeg installed - close this window so Windows picks up the new PATH.
) else (
  echo   ffmpeg OK
)
echo.

echo ============================================
echo   Setup done. Close this window, then
echo   double-click run.bat to start.
echo ============================================
pause
exit /b 0

rem ---------------------------------------------------------------
rem :try_python  <command-or-path>
rem Sets PYEXE to the real sys.executable if the candidate works.
rem ---------------------------------------------------------------
:try_python
if defined PYEXE exit /b 0
if exist "%~dp0python_path.txt" del "%~dp0python_path.txt" >nul 2>&1
%1 -c "import sys; open('python_path.txt','w').write(sys.executable)" >nul 2>&1
if not exist "%~dp0python_path.txt" exit /b 0
for /f "usebackq delims=" %%i in ("%~dp0python_path.txt") do set "PYEXE=%%i"
exit /b 0
