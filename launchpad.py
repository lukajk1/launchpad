"""
Launchpad: one place to start, stop and open local projects.

    python launchpad.py                 # opens the window (or double-click launchpad.pyw)
    python launchpad.py --browser       # use your normal browser instead
    python launchpad.py add C:\\path\\to\\project [--name N] [--cmd "..."] [--url http://...]
    python launchpad.py list

Projects live in projects.json next to this file (hand-editable). Each has a folder, a start
command, and an optional URL: projects with a URL are web apps (Open starts them if needed and
opens the page in your browser); projects without one are desktop apps (Start just launches them).

Launched processes outlive the Launchpad window. Their PIDs and logs are kept in
%LOCALAPPDATA%\\launchpad, so a restarted Launchpad still knows what it started.
"""
import os
import re
import sys
import html
import json
import time
import shlex
import socket
import zlib
import argparse
import threading
import subprocess
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(APP_DIR, 'ui')
REGISTRY = os.environ.get('LAUNCHPAD_REGISTRY', os.path.join(APP_DIR, 'projects.json'))
HOME = os.environ.get('LAUNCHPAD_HOME') or os.path.join(
    os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'launchpad')
STATE = os.path.join(HOME, 'state.json')
LOG_DIR = os.path.join(HOME, 'logs')
DEFAULT_PORT = 4700
WINDOWS = sys.platform == 'win32'
ICON_ICO = os.path.join(APP_DIR, 'assets', 'icon.ico')  # drawn by assets/make_icon.py
APP_ID = 'lukajk.launchpad'

lock = threading.RLock()  # guards registry and state files


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Registry (projects.json) and runtime state (PIDs of processes we started)

def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def load_projects():
    return _read_json(REGISTRY, {'projects': []})['projects']


def save_projects(projects):
    _write_json(REGISTRY, {'projects': projects})


def get_project(project_id):
    for p in load_projects():
        if p['id'] == project_id:
            return p
    raise ApiError(f'no project {project_id!r}', 404)


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'project'


def add_project(path, name=None, command=None, url=None):
    """Register a folder. Missing command/url/name are filled in by detect().

    A project needs neither: with only a URL (a static page) Open just opens it, and with
    neither it's a folder you can open from the list.
    """
    path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isdir(path):
        raise ApiError(f'not a folder: {path}')
    guess = (detect(path) or [{}])[0]
    command = (command or guess.get('command') or '').strip()
    project = {
        'id': '',
        'name': (name or guess.get('name') or os.path.basename(path)).strip(),
        'path': path,
        'command': command,
        'url': (url if url is not None else guess.get('url') or '').strip(),
    }
    with lock:
        projects = load_projects()
        taken = {p['id'] for p in projects}
        base = slugify(project['name'])
        project['id'] = next(f'{base}-{n}' if n else base for n in range(100)
                             if (f'{base}-{n}' if n else base) not in taken)
        projects.append(project)
        save_projects(projects)
    return project


def update_project(project_id, changes):
    with lock:
        projects = load_projects()
        for p in projects:
            if p['id'] == project_id:
                for key in ('name', 'path', 'command', 'url'):
                    if key in changes:
                        p[key] = str(changes[key]).strip()
                save_projects(projects)
                return p
    raise ApiError(f'no project {project_id!r}', 404)


def remove_project(project_id):
    with lock:
        projects = load_projects()
        kept = [p for p in projects if p['id'] != project_id]
        if len(kept) == len(projects):
            raise ApiError(f'no project {project_id!r}', 404)
        save_projects(kept)


def load_state():
    return _read_json(STATE, {})


def save_state(state):
    _write_json(STATE, state)


# ---------------------------------------------------------------------------
# Detecting how to run a folder

