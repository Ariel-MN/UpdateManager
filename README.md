# Update Manager

A small **cross-platform** application library / updater. One GUI lists the apps declared
in `config.ini` and keeps each of them up to date from its GitHub releases — and it also
keeps **itself** up to date.

The exact same file runs on **Linux, Windows and macOS**:

```bash
python updatemanager.py        # or: python3 updatemanager.py
```

All platform differences (config/temp paths, window icon, launching apps, hidden folders)
are handled inside the code — there is nothing to change per OS.

## How updating works

Two steps, used both for each managed app and for Update Manager itself:

1. **Detection by tag.** The repo's latest release tag (GitHub API `releases/latest`) is
   compared against the current version. This decides *whether* an update exists.
2. **Delivery by manifest.** An `update.json` published among the release **assets** lists
   the files to download and where each goes:

   ```json
   {
     "files": [
       { "filename": "main.py", "output": "main.py" }
     ]
   }
   ```

   If a release has no `update.json`, the tagged **source zip** is downloaded and extracted
   instead (automatic fallback).

All transfers go through the **authenticated GitHub API**, so **private repositories work** as
long as the configured token has access to them (see "Token / security"). For a private repo the
token is required, not optional.

On startup, Update Manager checks its own latest release and, if there is a newer one,
asks whether to update now; on *Yes* it self-updates and restarts. This can be turned off
with `check_on_startup = false` in `config.ini`.

## Configuration — `config.ini`

```ini
[GitHub]
username = Ariel-MN

[UpdateManager]
# Window title shown when the app opens.
title = Update Manager
# Heading shown at the top inside the window.
heading = Application Library
# UI scale: "auto"/blank = automatic (Linux reads the desktop scale from Xft.dpi); "off" = 1.0;
# a number (1.5, 2.0) forces that scale.
ui_scale = auto
# Self-update check on startup.
check_on_startup = true
# Theme: system / light / dark (the header theme button switches it and saves the choice).
appearance = system
# Re-check every app every N minutes while the window is open (0 = never).
recheck_minutes = 30
# Desktop notification when updates are found while the window is in the background.
notifications = true

# One [AppN] section per app. "name" must equal the GitHub repo name.
# Leave "version" empty for an app not installed yet (the button shows "Install").
[App1]
name = AutoClicker
version = v1.0.1
url = https://github.com/Ariel-MN/AutoClicker/releases/latest
```

Add one `[AppN]` section per app. Managed apps are expected to live **next to** the
`UpdateManager` folder (e.g. `…/Crypto/UpdateManager`, `…/Crypto/AutoClicker`).

### Using the window

- **Get updates** updates every app with a known update; if none is known it checks GitHub
  again first and updates whatever turns out to be outdated. It shows the pending count
  ("Get updates (2)"), and "Checking…" / "Updating…" while work is in progress; the window
  title also shows how many updates are available.
- Downloads report progress in the app's row ("Updating… 45 %", or MB when the size is unknown).
- **Open** shows "Launching…" for a moment and ignores repeat clicks meanwhile, so a slow app
  is not started twice.
