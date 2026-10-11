# Launchpad

One window to start, stop and open local projects, instead of digging through folders.

- **Web apps** (a project with a URL): **Open** starts it if it isn't running, waits until the
  page answers, then opens it in your default browser.
- **Desktop apps** (no URL, e.g. pywebview `.pyw` apps): **Start** launches them.
- **Static pages** (a URL but no command, e.g. a `file:///` `index.html`): **Open** just opens it.
- **Folders** (neither): clicking opens them in Explorer.

The window is a shelf of project icons. Click an icon to do the obvious thing (open, start, or open
the folder); a dot on the icon shows when it's running. Right-click an icon, or use its ⋯, for
everything else: Stop/Restart, the folder, command and address, the live log, Claude chats, and Edit.
Type in the search box and press Enter to launch the first match (`/` jumps to the search box).

## Install

Double-click `install.bat` (or `python launchpad.py install`, add `--desktop` for a desktop
shortcut too). It installs pywebview if it's missing, adds Launchpad to the Start menu, and lists it
in Settings > Apps > Installed apps, all per user without admin. Launchpad keeps running from this
folder, so code and `projects.json` edits apply without reinstalling. To remove it, uninstall it
from Settings > Apps, or run `python launchpad.py uninstall`. Either way this folder is left alone.

## Run

Use the Start menu entry, double-click `launchpad.pyw`, or:

```
python launchpad.py              # native window (pywebview)
python launchpad.py --browser    # in your normal browser instead
```

It serves on `127.0.0.1:4700`; launching it again just shows the running copy.

## Projects

Add one from the window (**Add project** → paste a folder → **Detect** fills in how it runs), or:

```
python launchpad.py add C:\path\to\project
python launchpad.py add C:\path\to\project --name "My site" --cmd "npm run dev" --url http://localhost:3000
python launchpad.py list
```

Detection looks for, in order: `.claude/launch.json` (command and port), a `package.json`
`dev`/`start` script (port 3000 is a guess, check it), `.pyw` files (desktop apps), and an
`index.html` (a static page). If it finds none of these, the project is kept as a plain folder.

Everything is in `projects.json` next to this file, which is safe to edit by hand. It's created
the first time you add a project and isn't tracked by git; `projects.example.json` shows the format:

```json
{ "id": "my-web-app", "name": "My web app",
  "path": "C:\\code\\my-web-app", "command": "npm run dev",
  "url": "http://127.0.0.1:3000" }
```

## Icons

Each project can describe itself with a `.launchpad.json` in its own folder:

```json
{ "icon": "assets/icon.png", "description": "Image gallery with local AI tagging" }
```

`icon` is relative to the project folder (an absolute path works too); PNG, JPG, SVG, ICO, WebP and
GIF all work. Without one, Launchpad draws a stand-in: the name's first letter on a tile whose
colour comes from the name. `description` shows under the name when you open a project's details.

## Claude Code chats

Every project has **New chat**, which opens a new Code session in the Claude desktop app in the
project's folder (the app asks you to confirm the folder each time; that's its own safety check). A
project with chats also shows its most recent one ("last chat: <title> · 2 hours ago") and a
**Last chat** button that reopens it in the app.

Chats are read from `~/.claude/projects`, where Claude Code saves each one under the folder it
ran in. A project's chats are those from its folder or anything inside it, plus its parent folder
when the project folder is named after it (`4d-chess\4d-chess-repo`). The buttons open the same
`claude://resume?session=<id>` and `claude://code/new?folder=<path>` links that
`claude --desktop` uses; that command can't be used directly since it needs an interactive console.

## Staying responsive

- If WebView2's page or GPU process crashes or hangs (Windows reclaiming memory, a GPU reset
  while a game runs), Launchpad reloads the page instead of leaving a frozen window. pywebview
  doesn't do this itself. Each crash is logged to `%LOCALAPPDATA%\launchpad\launchpad.log`.
- The list refreshes every 2 s, but only while the window is visible, and each refresh starts
  only after the last one finished.
- Port checks run in parallel with a 0.1 s timeout on local addresses: on this machine a closed
  local port never refuses, it just doesn't answer, so each check waits out its timeout.

## How processes are handled

- Projects run in their own folder with no console window. Output goes to
  `%LOCALAPPDATA%\launchpad\logs\<id>.log`.
- Started projects keep running when you close Launchpad. Their PIDs are in
  `%LOCALAPPDATA%\launchpad\state.json`, so Launchpad still knows about them next time.
- **Stop** kills the whole process tree (`uv run` and `npm` start child processes). Before killing
  anything it checks the process's start time against the one it recorded, because Windows reuses
  PIDs and an old PID could belong to an unrelated program by now.
- Launchpad waits on each process it started and keeps its exit code, so closing an app's window
  (exit code 0) shows as stopped, and only a crash or a kill shows as "stopped unexpectedly". It
  also picks up processes from an earlier run when it starts. A process that ends while Launchpad
  isn't open has no known exit code and just shows as stopped.
- A URL that answers but wasn't started by Launchpad shows as "started elsewhere"; Launchpad won't
  stop it.

## Tests

```
uv run --no-project --with pytest python -m pytest tests
```
