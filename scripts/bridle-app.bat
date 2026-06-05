@echo off
REM ============================================================
REM  Bridle App Launcher
REM  - Starts backend (bridle serve) in its own console window
REM  - Starts frontend (npm run dev) in its own console window
REM  - Waits for frontend port 5173 to become available
REM  - Opens Chrome in --app mode pointing at http://localhost:5173
REM  - When the Chrome app window is closed, kills backend + frontend
REM ============================================================

setlocal

set "PROJECT_ROOT=D:\Bridle"
set "VENV_PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "FRONTEND_DIR=%PROJECT_ROOT%\frontend"
set "APP_URL=http://localhost:5173"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "LOG_DIR=%PROJECT_ROOT%\scripts\.app-logs"
set "BACKEND_TITLE=Bridle Backend"
set "FRONTEND_TITLE=Bridle Frontend"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\launcher.log"

echo [%date% %time%] === Bridle launcher start === >> "%LOG_FILE%"

REM --- sanity checks ---
if not exist "%VENV_PY%" (
    echo [ERROR] venv python not found: %VENV_PY%
    echo [%date% %time%] missing venv python >> "%LOG_FILE%"
    pause & exit /b 1
)
if not exist "%FRONTEND_DIR%\package.json" (
    echo [ERROR] frontend dir not found: %FRONTEND_DIR%
    echo [%date% %time%] missing frontend dir >> "%LOG_FILE%"
    pause & exit /b 1
)
if not exist "%CHROME%" (
    echo [ERROR] Chrome not found: %CHROME%
    echo [%date% %time%] missing chrome.exe >> "%LOG_FILE%"
    pause & exit /b 1
)

REM --- start backend in its own window ---
echo [%date% %time%] starting backend... >> "%LOG_FILE%"
start "%BACKEND_TITLE%" cmd /k "cd /d %PROJECT_ROOT% && %VENV_PY% -m bridle serve"

REM --- start frontend in its own window ---
echo [%date% %time%] starting frontend... >> "%LOG_FILE%"
start "%FRONTEND_TITLE%" cmd /k "cd /d %FRONTEND_DIR% && npm run dev"

REM --- wait until frontend port 5173 responds (max ~180s for cold start) ---
echo Waiting for frontend on %APP_URL% ...
echo (first cold start can take 1-2 min while Vite pre-bundles deps)
set /a WAIT_LEFT=180
set /a TICK=0
:WAIT_LOOP
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -Uri '%APP_URL%' -TimeoutSec 1).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto READY
set /a WAIT_LEFT-=1
set /a TICK+=1
REM print a heartbeat every 5s so the window doesn't look frozen
set /a MOD=TICK %% 5
if %MOD%==0 echo   ... still waiting, %WAIT_LEFT%s left
if %WAIT_LEFT% LEQ 0 (
    echo.
    echo [ERROR] frontend did not come up within 180s.
    echo Check the "%FRONTEND_TITLE%" window for npm/Vite errors.
    echo Also check if Vite picked a different port ^(strictPort is false^).
    echo [%date% %time%] frontend timeout >> "%LOG_FILE%"
    echo.
    pause
    goto CLEANUP
)
timeout /t 1 /nobreak >nul
goto WAIT_LOOP

:READY
echo [%date% %time%] frontend up, launching Chrome --app >> "%LOG_FILE%"

REM --- launch Chrome in app mode (blocks until window closes) ---
REM Use a dedicated user-data-dir so this window is independent of your normal Chrome session.
set "CHROME_PROFILE=%LOG_DIR%\chrome-profile"
"%CHROME%" --app=%APP_URL% --user-data-dir="%CHROME_PROFILE%"

echo [%date% %time%] Chrome window closed, shutting down backend + frontend >> "%LOG_FILE%"

:CLEANUP
REM Close backend + frontend console windows by their titles.
taskkill /FI "WINDOWTITLE eq %BACKEND_TITLE%*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq %FRONTEND_TITLE%*" /T /F >nul 2>&1
echo [%date% %time%] === Bridle launcher exit === >> "%LOG_FILE%"

endlocal
exit /b 0
