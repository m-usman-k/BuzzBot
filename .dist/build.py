"""Builds .dist/BuzzBot.zip, a clean copy of the bot that can be sent to someone else.

Run from anywhere:  python .dist/build.py

The zip contains the code, fonts and wordlists, plus the files in .dist/template/
(setup guide, start scripts, .env.example). It never contains your .env, your
server data (*.json, *.db) or caches; the bot recreates its data files on first run.
"""
import os
import subprocess
import zipfile
from datetime import datetime, timezone

DIST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DIST_DIR)
TEMPLATE_DIR = os.path.join(DIST_DIR, 'template')
ZIP_PATH = os.path.join(DIST_DIR, 'BuzzBot.zip')
ZIP_ROOT = 'BuzzBot'  # Top-level folder inside the zip

ROOT_FILES = ['main.py', 'config.py', 'requirements.txt', 'README.md']
CODE_DIRS = ['cogs', 'utils']
DATA_EXTENSIONS = {'.ttf', '.otf', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
EXECUTABLE_FILES = {'start.sh'}


def collect_files():
    """Returns (source path, path inside the zip) pairs."""
    files = [(os.path.join(ROOT, name), name) for name in ROOT_FILES]

    for folder in CODE_DIRS:
        for name in sorted(os.listdir(os.path.join(ROOT, folder))):
            if name.endswith('.py'):
                files.append((os.path.join(ROOT, folder, name), f'{folder}/{name}'))

    for name in sorted(os.listdir(os.path.join(ROOT, 'data'))):
        if os.path.splitext(name)[1].lower() in DATA_EXTENSIONS:
            files.append((os.path.join(ROOT, 'data', name), f'data/{name}'))

    for name in sorted(os.listdir(TEMPLATE_DIR)):
        files.append((os.path.join(TEMPLATE_DIR, name), name))

    return files


def git_version():
    try:
        return subprocess.check_output(
            ['git', 'describe', '--always', '--dirty'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'


def build():
    files = collect_files()
    missing = [src for src, _ in files if not os.path.isfile(src)]
    if missing:
        raise SystemExit('Missing files:\n  ' + '\n  '.join(missing))

    version = f'{git_version()} built {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC'

    if os.path.exists(ZIP_PATH):
        os.remove(ZIP_PATH)

    with zipfile.ZipFile(ZIP_PATH, 'w', zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in files:
            info = zipfile.ZipInfo.from_file(src, f'{ZIP_ROOT}/{arcname}')
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(src, 'rb') as f:
                content = f.read()
            if arcname in EXECUTABLE_FILES:
                # Shell scripts break with Windows line endings and need the executable bit
                content = content.replace(b'\r\n', b'\n')
                info.external_attr = 0o100755 << 16
            zf.writestr(info, content)
        version_info = zipfile.ZipInfo(f'{ZIP_ROOT}/VERSION.txt', datetime.now().timetuple()[:6])
        version_info.compress_type = zipfile.ZIP_DEFLATED
        version_info.external_attr = 0o100644 << 16
        zf.writestr(version_info, version + '\n')

    size_kb = os.path.getsize(ZIP_PATH) / 1024
    print(f'Built {os.path.relpath(ZIP_PATH, ROOT)} ({len(files) + 1} files, {size_kb:.0f} KB)')
    print(f'Version: {version}')


if __name__ == '__main__':
    build()
