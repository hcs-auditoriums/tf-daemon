@echo off
rem Dev container shortcuts (Windows; ./dev is the macOS / Linux twin). Each starts it if needed.
rem   dev shell            bash in the container
rem   dev test [ARGS]      all tests in parallel; ARGS go to pytest (dev test -k reconnects)
rem   dev logs             follow the server log
rem   dev up ^| down        start (rebuilding if needed) / stop
rem   dev open [PAGE]      open the running app in the browser (dev open rf)
rem   dev help             list these commands
rem From PowerShell, type .\dev instead of dev.
setlocal
cd /d "%~dp0"
set "CMD=%~1"
if "%CMD%"=="" goto help
shift
set "ARGS="
:collect
if "%~1"=="" goto run
set ARGS=%ARGS% %1
shift
goto collect

:run
if /i "%CMD%"=="up"    (docker compose up -d --build & goto :eof)
if /i "%CMD%"=="down"  (docker compose down & goto :eof)
if /i "%CMD%"=="shell" (docker compose up -d && docker compose exec app bash & goto :eof)
if /i "%CMD%"=="test"  (docker compose up -d && docker compose exec app pytest%ARGS% & goto :eof)
if /i "%CMD%"=="logs"  (docker compose up -d && docker compose logs -f app & goto :eof)
if /i "%CMD%"=="open"  goto open
if /i "%CMD%"=="help"  goto help
if /i "%CMD%"=="-h"    goto help
if /i "%CMD%"=="--help" goto help
if /i "%CMD%"=="/?"    goto help

echo unknown command: %CMD% 1>&2
echo. 1>&2
call :usage 1>&2
exit /b 2

:open
docker compose up -d || exit /b 1
rem curl.exe ships with Windows 10 and later.
<nul set /p "=waiting for the server"
for /l %%i in (1,1,30) do (
  curl -fsS -o nul http://localhost:8080/api/status 2>nul && goto opennow
  <nul set /p "=."
  timeout /t 1 /nobreak >nul
)
:opennow
echo.
set "PAGE="
if defined ARGS set "PAGE=%ARGS:~1%"
if defined PAGE if "%PAGE:~0,1%"=="/" set "PAGE=%PAGE:~1%"
echo http://localhost:8080/%PAGE%
start "" "http://localhost:8080/%PAGE%"
exit /b 0

:help
call :usage
exit /b 0

:usage
echo Dev container shortcuts (each starts the container if it isn't running):
echo.
echo   dev shell            bash in the container
echo   dev test [ARGS]      all tests, in parallel; ARGS go to pytest
echo                        e.g. dev test tests/test_tf5.py -k reconnects
echo                             dev test -n 0 --pdb     (serial, debugger)
echo   dev logs             follow the server log (Ctrl+C to stop following)
echo   dev open [PAGE]      open the running app in the browser
echo                        PAGE: rf ^| qlxd ^| config (default: the audio page)
echo   dev up               start, rebuilding the image if pyproject.toml changed
echo   dev down             stop
echo   dev help             this list
echo.
echo From PowerShell, type .\dev instead of dev.
echo Server: http://localhost:8080. It reloads when you save a file.
goto :eof
