@echo off
cd /d "%~dp0"

if not exist .env (
    copy .env.example .env >nul
    echo Created .env - paste your bot token after BOT_TOKEN= then save and run start.bat again.
    notepad .env
    pause
    exit /b
)

if not exist venv (
    echo First run: setting up, this takes a minute...
    python -m venv venv
    if errorlevel 1 (
        echo Python 3.10 or newer is required. Get it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt
python main.py
pause
