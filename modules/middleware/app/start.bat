@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM Omni-Core ERP & BI: zero-config Docker startup for Windows Docker Desktop.

cd /d "%~dp0"

docker version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Desktop is not installed or the Docker daemon is unavailable.
  echo         Install/start Docker Desktop and retry.
  exit /b 1
)

docker compose version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Compose v2 is required.
  exit /b 1
)

if not exist .env (
  powershell -NoProfile -Command "$bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); [Convert]::ToHexString($bytes).ToLower()" > .password.tmp
  set /p DB_PASSWORD=<.password.tmp
  del .password.tmp
  > .env (
    echo POSTGRES_USER=omnicore
    echo POSTGRES_PASSWORD=!DB_PASSWORD!
    echo POSTGRES_DB=omnicore
    echo OMNI_CORE_PORT=8000
    echo OMNIROUTE_URL=
    echo OMNIROUTE_API_KEY=
    echo MATTERHORN_API_TOKEN=
  )
  echo [INFO] Created .env with a generated PostgreSQL password.
)

echo [INFO] Building and starting Omni-Core...
docker compose up --build -d --remove-orphans
if errorlevel 1 exit /b 1

for /L %%I in (1,1,60) do (
  powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8000/api/health' -TimeoutSec 2; exit [int]($r.StatusCode -ne 200) } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto ready
  timeout /t 1 /nobreak >nul
)

echo [ERROR] Stack did not become healthy within 60 seconds.
docker compose ps
docker compose logs --tail=100 omni-core
exit /b 1

:ready
echo.
echo Omni-Core is ready.
echo   Dashboard: http://localhost:8000
echo   API docs:  http://localhost:8000/docs
echo   Logs:      docker compose logs -f omni-core
exit /b 0
