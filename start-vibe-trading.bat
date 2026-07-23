@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "ROOT=%CD%"
set "FRONTEND_DIR=%ROOT%\frontend"
set "LOG_DIR=%ROOT%\.runtime"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "HOST=127.0.0.1"
set "BACKEND_PORT=8899"
set "FRONTEND_PORT=5899"
set "BACKEND_URL=http://%HOST%:%BACKEND_PORT%/health"
set "FRONTEND_URL=http://%HOST%:%FRONTEND_PORT%/"
set "PROXY_URL=http://%HOST%:%FRONTEND_PORT%/sessions"
set "OPEN_BROWSER=1"
set "PAUSE_AT_END=1"
set "EXIT_CODE=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-browser" set "OPEN_BROWSER=0"
if /i "%~1"=="--no-pause" set "PAUSE_AT_END=0"
shift
goto parse_args

:args_done
title Vibe-Trading Launcher
echo.
echo ========================================
echo   Vibe-Trading one-click launcher
echo ========================================
echo Project: %ROOT%
echo.

if not exist "%PYTHON%" (
    echo [ERROR] Python virtual environment not found:
    echo         %PYTHON%
    echo         Create the .venv environment and install backend dependencies first.
    set "EXIT_CODE=1"
    goto finish
)

if not exist "%POWERSHELL%" (
    echo [ERROR] Windows PowerShell was not found:
    echo         %POWERSHELL%
    set "EXIT_CODE=1"
    goto finish
)

if not exist "%FRONTEND_DIR%\package.json" (
    echo [ERROR] Frontend package.json not found:
    echo         %FRONTEND_DIR%\package.json
    set "EXIT_CODE=1"
    goto finish
)

if not exist "%FRONTEND_DIR%\node_modules\vite\bin\vite.js" (
    echo [ERROR] Frontend dependencies are not installed.
    echo         Run: cd /d "%FRONTEND_DIR%" ^&^& npm install
    set "EXIT_CODE=1"
    goto finish
)

where node.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] node.exe was not found in PATH. Install Node.js first.
    set "EXIT_CODE=1"
    goto finish
)

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo [ERROR] curl.exe was not found in PATH.
    set "EXIT_CODE=1"
    goto finish
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

echo [1/3] Checking backend on %HOST%:%BACKEND_PORT% ...
call :is_healthy "%BACKEND_URL%"
if not errorlevel 1 (
    echo       Backend is already healthy.
) else (
    call :is_listening %BACKEND_PORT%
    if not errorlevel 1 (
        echo [ERROR] Port %BACKEND_PORT% is occupied, but the backend health check failed.
        echo         Stop the conflicting process and run this launcher again.
        set "EXIT_CODE=1"
        goto finish
    )

    echo       Starting backend ...
    "%POWERSHELL%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList '-m','cli','serve','--host','%HOST%','--port','%BACKEND_PORT%' -WorkingDirectory '%ROOT%' -WindowStyle Hidden -RedirectStandardOutput '%LOG_DIR%\backend.out.log' -RedirectStandardError '%LOG_DIR%\backend.err.log'"
    if errorlevel 1 (
        echo [ERROR] Failed to create the backend process.
        set "EXIT_CODE=1"
        goto finish
    )
    call :wait_healthy "%BACKEND_URL%" 180
    if errorlevel 1 (
        echo [ERROR] Backend did not become healthy within 180 seconds.
        echo         Check %LOG_DIR%\backend.err.log for details.
        set "EXIT_CODE=1"
        goto finish
    )
    echo       Backend is healthy.
)

echo [2/3] Checking frontend on %HOST%:%FRONTEND_PORT% ...
call :is_healthy "%FRONTEND_URL%"
if not errorlevel 1 (
    echo       Frontend is already healthy.
) else (
    call :is_listening %FRONTEND_PORT%
    if not errorlevel 1 (
        echo [ERROR] Port %FRONTEND_PORT% is occupied, but the frontend health check failed.
        echo         Stop the conflicting process and run this launcher again.
        set "EXIT_CODE=1"
        goto finish
    )

    echo       Starting frontend ...
    "%POWERSHELL%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'node.exe' -ArgumentList 'node_modules\vite\bin\vite.js','--host','%HOST%','--port','%FRONTEND_PORT%' -WorkingDirectory '%FRONTEND_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%LOG_DIR%\frontend.out.log' -RedirectStandardError '%LOG_DIR%\frontend.err.log'"
    if errorlevel 1 (
        echo [ERROR] Failed to create the frontend process.
        set "EXIT_CODE=1"
        goto finish
    )
    call :wait_healthy "%FRONTEND_URL%" 90
    if errorlevel 1 (
        echo [ERROR] Frontend did not become healthy within 90 seconds.
        echo         Check %LOG_DIR%\frontend.err.log for details.
        set "EXIT_CODE=1"
        goto finish
    )
    echo       Frontend is healthy.
)

echo [3/3] Checking frontend-to-backend proxy ...
call :wait_healthy "%PROXY_URL%" 20
if errorlevel 1 (
    echo [ERROR] Frontend proxy check failed: %PROXY_URL%
    set "EXIT_CODE=1"
    goto finish
)
echo       Proxy is healthy.
echo.
echo Vibe-Trading is ready:
echo   Frontend: %FRONTEND_URL%
echo   Backend:  %BACKEND_URL%
echo   Logs:     %LOG_DIR%

if "%OPEN_BROWSER%"=="1" start "" "%FRONTEND_URL%"

:finish
echo.
if "%EXIT_CODE%"=="0" (
    echo Launcher completed successfully.
) else (
    echo Launcher failed with exit code %EXIT_CODE%.
    if "%PAUSE_AT_END%"=="1" pause
)
exit /b %EXIT_CODE%

:is_healthy
curl.exe --fail --silent --show-error --max-time 10 "%~1" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:is_listening
"%POWERSHELL%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:wait_healthy
set "WAIT_URL=%~1"
set "WAIT_SECONDS=%~2"
for /l %%I in (1,1,%WAIT_SECONDS%) do (
    call :is_healthy "%WAIT_URL%"
    if not errorlevel 1 exit /b 0
    timeout /t 1 /nobreak >nul 2>&1
)
exit /b 1
