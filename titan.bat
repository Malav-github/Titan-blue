@echo off
rem Starts Titan: builds the app the first time, starts the server, and opens it in your browser.
rem   titan.bat          normal start
rem   titan.bat build    rebuild the app first (after changing anything in frontend\)
rem   titan.bat dev      also run the app's development server (instant edits) at http://127.0.0.1:5173/
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv is not installed. Install it from https://docs.astral.sh/uv/ and run this again.
  pause
  exit /b 1
)

set NEEDBUILD=0
if not exist frontend\dist\index.html set NEEDBUILD=1
if /i "%1"=="build" set NEEDBUILD=1
if "%NEEDBUILD%"=="1" (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo Node.js is needed once, to build the app. Install it with:  winget install OpenJS.NodeJS.LTS
    echo Then close this window and run titan.bat again.
    pause
    exit /b 1
  )
  echo Building the app...
  pushd frontend
  call npm install
  call npm run build
  if errorlevel 1 (
    popd
    echo The app failed to build. See the messages above.
    pause
    exit /b 1
  )
  popd
)

if /i "%1"=="dev" start "Titan app (development)" cmd /k "cd /d %~dp0frontend && npm run dev"

rem open the browser a few seconds from now, once the server is listening
if /i "%1"=="dev" (
  start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:5173/"
) else (
  start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8000/"
)

echo Titan is starting. Close this window, or press Ctrl+C, to stop it.
uv run python serve.py