- Status colours: accent = update available / in progress, red = "Couldn't check" (offline,
  rate limit, GitHub error — the app's button still works), grey = the repo has no releases.
- Click a red **"Couldn't check ⓘ"** to see why (offline, rate limit, HTTP error…).
- Every app is re-checked automatically (every 30 minutes by default) while the window stays
  open. If updates show up while the window is in the background or minimised, a desktop
  notification says so once per version (Linux `notify-send`, macOS Notification Center; on
  Windows the taskbar button flashes).
- **Settings** (gear button, **Ctrl+,**): GitHub username, re-check interval, notifications,
  startup self-update check and theme. They are saved in `config.ini`. The token is deliberately
  not editable there (see *Token / security*); `ui_scale` stays a `config.ini` option.
- **Search** (field at the top right; click it or press **Ctrl+F**) filters the list by app name;
  **Esc** clears it. It does not take the keyboard focus when the window opens.
- While an app downloads, its button becomes **Cancel**. Everything is downloaded to a temp
  folder first and copied only at the end, so cancelling (or a failed download) leaves the
  installed version untouched.
- The **⋯** button of each row (or a right-click on the row) opens: Open, Open folder,
  What's new (when an update is available), Check again and **Uninstall…**.
- **Uninstall** asks first, showing exactly what will happen. It runs the app's own uninstaller
  if it ships one (to remove menu entries / `.desktop` files, shortcuts…), then deletes the app
  folder and clears its version in `config.ini`. See *Uninstalling an app* below for exactly
  which files are run.
- An app whose version is in `config.ini` but whose folder is missing (deleted by hand) is
  shown as not installed, with an **Install** button.
- Closing the window while an install/update is running asks first ("Wait" / "Close"), since
  closing mid-copy can leave that app half-updated.
- With no GitHub username the rows say so (click the text to open Settings); with no `[AppN]`
  sections the window says what to add to `config.ini`.
- Click **"Update available (vX)"** to read that version's release notes, with **Update now**
  and **Open on GitHub** buttons.
- Launching Update Manager while it is already open brings the existing window to the front
  (also when it is minimised) instead of starting a second copy — on every OS.
- Keyboard: **F5** or **Ctrl+R** re-checks every app; **Ctrl+F** search; **Ctrl+,** settings;
  **Ctrl+Q** / **Ctrl+W** closes (Command instead of Ctrl on macOS).
- An app given a single grey icon gets it inverted automatically on the theme where it would
  not be visible (e.g. a white glyph in the light theme). Coloured icons are never changed.

### Uninstalling an app

**Uninstall…** (in the **⋯** menu of an installed app) does three things, after a confirmation
that lists them:

1. Runs the app's own uninstaller, if one is found (see below). Use it to undo what the app's
   installer did outside its folder: menu entries / `.desktop` files, shortcuts, services…
2. Deletes the app folder (`<folder next to UpdateManager>/<AppName>`).
3. Clears the app's `version` in `config.ini`, so it shows **Install** again.

**Which file is run.** Update Manager looks inside the app folder, and in its subfolders up to
**3 levels** below it (e.g. `install/linux/uninstall.sh`; not `a/b/c/d/uninstall.sh`), for a
file whose name (without extension) is one of these, in any letter case:

| Name | Examples that match | Examples that do **not** match |
|---|---|---|
| anything starting with `uninstal` | `uninstal`, `uninstall`, `Uninstall`, `uninstaler`, `uninstaller`, `uninstallapp`, `uninstall-linux`, `Uninstall MyApp` | `uninst`, `deinstall`, `my-uninstall` |
| `unins` + optional digits (Inno Setup uninstallers) | `unins`, `unins000` | `uninsx` |
| `remove` | `remove` | `removeall` |
| `cleanup` | `cleanup` | `clean` |
| `unins…`, `remove` or `cleanup` + a separator (`-`, `_`, `.`, space…) + anything | `unins000-x`, `remove-app`, `cleanup_all` | |

…with an extension that **this** operating system can run:

| OS | Extensions (in order of preference) |
|---|---|
| Windows | `.exe`, `.ps1`, `.bat`, `.cmd`, `.vbs`, `.py`, `.pyw` |
| macOS | `.command`, `.sh`, `.py`, or no extension if the file is executable |
| Linux | `.sh`, `.py`, or no extension if the file is executable |

Where it looks:

- **Folders named after an operating system** are used only on that system: `linux`, `ubuntu`,
  `debian`, `raspberrypi` (Linux); `macos`, `mac`, `osx`, `darwin` (macOS); `windows`, `win`,
  `win32`, `win64` (Windows). So `install/linux/uninstall.sh` runs on Linux and
  `install/windows/uninstall.ps1` on Windows, never the other way round.
- **Never searched:** `env`, `venv`, `.venv`, `.git`, `node_modules`, `__pycache__`,
  `site-packages`, `img`, `assets`, `docs`, `test`, `tests`, and symlinked folders.
- **If several files match, only ONE is run** — not the first one found, but the best one by
  these rules, in order:
  1. one inside a folder for this OS (e.g. `install/linux/`) wins over one elsewhere;
  2. then the one nearest to the app folder (fewest levels down);
  3. then the extension order in the table above (on Linux: `.sh`, then `.py`, then an
     executable without extension);
  4. if still tied, the first by path in alphabetical order.

  Example on Linux, with `uninstall.sh` in the app folder, `install/linux/uninstall.sh` and
  `scripts/uninstal.py`: only `install/linux/uninstall.sh` runs. The confirmation shown before
  uninstalling always names the file that will run. Running several would repeat the same
  work, since apps often ship one uninstaller per OS or per language.

How it runs:

- From the app folder, with no console window and no input (it must not ask questions), with the
  environment variable `UPDATEMANAGER_UNINSTALL=1` set, and for at most 10 minutes.
- `.sh` / `.command` with `bash`; `.ps1` with
  `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File`; `.bat` / `.cmd` with
  `cmd /c`; `.vbs` with `cscript //nologo`; `.py` / `.pyw` with the app's own virtualenv
  (`env`, `venv` or `.venv` in the app folder, or the shared `env` next to the apps), otherwise
  with Update Manager's Python; `.exe` and extension-less files directly.
- If it exits with an error, you see its last lines of output and choose whether to delete the
  folder anyway or keep it.

Safety: only a folder **directly next to** Update Manager can be deleted, never Update Manager
itself; a symlinked app folder only loses the link; read-only files (e.g. Windows, `.git`
objects) are made writable so they can be removed.

## Token / security

Update Manager talks to GitHub through its authenticated API, so it needs a **Personal Access
Token (PAT)** to see and download releases from **private** repositories (for public repos a
token is optional — it only raises the API rate limit). The token is **never** stored in
`config.ini`.

### 1. Create the token on GitHub

You can use either a fine-grained token (recommended, least privilege) or a classic token.

**Fine-grained token (recommended):**

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
   tokens**, or directly <https://github.com/settings/personal-access-tokens/new>.
2. **Token name:** e.g. `update-manager`. **Expiration:** pick one (you can rotate later).
3. **Resource owner:** your account.
4. **Repository access:** *Only select repositories* → choose the repos Update Manager will
   manage (e.g. `UpdateManager`, and your apps). *All repositories* also works.
5. **Permissions → Repository permissions → Contents: Read-only.** That single permission is
   enough to read releases and download their assets / source archives. Leave everything else
   as *No access*.
6. **Generate token** and copy it (shown only once).

**Classic token (simpler, broader):**

1. Go to <https://github.com/settings/tokens> → **Generate new token (classic)**.
2. Give it a note and expiration, tick the **`repo`** scope (full control of private repos).
3. **Generate token** and copy it (`ghp_…`, shown only once).

### 2. Give the token to Update Manager

Provide it in **one** of these ways (the environment variable wins if both are set):

- **Secrets file** `secrets.ini` in the **project folder** (next to `updatemanager.py`),
  created with owner-only `0600` permissions. It is **gitignored**, so it never reaches the
  repository:

  ```ini
  [GitHub]
  token = ghp_xxx
  ```

- **Environment variable:**

  ```bash
  export UPDATEMANAGER_GITHUB_TOKEN=ghp_xxx     # Linux/macOS (add to ~/.profile to persist)
  setx  UPDATEMANAGER_GITHUB_TOKEN ghp_xxx      :: Windows (new terminals/sessions)
  ```

**Migration:** if an older `config.ini` still contains a `token` under `[GitHub]`, on first
run it is moved to `secrets.ini` and removed from `config.ini` automatically — the config file
itself is kept, only the secret leaves it.

> **Security:** keep the token out of the repo (it already is, via `.gitignore`). If a token is
> ever committed or shared, **revoke it** at <https://github.com/settings/tokens> and generate
> a new one.

## Install (dependencies)

Easiest — run the installer for your OS (creates a virtualenv in `env/` and installs
everything; see `install/README`):

| OS | Command |
|----|---------|
| Linux / Raspberry Pi | `chmod +x install/linux/install.sh && ./install/linux/install.sh` |
| macOS | `chmod +x install/macos/install.command && ./install/macos/install.command` |
| Windows | `powershell -ExecutionPolicy Bypass -File install\windows\install.ps1` |

The one-liner marks the installer executable (so it can be double-clicked next time) and runs it.
If you can't set the bit (e.g. a file on an exFAT USB), run it with `bash install/linux/install.sh`
instead — no execute bit needed. The installer then also restores `+x` on the launchers.

On **Linux**, the installer **adds "Update Manager" to your applications menu**: it writes working
`.desktop` entries (with the real install path, GUI-only, with icon) into
`~/.local/share/applications`, so it shows up in the menu right away. The `launchers/linux/*.desktop`
files in the repo are only templates (they contain `{INSTALL_DIR}`) — launch from the menu, not
from those files directly.

Or manually:

```bash
python -m venv env
# Linux/macOS: source env/bin/activate   |   Windows: env\Scripts\activate
pip install -r requirements.txt
```

> **Raspberry Pi / HiDPI note:** if `pip install` fails building **Pillow**, the Linux
> installer falls back to the system Pillow (`sudo apt install python3-pil python3-pil.imagetk`)
> inside a `--system-site-packages` venv. `requirements.txt` leaves Pillow unpinned so pip can
> pick a version with a prebuilt wheel for your Python.

## Launchers

Optional double-click launchers live in `launchers/`, one set per OS, each labelled with
its target system. They auto-activate the `env/` virtualenv (repo-local first, then a shared
`../env`). See `launchers/README`. Duplicate launches are handled by the app itself (the
running window comes to the front), so the launchers no longer need their own guard.

## Uninstall

Removes what the installer created (menu entries on Linux, Desktop shortcut on Windows, the `env/`
virtualenv and caches). Your source files, `config.ini` and `secrets.ini` are kept.

| OS | Command |
|----|---------|
| Linux / Raspberry Pi | `bash install/linux/uninstall.sh` |
| macOS | `bash install/macos/uninstall.command` |
| Windows | `powershell -ExecutionPolicy Bypass -File install\windows\uninstall.ps1` |

To remove everything afterwards, delete the folder.

## Troubleshooting

- **Window too small / unreadable on a 4K screen:** set `ui_scale` under `[UpdateManager]` in
  `config.ini` to a number (e.g. `ui_scale = 2.0`). On Linux `auto` uses `Xft.dpi` (run
  `xrdb -query | grep dpi`); if your session does not publish it, set the number by hand.
- **Window size:** only the size is remembered (`window_size`, in unscaled units); the position
  is left to the window manager, which centres the window on GNOME.
- **`.desktop` flashes a terminal and shows no window:** the app crashed because its Python has no
  dependencies. Run the installer so a `env/` exists next to the app; the launchers use it.

## License

Apache License 2.0 — see `LICENSE`.