def detect(path):
    """Ways to run the project in `path`, best first: [{name, command, url, source}]."""
    found = []
    name = os.path.basename(os.path.abspath(path))

    launch = _read_json(os.path.join(path, '.claude', 'launch.json'), None)
    for cfg in (launch or {}).get('configurations', []):
        exe = cfg.get('runtimeExecutable')
        if not exe:
            continue
        command = ' '.join([exe, *(_quote(a) for a in cfg.get('runtimeArgs', []))])
        url = cfg.get('url') or (f"http://127.0.0.1:{cfg['port']}" if cfg.get('port') else '')
        found.append({'name': cfg.get('name') or name, 'command': command, 'url': url,
                      'source': '.claude/launch.json'})

    package = _read_json(os.path.join(path, 'package.json'), None)
    scripts = (package or {}).get('scripts', {})
    for script, port in (('dev', 3000), ('start', 3000)):
        if script in scripts:
            found.append({'name': (package or {}).get('name') or name,
                          'command': f'npm run {script}', 'url': f'http://localhost:{port}',
                          'source': f'package.json "{script}" (port is a guess)'})
            break

    for entry in sorted(os.listdir(path)):
        if entry.lower().endswith('.pyw'):
            found.append({'name': name, 'command': f'pythonw {_quote(entry)}', 'url': '',
                          'source': f'{entry} (desktop app)'})

    index = os.path.join(path, 'index.html')
    if os.path.isfile(index):
        found.append({'name': name, 'command': '', 'url': file_url(index),
                      'source': 'index.html (static page, no server)'})
    return found


# Each project can describe itself in its own folder, in .launchpad.json:
#   {"icon": "assets/icon.png", "description": "Graded Chinese stories with a word list"}
# The icon path is relative to the project folder (an absolute path works too).
MANIFEST = '.launchpad.json'
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
               '.ico': 'image/x-icon'}


def manifest(project):
    try:
        data = _read_json(os.path.join(project['path'], MANIFEST), {})
    except (OSError, ValueError):
        return {}  # unreadable or half-written; treat it as missing
    return data if isinstance(data, dict) else {}


def icon_file(project, info=None):
    rel = (info if info is not None else manifest(project)).get('icon')
    if not isinstance(rel, str) or not rel.strip():
        return None
    path = os.path.normpath(os.path.join(project['path'], os.path.expanduser(rel.strip())))
    if os.path.splitext(path)[1].lower() in IMAGE_TYPES and os.path.isfile(path):
        return path
    return None


def default_icon(project):
    """Stand-in for projects without an icon: the name's first letter on a tile whose hue comes
    from the name, so each project keeps its own colour."""
    name = project.get('name') or project.get('id') or '?'
    letter = next((c.upper() for c in name if c.isalnum()), '?')
    hue = zlib.crc32(name.encode()) % 360
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="hsl({hue},38%,52%)"/><stop offset="1" stop-color="hsl({hue},42%,40%)"/>
</linearGradient></defs>
<rect width="64" height="64" rx="15" fill="url(#g)"/>
<rect x="1" y="1" width="62" height="62" rx="14" fill="none" stroke="#fff" stroke-opacity=".14" stroke-width="2"/>
<text x="32" y="33" text-anchor="middle" dominant-baseline="central" fill="#fff"
 font-family="Segoe UI Variable Display, Segoe UI, system-ui, sans-serif" font-size="32"
 font-weight="600">{html.escape(letter)}</text>
