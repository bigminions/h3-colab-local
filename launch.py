"""Double-click launcher. Reuses an already running local workbench."""
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.error import URLError
import webbrowser

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / 'log'
URL = 'http://127.0.0.1:7865'


def running():
    try:
        with urlopen(URL, timeout=1) as response:
            html = response.read().decode('utf-8')
            if '<meta name="application-name" content="LStudio">' not in html:
                raise RuntimeError('Port 7865 is occupied by another application.')
            return True
    except URLError:
        return False


if __name__ == '__main__':
    if not running():
        LOG_DIR.mkdir(exist_ok=True)
        stdout_log = LOG_DIR / 'server.log'
        stderr_log = LOG_DIR / 'server-error.log'
        with stdout_log.open('a', encoding='utf-8') as stdout, stderr_log.open('a', encoding='utf-8') as stderr:
            process = subprocess.Popen([sys.executable, '-u', str(BASE / 'app.py')], cwd=BASE,
                stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        for _ in range(120):
            if running():
                break
            if process.poll() is not None:
                raise RuntimeError('Server failed to start. See log/server-error.log.')
            time.sleep(.25)
        else:
            raise RuntimeError('Server startup timed out. See log/server-error.log.')
    webbrowser.open(URL)
