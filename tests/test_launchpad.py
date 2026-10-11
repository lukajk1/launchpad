import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import launchpad as lp  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the registry, state and logs at a temp dir."""
    monkeypatch.setattr(lp, 'REGISTRY', str(tmp_path / 'projects.json'))
    monkeypatch.setattr(lp, 'STATE', str(tmp_path / 'home' / 'state.json'))
    monkeypatch.setattr(lp, 'LOG_DIR', str(tmp_path / 'home' / 'logs'))
    return tmp_path


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def make_folder(tmp_path, name, files):
    folder = tmp_path / name
    for rel, content in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(content, encoding='utf-8')
    return folder


def test_detects_claude_launch_json(tmp_path):
    launch = {'configurations': [{'name': 'reader', 'runtimeExecutable': 'uv',
                                  'runtimeArgs': ['run', 'my web'], 'port': 8000}]}
    folder = make_folder(tmp_path, 'web', {'.claude/launch.json': json.dumps(launch)})
    assert lp.detect(str(folder))[0] == {
        'name': 'reader', 'command': 'uv run "my web"', 'url': 'http://127.0.0.1:8000',
        'source': '.claude/launch.json'}


def test_detects_npm_dev_script_and_pyw_desktop_apps(tmp_path):
    folder = make_folder(tmp_path, 'site', {
        'package.json': json.dumps({'name': 'site', 'scripts': {'dev': 'next dev'}}),
        'tool.pyw': ''})
    found = lp.detect(str(folder))
    assert [f['command'] for f in found] == ['npm run dev', 'pythonw tool.pyw']
    assert found[0]['url'] == 'http://localhost:3000' and found[1]['url'] == ''


def test_add_update_remove(tmp_path):
    folder = make_folder(tmp_path, 'app', {'main.pyw': ''})
    first = lp.add_project(str(folder))
    second = lp.add_project(str(folder))
    assert (first['id'], second['id']) == ('app', 'app-1')
    assert first['command'] == 'pythonw main.pyw' and first['url'] == ''
    lp.update_project('app', {'name': 'Renamed', 'url': 'http://127.0.0.1:1'})
    assert lp.get_project('app')['name'] == 'Renamed'
    lp.remove_project('app-1')
    assert [p['id'] for p in lp.load_projects()] == ['app']


def test_folders_and_static_pages_need_no_command(tmp_path):
    plain = lp.add_project(str(make_folder(tmp_path, 'notes', {'notes.txt': ''})))
    assert (plain['command'], plain['url']) == ('', '')
    page = lp.add_project(str(make_folder(tmp_path, 'my site', {'index.html': ''})))
    assert page['command'] == ''
    assert page['url'].startswith('file:///') and page['url'].endswith('/my%20site/index.html')
    for p in (plain, page):
        assert lp.status(p)['state'] == 'static'
        with pytest.raises(lp.ApiError, match='no start command'):
            lp.start(p)
        assert lp.stop(p) is False
    with pytest.raises(lp.ApiError, match='no URL'):
        lp.open_when_ready(plain)
    with pytest.raises(lp.ApiError, match='not a folder'):
        lp.add_project(str(tmp_path / 'missing'))


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows browser lookup')
def test_file_pages_go_to_the_browser_not_the_html_handler(monkeypatch):
    launched = []
    monkeypatch.setattr(lp.subprocess, 'Popen', launched.append)
    monkeypatch.setattr(lp, 'browser_command', lambda: '"C:\\b\\edge.exe" --single-argument %1')
    lp.open_url('file:///C:/x/index.html')
    monkeypatch.setattr(lp, 'browser_command', lambda: '"C:\\b\\firefox.exe" -osint -url "%1"')
    lp.open_url('file:///C:/my site/index.html')
    assert launched == ['"C:\\b\\edge.exe" --single-argument file:///C:/x/index.html',
                        '"C:\\b\\firefox.exe" -osint -url "file:///C:/my site/index.html"']
    assert 'msedge' in lp.browser_command() or 'exe' in lp.browser_command()  # the real lookup


def write_chat(root, cwd, session, title=None, rename=None, age=0, moved_to=None):
    folder = root / lp._claude_dir(moved_to or cwd)
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{'type': 'user', 'cwd': cwd, 'sessionId': session}]
    if title:
        rows.append({'type': 'ai-title', 'aiTitle': title})
    rows += [{'type': 'assistant', 'filler': 'x' * 70000}]  # push the rename past the head window
    if moved_to:
        rows.append({'type': 'user', 'cwd': moved_to, 'sessionId': session})
    if rename:
        rows.append({'type': 'custom-title', 'customTitle': rename})
    file = folder / f'{session}.jsonl'
    file.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    t = time.time() - age
    os.utime(file, (t, t))


def test_last_chat_finds_the_projects_newest_chat(tmp_path, monkeypatch):
    chats = tmp_path / 'chats'
    monkeypatch.setattr(lp, 'CLAUDE_PROJECTS', str(chats))
    repo = make_folder(tmp_path, 'chess/chess-repo', {'index.html': ''})
    project = lp.add_project(str(repo))
    assert lp.last_chat(project) is None
    write_chat(chats, str(tmp_path / 'chess'), 'old', title='Old', age=600)
    assert lp.last_chat(project)['id'] == 'old'  # the parent it's named after counts
    write_chat(chats, str(repo / 'src'), 'sub', title='Sub', rename='Renamed', age=300)
    assert lp.last_chat(project)['id'] == 'sub' and lp.last_chat(project)['title'] == 'Renamed'
    write_chat(chats, str(tmp_path / 'chess' / 'chess-repo-2'), 'sibling', age=0)
    assert lp.last_chat(project)['id'] == 'sub'  # same folder-name prefix, different project

    opened = []
    monkeypatch.setattr(lp, 'open_link', opened.append)
    lp.resume_chat(project)
    lp.new_chat(project)
    assert opened[0] == 'claude://resume?session=sub'
    assert opened[1] == 'claude://code/new?folder=' + urllib.parse.quote(str(repo), safe='')
    with pytest.raises(lp.ApiError, match='no Claude Code chats'):
        lp.resume_chat(lp.add_project(str(make_folder(tmp_path, 'other', {'a.txt': ''}))))


def test_server_beats_static_index_html(tmp_path):
    folder = make_folder(tmp_path, 'app', {'index.html': '',
                                           'package.json': json.dumps({'scripts': {'dev': 'vite'}})})
    assert lp.add_project(str(folder))['command'] == 'npm run dev'


def test_start_status_stop_a_real_server(tmp_path):
    port = free_port()
    folder = make_folder(tmp_path, 'srv', {'index.html': 'hi'})
    project = lp.add_project(str(folder), name='srv',
                             command=f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1',
                             url=f'http://127.0.0.1:{port}')
    assert lp.status(project)['state'] == 'stopped'
    lp.start(project)
    for _ in range(40):
        if lp.status(project)['state'] == 'running':
            break
        time.sleep(0.25)
    assert lp.status(project)['state'] == 'running'
    assert lp.start(project) == 'running'  # no duplicate process
    assert lp.stop(project) is True
    time.sleep(0.5)
    assert lp.status(project)['state'] == 'stopped'
    assert not lp.port_open(project['url'])
    assert 'start:' in lp.tail(project['id']) and 'stopped' in lp.tail(project['id'])


def test_tail_reads_only_the_end_of_a_big_log(tmp_path):
    os.makedirs(lp.LOG_DIR, exist_ok=True)
    with open(lp.log_path('big'), 'w', encoding='utf-8') as f:
        f.writelines(f'line {i}\n' for i in range(200000))  # ~2.6 MB
    assert lp.tail('big', lines=3) == 'line 199997\nline 199998\nline 199999\n'
    small = lp.tail('big', lines=10**6, max_bytes=100)
    assert small.startswith('line ') and small.endswith('line 199999\n')  # no half line first
    assert lp.tail('missing') == ''


def test_reused_pid_is_not_treated_as_ours():
    # Our own PID is alive, but its start time doesn't match what we recorded.
    assert lp.tracked_alive({'pid': os.getpid(), 'started': time.time() - 3600}) is False
    me = lp._process_start_time(os.getpid())
    assert lp.tracked_alive({'pid': os.getpid(), 'started': me}) is True


def test_external_server_shows_as_external(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), lp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}'
        project = lp.add_project(str(make_folder(tmp_path, 'x', {'a.pyw': ''})), url=url)
        assert lp.status(project)['state'] == 'external'
    finally:
        server.shutdown()


def test_http_api(tmp_path):
    folder = make_folder(tmp_path, 'app', {'main.pyw': ''})
    server = ThreadingHTTPServer(('127.0.0.1', 0), lp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        assert call('GET', '/api/ping') == (200, {'app': 'launchpad'})
        assert call('POST', '/api/detect', {'path': str(folder)})[1][0]['command'] == 'pythonw main.pyw'
        status, project = call('POST', '/api/projects', {'path': str(folder)})
        assert status == 200 and project['id'] == 'app'
        status, listed = call('GET', '/api/projects')
        assert listed[0]['status']['state'] == 'stopped'
        assert call('GET', '/api/projects/nope/log')[0] == 404
        assert call('DELETE', '/api/projects/app') == (200, {'ok': True})
    finally:
        server.shutdown()


def test_icons_come_from_the_projects_launchpad_json(tmp_path):
    folder = make_folder(tmp_path, 'app', {'main.pyw': '', 'art/logo.png': 'png bytes'})
    plain = make_folder(tmp_path, 'plain', {'main.pyw': ''})
    (folder / '.launchpad.json').write_text(
        json.dumps({'icon': 'art/logo.png', 'description': ' Does things '}), encoding='utf-8')
    lp.add_project(str(folder))
    lp.add_project(str(plain), name='Zebra tool')
    server = ThreadingHTTPServer(('127.0.0.1', 0), lp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'

    def get(path):
        with urllib.request.urlopen(base + path) as r:
            return r.headers['Content-Type'], r.read()

    try:
        app, zebra = json.loads(get('/api/projects')[1])
        assert app['has_icon'] and app['description'] == 'Does things'
        assert get(app['icon']) == ('image/png', b'png bytes')
        # No .launchpad.json: a generated stand-in with the name's first letter
        assert not zebra['has_icon'] and zebra['description'] == ''
        kind, svg = get(zebra['icon'])
        assert kind == 'image/svg+xml' and b'>Z</text>' in svg
        # Only images, and only files that exist, count as icons
        (folder / '.launchpad.json').write_text('{"icon": "main.pyw"}', encoding='utf-8')
        assert lp.icon_file(lp.get_project('app')) is None
        (folder / '.launchpad.json').write_text('{not json', encoding='utf-8')
        assert lp.manifest(lp.get_project('app')) == {}
    finally:
        server.shutdown()


def test_ui_files_are_served_but_nothing_outside_ui():
    server = ThreadingHTTPServer(('127.0.0.1', 0), lp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with urllib.request.urlopen(base + '/icon.png') as r:
            assert r.headers['Content-Type'] == 'image/png'
        for path in ('/launchpad.py', '/..%5Claunchpad.py', '/projects.json'):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + path)
            assert err.value.code == 404
    finally:
        server.shutdown()


def wait_for(check, seconds=10):
    for _ in range(int(seconds / 0.1)):
        if check():
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize('code, state', [(0, 'stopped'), (3, 'exited')])
def test_closing_an_app_is_not_a_crash(tmp_path, code, state):
    folder = make_folder(tmp_path, 'app', {'main.pyw': ''})
    project = lp.add_project(str(folder), command=f'"{sys.executable}" -c "raise SystemExit({code})"')
    lp.start(project)
    assert wait_for(lambda: 'exit' in lp.load_state()['app'])
    assert lp.status(project)['state'] == state
    assert lp.status(project)['exit'] == code
    assert ('exited normally' if code == 0 else f'exited with code {code}') in lp.tail('app')


def test_exit_code_unknown_is_just_stopped(tmp_path):
    folder = make_folder(tmp_path, 'app', {'main.pyw': ''})
    project = lp.add_project(str(folder))
    lp.save_state({'app': {'pid': 999999, 'started': 0}})  # ended while Launchpad wasn't running
    assert lp.status(project)['state'] == 'stopped'


@pytest.mark.skipif(sys.platform != 'win32', reason='reads exit codes of non-child processes')
def test_watches_processes_from_an_earlier_run(tmp_path):
    folder = make_folder(tmp_path, 'app', {'main.pyw': ''})
    project = lp.add_project(str(folder))
    proc = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(1); raise SystemExit(5)'])
    lp.save_state({'app': {'pid': proc.pid, 'started': lp._process_start_time(proc.pid)}})
    lp.watch_running()  # as if Launchpad had just been reopened
    proc.wait()
    assert wait_for(lambda: lp.load_state()['app'].get('exit') == 5)
    assert lp.status(project)['state'] == 'exited'


def test_a_chat_that_moved_folders_belongs_where_it_is_now(tmp_path, monkeypatch):
    chats = tmp_path / 'chats'
    monkeypatch.setattr(lp, 'CLAUDE_PROJECTS', str(chats))
    old = lp.add_project(str(make_folder(tmp_path, 'ithaca', {'a.txt': ''})))
    new = lp.add_project(str(make_folder(tmp_path, 'arkvain', {'a.txt': ''})))
    write_chat(chats, str(tmp_path / 'ithaca'), 'moved', rename='arkvain',
               moved_to=str(tmp_path / 'arkvain'))
    assert lp.last_chat(new)['title'] == 'arkvain'
    assert lp.last_chat(old) is None
