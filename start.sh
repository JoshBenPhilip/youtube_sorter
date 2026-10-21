#!/usr/bin/env bash
# One-step launcher for macOS / Linux: sets up a virtualenv on first run, then starts the app.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f .env ] && [ -z "$ANTHROPIC_API_KEY" ]; then
  read -rp "Paste your Anthropic API key: " key
  echo "ANTHROPIC_API_KEY=$key" > .env
fi
echo "Open http://127.0.0.1:${PORT:-5000} in your browser."
exec .venv/bin/python app.py
