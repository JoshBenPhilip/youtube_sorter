@echo off
rem One-step launcher for Windows: sets up a virtualenv on first run, then starts the app.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
if not exist .env if "%ANTHROPIC_API_KEY%"=="" (
  set /p key="Paste your Anthropic API key: "
  call echo ANTHROPIC_API_KEY=%%key%%> .env
)
echo Open http://127.0.0.1:5000 in your browser.
.venv\Scripts\python app.py
