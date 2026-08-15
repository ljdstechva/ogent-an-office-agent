@echo off
setlocal

set "OGENT_SCRIPT=%~dp0ogent.py"

if /I "%~1"=="stop" (
    py -3 "%OGENT_SCRIPT%" --stop
    exit /b %errorlevel%
)

if /I "%~1"=="agent" goto agent_mode

if not "%~1"=="" (
    where pyw.exe >nul 2>&1
    if not errorlevel 1 (
        start "" pyw.exe -3 "%OGENT_SCRIPT%" --open "%~1"
        exit /b 0
    )

    where py.exe >nul 2>&1
    if not errorlevel 1 (
        start "Ogent Lite" /min py.exe -3 "%OGENT_SCRIPT%" --open "%~1"
        exit /b 0
    )

    echo Ogent Lite requires Python 3. Install Python, then confirm "py -3 --version" works.
    exit /b 1
)

where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pyw.exe -3 "%OGENT_SCRIPT%"
    exit /b 0
)

where py.exe >nul 2>&1
if not errorlevel 1 (
    start "Ogent Lite" /min py.exe -3 "%OGENT_SCRIPT%"
    exit /b 0
)

echo Ogent Lite requires Python 3. Install Python, then confirm "py -3 --version" works.
exit /b 1

:agent_mode
rem Interactive workspace picker: runs in this console for the prompt.
if "%~2"=="" (
    py -3 "%OGENT_SCRIPT%" --agent
) else (
    py -3 "%OGENT_SCRIPT%" --agent "%~2"
)
exit /b %errorlevel%
