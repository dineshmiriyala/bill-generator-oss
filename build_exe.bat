@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_NAME=BillGenerator_V4.5.1"
set "BUILD_VENV=.build-venv"
set "PYTHON_CMD="

echo =====================================================
echo   Building %APP_NAME%
echo =====================================================

call :resolve_python
if errorlevel 1 goto :fail

echo.
if not exist "%BUILD_VENV%\Scripts\python.exe" (
    echo Creating local build environment at %BUILD_VENV%...
    %PYTHON_CMD% -m venv "%BUILD_VENV%"
    if errorlevel 1 (
        echo Failed to create the local build environment.
        goto :fail
    )
) else (
    echo Reusing local build environment at %BUILD_VENV%...
)

echo.
echo Activating local build environment...
call "%BUILD_VENV%\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate %BUILD_VENV%.
    goto :fail
)

echo.
echo Installing or updating build tools...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo Failed to update pip tooling.
    goto :fail
)

echo.
echo Installing project dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install project dependencies.
    goto :fail
)

echo.
echo Installing PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo Failed to install PyInstaller.
    goto :fail
)

echo.
echo Cleaning old build folders...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

set "BG_DESKTOP=1"

echo.
echo Running PyInstaller build...
python -m PyInstaller --noconsole --onefile ^
--name "%APP_NAME%" ^
--add-data "templates;templates" ^
--add-data "static;static" ^
--add-data "db;db" ^
--hidden-import jinja2.ext ^
--hidden-import waitress ^
--version-file version.txt ^
desktop_launcher.py

if errorlevel 1 (
    echo Build failed.
    goto :fail
)

echo.
echo =====================================================
echo Build complete! Check the dist folder:
echo     %cd%\dist\%APP_NAME%.exe
echo =====================================================
pause
exit /b 0

:resolve_python
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    %PYTHON_CMD% --version >nul 2>nul
    if not errorlevel 1 exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    python --version >nul 2>nul
    if not errorlevel 1 exit /b 0
)

echo Python 3 was not found.
echo Install Python 3 for Windows first, then run this file again.
exit /b 1

:fail
echo.
echo Build stopped before completion.
pause
exit /b 1