</svg>'''
    return svg.encode()


def file_url(path):
    return 'file:///' + os.path.abspath(path).replace('\\', '/').replace(' ', '%20')


def runnable(project):
    return bool(project.get('command'))


def _quote(arg):
    return f'"{arg}"' if (' ' in arg or not arg) else arg


# ---------------------------------------------------------------------------
# Processes

def _process_start_time(pid):
    """Creation time (epoch seconds) of a live process, or None if it isn't running."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None  # 259 = STILL_ACTIVE
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return created / 1e7 - 11644473600  # FILETIME (100 ns since 1601) -> Unix time
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return 0.0  # alive; creation time not checked on this platform


def tracked_alive(entry):
    """True if the PID we recorded is still the process we started (PIDs get reused)."""
    started = _process_start_time(entry['pid'])
    if started is None:
        return False
    return started == 0.0 or abs(started - entry['started']) < 5


# Closing an app's window and a crash look the same from outside (the PID is just gone), so
# Launchpad waits on each process it started and keeps its exit code: 0 is a normal close, anything
# else shows as "stopped unexpectedly". A process that ends while Launchpad isn't running has no
# known code and shows as plain "stopped".

def record_exit(project_id, pid, code):
    with lock:
        state = load_state()
        entry = state.get(project_id)
        if not entry or entry['pid'] != pid:
            return  # stopped from Launchpad, or started again since
        entry['exit'] = code
        save_state(state)
    try:
        with open(log_path(project_id), 'a', encoding='utf-8') as log:
            log.write(f'--- {datetime.now():%Y-%m-%d %H:%M:%S} exited'
                      + (' normally\n' if code == 0 else f' with code {code}\n'))
    except OSError:
        pass


def _wait_for_pid(project_id, entry):
    """Wait on a process from an earlier Launchpad run (not our child, so no Popen to wait on)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION; holding the handle keeps the exit code
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, entry['pid'])
    if not handle:
        return
    try:
        if not tracked_alive(entry):  # checked after opening, so the PID can't be reused under us
            return
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            record_exit(project_id, entry['pid'], ctypes.c_int32(code.value).value)
    finally:
        kernel32.CloseHandle(handle)


def watch_running():
    """At startup: watch processes that an earlier Launchpad started and that are still running."""
    if not WINDOWS:
        return  # only the parent can read a process's exit code here
    for project_id, entry in load_state().items():
        if 'exit' not in entry:
            threading.Thread(target=_wait_for_pid, args=(project_id, entry), daemon=True).start()


LOOPBACK = {'127.0.0.1', 'localhost', '::1'}


def port_open(url, timeout=None):
    if not url or not url.startswith(('http://', 'https://')):
        return False
    parts = urlparse(url)
    host = parts.hostname or '127.0.0.1'
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if timeout is None:
        # A closed port here isn't refused, it just never answers, so every check of a stopped
        # app waits out the timeout. A local server that is up answers within milliseconds.
        timeout = 0.1 if host in LOOPBACK else 0.5
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def status(project):
    if not runnable(project):  # a folder or static page: nothing to start or stop
        return {'state': 'static', 'pid': None, 'since': None}
    entry = load_state().get(project['id'])
    ours = bool(entry) and tracked_alive(entry)
    listening = port_open(project.get('url'))
    if ours:
        state = 'running' if (listening or not project.get('url')) else 'starting'
    elif listening:
        state = 'external'  # something is serving the URL, but we didn't start it
    elif entry and entry.get('exit') not in (None, 0):
        state = 'exited'  # crashed or was killed; a normal close (0) or an unknown code is 'stopped'
    else:
        state = 'stopped'
    return {'state': state, 'pid': entry['pid'] if ours else None,
            'since': entry.get('started') if entry else None,
            'exit': entry.get('exit') if entry and not ours else None}


def log_path(project_id):
    return os.path.join(LOG_DIR, f'{project_id}.log')


def start(project):
    if not runnable(project):
        raise ApiError(f'{project["name"]} has no start command')
    with lock:
        current = status(project)['state']
        if current in ('running', 'starting', 'external'):
            return current
        os.makedirs(LOG_DIR, exist_ok=True)
        log = open(log_path(project['id']), 'a', encoding='utf-8', errors='replace')
        log.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} start: {project['command']}\n")
        log.flush()
        env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        env.pop('VIRTUAL_ENV', None)  # don't leak Launchpad's own environment into projects
        kwargs = {'cwd': project['path'], 'stdout': log, 'stderr': subprocess.STDOUT,
                  'stdin': subprocess.DEVNULL, 'env': env}
        if WINDOWS:
            kwargs['creationflags'] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
            command = project['command']  # CreateProcess parses the string itself
        else:
            kwargs['start_new_session'] = True
            command = shlex.split(project['command'])
        try:
            proc = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            log.write(f'failed to start: {exc}\n')
            log.close()
            raise ApiError(f'could not start {project["name"]}: {exc}') from exc
        log.close()  # the child keeps its own handle
        state = load_state()
        state[project['id']] = {'pid': proc.pid,
                                'started': _process_start_time(proc.pid) or time.time()}
        save_state(state)
    threading.Thread(target=lambda: record_exit(project['id'], proc.pid, proc.wait()),
                     daemon=True).start()
    return 'starting'


def stop(project):
    with lock:
        state = load_state()
        entry = state.pop(project['id'], None)
        save_state(state)
    if not entry or not tracked_alive(entry):
        return False
    if WINDOWS:  # /T takes the whole tree: `uv run` and `npm` start child processes
        subprocess.run(['taskkill', '/PID', str(entry['pid']), '/T', '/F'],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.killpg(entry['pid'], signal.SIGTERM)
    with open(log_path(project['id']), 'a', encoding='utf-8') as log:
        log.write(f'--- {datetime.now():%Y-%m-%d %H:%M:%S} stopped\n')
    return True


def browser_command():
    """The default browser's open command, e.g. '"...msedge.exe" --single-argument %1'.

    Taken from the https association, not the .html one: Windows opens file:/// pages with
    whatever owns .html files, which is often an editor (VS Code) rather than a browser.
    """
    import winreg
    key = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        prog_id = winreg.QueryValueEx(k, 'ProgId')[0]
    return winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, prog_id + r'\shell\open\command')


def open_url(url):
    """Open a page in the default browser, file:/// pages included."""
    if WINDOWS and url.startswith('file:'):
        try:
            template = browser_command()
            arg = url if '"%1"' in template or ' ' not in url else f'"{url}"'
            subprocess.Popen(template.replace('%1', arg))
            return
        except OSError:
            pass  # no readable association: fall back to Windows' own choice
    webbrowser.open(url)


def open_when_ready(project, timeout=90):
    """Start the project if needed, wait for its URL to answer, then open it in the browser."""
    url = project.get('url')
    if not url:
        raise ApiError(f'{project["name"]} has no URL to open')
    if not runnable(project):
        open_url(url)
        return
    if status(project)['state'] in ('stopped', 'exited'):
        start(project)

    def wait_and_open():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if port_open(url):
                open_url(url)
                return
            if status(project)['state'] in ('exited', 'stopped'):
                return  # it crashed; the log says why
            time.sleep(0.5)

    threading.Thread(target=wait_and_open, daemon=True).start()


def tail(project_id, lines=200, max_bytes=262144):
    """The last `lines` lines, reading only the end of the file: a dev server left running for days
    writes a log far too big to reread every two seconds."""
    try:
        with open(log_path(project_id), 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except FileNotFoundError:
        return ''
    text = chunk.decode('utf-8', 'replace').replace('\r\n', '\n')  # as text mode read it
    if size > max_bytes:
        text = text.split('\n', 1)[-1]  # drop the line the window cut in half
    return ''.join(text.splitlines(keepends=True)[-lines:])


# ---------------------------------------------------------------------------
# Claude Code chats
#
# Claude Code (CLI and the desktop app's Code tab) saves each chat as
# ~/.claude/projects/<cwd with non-alphanumerics as '-'>/<session id>.jsonl. The claude:// links
# below are the ones `claude --desktop` opens; that command itself refuses to run without an
# interactive console, so Launchpad opens the links directly.

CLAUDE_PROJECTS = os.environ.get('LAUNCHPAD_CLAUDE_PROJECTS') or os.path.join(
    os.path.expanduser('~'), '.claude', 'projects')
_chat_cache = {}  # transcript path -> (mtime, metadata)


def _claude_dir(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


def _chat_folders(path):
    """Folders whose chats count for a project at `path`: the folder itself and anything inside
    it, plus its parent when the project folder is named after it (4d-chess/4d-chess-repo)."""
    path = os.path.normcase(os.path.abspath(path))
    parent = os.path.dirname(path)
    ok = [path]
    if os.path.basename(path).startswith(os.path.basename(parent)):
        ok.append(parent)
    return ok


def _cwd_matches(cwd, path):
    cwd = os.path.normcase(os.path.abspath(cwd))
    project, *parent = _chat_folders(path)
    return cwd == project or cwd.startswith(project + os.sep) or cwd in parent


def _chat_meta(file):
    """{id, cwd, title} from a transcript's first and last 64 KB (a rename appends a new title)."""
    mtime = os.path.getmtime(file)
    cached = _chat_cache.get(file)
    if cached and cached[0] == mtime:
        return cached[1]
    meta = {'id': os.path.splitext(os.path.basename(file))[0], 'cwd': None, 'title': None,
            'updated': mtime}
    with open(file, 'rb') as f:
        head = f.read(65536)
        f.seek(max(0, os.path.getsize(file) - 65536))
        tail = f.read()
    first_cwd = last_cwd = None
    for chunk in (head, tail):
        for line in chunk.decode('utf-8', 'replace').splitlines():
            if not line.startswith('{'):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a line cut by the 64 KB window
            first_cwd = first_cwd or row.get('cwd')
            last_cwd = row.get('cwd') or last_cwd
            meta['title'] = row.get('customTitle') or row.get('aiTitle') or meta['title']
    # A chat can move to another folder partway through; it belongs where it is now
    meta['cwd'] = last_cwd or first_cwd
    _chat_cache[file] = (mtime, meta)
    return meta


def last_chat(project):
    """The project's most recently active Claude Code chat, or None."""
    try:
        names = os.listdir(CLAUDE_PROJECTS)
    except FileNotFoundError:
        return None
    prefixes = [_claude_dir(p) for p in _chat_folders(project['path'])]
    files = []
    for name in names:
        if any(name.lower() == p.lower() or name.lower().startswith(p.lower() + '-')
               for p in prefixes):
            folder = os.path.join(CLAUDE_PROJECTS, name)
            files += [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith('.jsonl')]
    for file in sorted(files, key=os.path.getmtime, reverse=True):
        meta = _chat_meta(file)
        if meta['cwd'] and _cwd_matches(meta['cwd'], project['path']):
            return meta  # the folder name alone can't tell cs3-a-b from cs3\a-b
    return None


def open_link(url):
    if WINDOWS:
        os.startfile(url)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', url])


def new_chat(project):
    """A new Code session in the project's folder. The app asks you to confirm the folder."""
    open_link('claude://code/new?folder=' + quote(project['path'], safe=''))


def resume_chat(project):
    chat = last_chat(project)
    if not chat:
        raise ApiError(f'no Claude Code chats found for {project["name"]}')
    open_link('claude://resume?session=' + quote(chat['id'], safe=''))
    return chat


def open_folder(project):
    if WINDOWS:
        os.startfile(project['path'])
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', project['path']])


# ---------------------------------------------------------------------------
# HTTP API

def projects_with_status():
    def one(p):
        try:
            chat = last_chat(p)
        except OSError:
            chat = None  # a transcript being written or removed right now; next poll gets it
        info = manifest(p)
        icon = icon_file(p, info)
        # The version in the URL lets the page cache the image until the file (or, for the
        # stand-in, the name) changes
        version = int(os.path.getmtime(icon)) if icon else f"d{zlib.crc32(p['name'].encode())}"
        description = info.get('description')
        return {**p, 'status': status(p), 'chat': chat,
                'icon': f"/api/projects/{quote(p['id'])}/icon?v={version}", 'has_icon': bool(icon),
                'description': description.strip() if isinstance(description, str) else ''}

    projects = load_projects()
    # In parallel: each stopped web app's port check waits out its timeout
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(projects)))) as pool:
        return list(pool.map(one, projects))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code, body, content_type='application/json; charset=utf-8',
              cache='no-store'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(length) or b'{}') if length else {}

    def _handle(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split('/') if p]
        try:
            if method == 'GET' and len(parts) <= 1 and parts[:1] != ['api']:
                # Files directly in ui/ (the page, its icon, design concepts); no subfolders
                name = parts[0] if parts else 'index.html'
                kind = {'.html': 'text/html; charset=utf-8', **IMAGE_TYPES}.get(
                    os.path.splitext(name)[1].lower())
                file = os.path.join(UI_DIR, name)
                if not kind or os.path.dirname(os.path.normpath(file)) != UI_DIR \
                        or not os.path.isfile(file):
                    raise ApiError('not found', 404)
                with open(file, 'rb') as f:
                    return self._send(200, f.read(), kind)
            if parts[:1] != ['api']:
                raise ApiError('not found', 404)
            route = parts[1:]
            if route == ['ping']:
                return self._send(200, {'app': 'launchpad'})
            if route == ['projects'] and method == 'GET':
                return self._send(200, projects_with_status())
            if route == ['projects'] and method == 'POST':
                b = self._body()
                project = add_project(b.get('path', ''), b.get('name') or None,
                                      b.get('command') or None, b.get('url'))
                return self._send(200, project)
            if route == ['detect'] and method == 'POST':
                path = self._body().get('path', '').strip().strip('"')
                if not os.path.isdir(path):
                    raise ApiError(f'not a folder: {path}')
                return self._send(200, detect(path))
            if len(route) >= 2 and route[0] == 'projects':
                project = get_project(route[1])
                action = route[2] if len(route) > 2 else None
                if action is None and method == 'PUT':
                    return self._send(200, update_project(project['id'], self._body()))
                if action is None and method == 'DELETE':
                    stop(project)
                    remove_project(project['id'])
                    return self._send(200, {'ok': True})
                if method == 'GET' and action == 'icon':
                    icon = icon_file(project)
                    if not icon:
                        return self._send(200, default_icon(project), IMAGE_TYPES['.svg'],
                                          'max-age=86400')
                    with open(icon, 'rb') as f:
                        return self._send(200, f.read(),
                                          IMAGE_TYPES[os.path.splitext(icon)[1].lower()],
                                          'max-age=86400')
                if method == 'GET' and action == 'log':
                    lines = int(parse_qs(url.query).get('lines', ['200'])[0])
                    return self._send(200, {'log': tail(project['id'], lines)})
                if method == 'POST' and action == 'start':
                    return self._send(200, {'state': start(project)})
                if method == 'POST' and action == 'stop':
                    return self._send(200, {'stopped': stop(project)})
                if method == 'POST' and action == 'restart':
                    stop(project)
                    time.sleep(0.5)
                    return self._send(200, {'state': start(project)})
                if method == 'POST' and action == 'open':
                    open_when_ready(project)
                    return self._send(200, {'ok': True})
                if method == 'POST' and action == 'folder':
                    open_folder(project)
                    return self._send(200, {'ok': True})
                if method == 'POST' and action == 'new-chat':
                    new_chat(project)
                    return self._send(200, {'ok': True})
                if method == 'POST' and action == 'last-chat':
                    return self._send(200, resume_chat(project))
            raise ApiError('not found', 404)
        except ApiError as exc:
            self._send(exc.status, {'error': str(exc)})
        except Exception as exc:  # report instead of dropping the connection
            self._send(500, {'error': f'{type(exc).__name__}: {exc}'})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')


def app_log(message):
    """Launchpad's own log, %LOCALAPPDATA%\\launchpad\\launchpad.log (projects log separately)."""
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, 'launchpad.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n')
    except OSError:
        pass


def watch_webview(window):
    """Reload the page when WebView2's renderer or GPU process dies or stops responding.

    pywebview doesn't handle WebView2's ProcessFailed event, so after one of those processes is
    lost (Windows reclaiming memory, a GPU reset while a game runs) the window just stays frozen
    until Launchpad is restarted. Runs on every page load; attaches once.
    """
    if getattr(window, '_launchpad_watched', False):
        return
    window._launchpad_watched = True

    def on_failed(sender, args):
        kind = str(args.ProcessFailedKind)
        app_log(f'WebView2 {kind} (exit code {args.ExitCode})')
        if kind == 'BrowserProcessExited':
            return  # the whole browser is gone; nothing left to reload, only a restart helps
        try:
            sender.Reload()
            app_log('reloaded the page')
        except Exception as exc:  # noqa: BLE001  report it rather than crash the UI thread
            app_log(f'reload failed: {exc}')

    def attach():  # on the UI thread, which owns the control
        try:
            form.webview.CoreWebView2.ProcessFailed += on_failed  # pythonnet wraps it on +=
            app_log('watching WebView2 for crashes')
        except Exception as exc:  # noqa: BLE001
            app_log(f'not watching WebView2 for crashes: {exc}')

    try:
        from System import Action  # pythonnet, which pywebview's WebView2 backend runs on
        form = window.native
        form.Invoke(Action(attach))
    except Exception as exc:  # noqa: BLE001  another backend, or pywebview internals changed
        app_log(f'not watching WebView2 for crashes: {exc}')


def already_running(port):
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ping', timeout=1) as r:
            return json.load(r).get('app') == 'launchpad'
    except Exception:
        return False


def serve(port, browser):
    url = f'http://127.0.0.1:{port}/'
    if already_running(port):  # a second launch just shows the running copy
        server = None
    else:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        watch_running()
    if not browser:
        try:
            import webview
        except ImportError:
            browser = True
        else:
            if WINDOWS:
                # Without its own app ID, Windows groups the window under pythonw's taskbar icon
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
            window = webview.create_window('Launchpad', url, width=980, height=720,
                                           min_size=(640, 420))
            window.events.loaded += lambda: watch_webview(window)
            webview.start(icon=ICON_ICO)
            return
    webbrowser.open(url)
    if server:
        print(f'Launchpad on {url}  (Ctrl+C to quit)')
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def make_shortcut(folder):
    """Create Launchpad.lnk in `folder`, with the app icon, running launchpad.pyw without a console."""
    if not WINDOWS:
        raise SystemExit('shortcuts are only supported on Windows')
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    lnk = os.path.join(folder, 'Launchpad.lnk')
    ps = lambda s: "'" + s.replace("'", "''") + "'"  # noqa: E731  PowerShell string literal
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps(lnk)}); "
        f"$s.TargetPath = {ps(pythonw)}; "
        f"$s.Arguments = {ps(chr(34) + os.path.join(APP_DIR, 'launchpad.pyw') + chr(34))}; "
        f"$s.WorkingDirectory = {ps(APP_DIR)}; "
        f"$s.IconLocation = {ps(ICON_ICO)}; "
        "$s.Description = 'Start, stop and open local projects'; $s.Save()"
    )
    subprocess.run(['powershell', '-NoProfile', '-Command', script], check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    return lnk


START_MENU = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu',
                          'Programs')
DESKTOP = os.path.join(os.path.expanduser('~'), 'Desktop')
UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Launchpad'


def install(desktop=False):
    """Start menu shortcut + an Installed apps entry (per user, no admin). Runs from this folder,
    so edits to the code and projects.json apply without reinstalling."""
    if not WINDOWS:
        raise SystemExit('install is only supported on Windows')
    try:
        import webview  # noqa: F401
    except ImportError:
        print('installing pywebview...')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--user', '-r',
                        os.path.join(APP_DIR, 'requirements.txt')], check=True)
    print('created', make_shortcut(START_MENU))
    if desktop:
        print('created', make_shortcut(DESKTOP))

    import winreg
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    script = os.path.join(APP_DIR, 'launchpad.py')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (('DisplayName', 'Launchpad'),
                            ('DisplayIcon', ICON_ICO),
                            ('Publisher', 'lukajk'),
                            ('InstallLocation', APP_DIR),
                            ('UninstallString', f'"{pythonw}" "{script}" uninstall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ('NoModify', 'NoRepair'):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
    print('added Launchpad to Settings > Apps > Installed apps')
    print('done: search "Launchpad" in the Start menu (right-click it to pin)')


def uninstall():
    """Undo install(). Leaves this folder, projects.json and the logs alone."""
    if not WINDOWS:
        raise SystemExit('uninstall is only supported on Windows')
    for folder in (START_MENU, DESKTOP):
        lnk = os.path.join(folder, 'Launchpad.lnk')
        if os.path.exists(lnk):
            os.remove(lnk)
            print('removed', lnk)
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        print('removed the Installed apps entry')
    except FileNotFoundError:
        pass
    print(f'Launchpad is uninstalled; its files are still in {APP_DIR}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Start, stop and open local projects.')
    parser.add_argument('--browser', action='store_true', help='use your browser, not a window')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest='command')
    add = sub.add_parser('add', help='register a project folder')
    add.add_argument('path')
    add.add_argument('--name')
    add.add_argument('--cmd', help='start command (detected if omitted)')
    add.add_argument('--url', help='page to open, for web apps (detected if omitted)')
    sub.add_parser('list', help='show registered projects')
    shortcut = sub.add_parser('shortcut', help='create a Launchpad shortcut with the app icon')
    where = shortcut.add_mutually_exclusive_group()
    where.add_argument('--start-menu', action='store_true', help='in the Start menu (searchable)')
    where.add_argument('--desktop', action='store_true', help='on the desktop')
    inst = sub.add_parser('install', help='add to the Start menu and Installed apps')
    inst.add_argument('--desktop', action='store_true', help='also put a shortcut on the desktop')
    sub.add_parser('uninstall', help='remove the shortcuts and the Installed apps entry')
    args = parser.parse_args(argv)

    if args.command == 'add':
        p = add_project(args.path, args.name, args.cmd, args.url)
        what = p['command'] or ('static page' if p['url'] else 'folder only')
        print(f"added {p['id']}: {what}" + (f"  ->  {p['url']}" if p['url'] else ''))
    elif args.command == 'shortcut':
        folder = START_MENU if args.start_menu else DESKTOP if args.desktop else APP_DIR
        print('created', make_shortcut(folder))
    elif args.command == 'install':
        install(args.desktop)
    elif args.command == 'uninstall':
        uninstall()
    elif args.command == 'list':
        for p in projects_with_status():
            print(f"{p['status']['state']:9} {p['id']:24} {p['command'] or '-'}  {p['url']}")
    else:
        serve(args.port, args.browser)


if __name__ == '__main__':
    main()
