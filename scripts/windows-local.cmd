@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0.."
if errorlevel 1 exit /b 1

if "%~1"=="start" goto check_docker
if "%~1"=="stop" goto check_docker
if "%~1"=="status" goto check_docker
echo ERROR: Use START-LOCAL.cmd, STOP-LOCAL.cmd or STATUS-LOCAL.cmd.
goto failed

:check_docker
where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: Install Docker Desktop, open it, then run this file again.
    echo See LOCAL-WINDOWS.md for the download link and instructions.
    goto failed
)
if defined DOCKER_HOST (
    echo ERROR: DOCKER_HOST is set. This launcher only supports local Docker Desktop.
    echo Open a terminal without DOCKER_HOST and select your local Linux engine.
    goto failed
)
rem Refuse SSH/TCP Docker contexts: this launcher must never target a remote host.
rem Parse CLI output as tokens instead of matching line endings through FINDSTR.
rem Keep stderr visible so a daemon/context failure retains its original message.
set "AGENTOS_DOCKER_ENDPOINT="
for /f "tokens=1" %%H in ('docker context inspect --format "{{.Endpoints.docker.Host}}"') do set "AGENTOS_DOCKER_ENDPOINT=%%H"
if /I not "%AGENTOS_DOCKER_ENDPOINT:~0,8%"=="npipe://" (
    echo ERROR: Select a local Docker Desktop context using a Windows named pipe.
    echo In Docker Desktop, use Linux containers. Do not use a remote Docker context.
    goto failed
)
set "AGENTOS_DOCKER_OS="
for /f "tokens=1" %%T in ('docker info --format "{{.OSType}}"') do set "AGENTOS_DOCKER_OS=%%T"
if not defined AGENTOS_DOCKER_OS (
    echo ERROR: Docker did not return its engine type. Read the Docker error above.
    echo Open Docker Desktop and wait for the engine to start, then try again.
    goto failed
)
if /I "%AGENTOS_DOCKER_OS%"=="windows" (
    echo ERROR: The active Docker engine uses Windows containers.
    echo Switch Docker Desktop to Linux containers and try again.
    goto failed
)
if /I not "%AGENTOS_DOCKER_OS%"=="linux" (
    echo ERROR: Docker returned an unexpected engine type.
    echo Run this in PowerShell and send its output:
    echo docker info --format "{{.OSType}}"
    goto failed
)
echo Docker check passed: local Linux engine.
docker compose version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker Compose is missing. Update Docker Desktop and try again.
    goto failed
)

rem Shell variables take precedence over --env-file in Compose. Use our file only.
set "ADMIN_USERNAME="
set "ADMIN_PASSWORD_HASH="
set "MASTER_KEY="
set "DEMO_MODE="

if "%~1"=="stop" goto stop
if "%~1"=="status" goto status

if exist ".env.local" goto build
docker volume inspect agentos_local_data >nul 2>&1
if not errorlevel 1 (
    echo ERROR: Local data already exists, but this folder has no .env.local.
    echo Return to the original project folder or restore its original .env.local.
    echo A new encryption key would make saved credentials unreadable.
    goto failed
)

:build
echo.
echo Building Agent OS. The first launch needs Internet and can take a few minutes.
docker build --tag agentos:local-test .
if errorlevel 1 goto failed
if exist ".env.local" goto start
echo.
echo First setup: choose an owner password of at least 16 characters.
echo Input is hidden: no dots or asterisks will appear. Default login: sergey
docker run --rm -it --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 0:0 --mount "type=bind,source=%CD%,target=/config" agentos:local-test python scripts/configure.py --demo --output /config/.env.local
if errorlevel 1 goto failed

:start
echo.
echo Starting Agent OS on this computer only...
docker compose --project-name agentos-local --env-file .env.local --file compose.local.yaml up --detach --wait --wait-timeout 90
if errorlevel 1 (
    echo ERROR: Startup failed. Port 8000 may be busy. Run STATUS-LOCAL.cmd for details.
    goto failed
)
echo.
echo Agent OS is ready: http://localhost:8000
echo Login: sergey, unless you changed ADMIN_USERNAME in .env.local.
echo Use the password you chose during first setup.
echo You may close this window. STOP-LOCAL.cmd stops the app and keeps its data.
start "" "http://localhost:8000"
goto success

:stop
if not exist ".env.local" goto missing_config
docker compose --project-name agentos-local --env-file .env.local --file compose.local.yaml stop --timeout 50
if errorlevel 1 goto failed
echo Stopped. Your tasks, settings and credentials are retained.
goto success

:status
if not exist ".env.local" goto missing_config
docker compose --project-name agentos-local --env-file .env.local --file compose.local.yaml ps --all
if errorlevel 1 goto failed
docker compose --project-name agentos-local --env-file .env.local --file compose.local.yaml logs --tail 60 app
if errorlevel 1 goto failed
goto success

:missing_config
echo ERROR: .env.local was not found. Use your original project folder.
echo For a new installation, run START-LOCAL.cmd first.
goto failed

:failed
echo.
echo No data was deleted. Read LOCAL-WINDOWS.md or send a screenshot of this error.
popd
exit /b 1

:success
popd
exit /b 0
