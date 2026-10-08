#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env - paste your bot token after BOT_TOKEN= then run ./start.sh again."
    exit 0
fi

if [ ! -d venv ]; then
    echo "First run: setting up, this takes a minute..."
    python3 -m venv venv || { echo "Python 3.10 or newer is required: https://www.python.org/downloads/"; exit 1; }
fi

. venv/bin/activate
python -m pip install -q --disable-pip-version-check -r requirements.txt
python main.py
