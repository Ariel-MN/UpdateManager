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
# UI scale: "auto"/blank = automatic DPI scaling; a number (1.5, 2.0) enlarges on HiDPI/4K.
ui_scale = auto
# Self-update check on startup.
check_on_startup = true

# One [AppN] section per app. "name" must equal the GitHub repo name.
# Leave "version" empty for an app not installed yet (the button shows "Install").
[App1]
name = AutoClicker
version = v1.0.1
url = https://github.com/Ariel-MN/AutoClicker/releases/latest
```

Add one `[AppN]` section per app. Managed apps are expected to live **next to** the
`UpdateManager` folder (e.g. `…/Crypto/UpdateManager`, `…/Crypto/AutoClicker`).

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
`../env`). See `launchers/README`.

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
  `config.ini` to a number (e.g. `ui_scale = 2.0`). Automatic DPI scaling reads as 1.0 on some
  Linux/HiDPI sessions, which makes everything tiny.
- **`.desktop` flashes a terminal and shows no window:** the app crashed because its Python has no
  dependencies. Run the installer so a `env/` exists next to the app; the launchers use it.

## License

Apache License 2.0 — see `LICENSE`.
