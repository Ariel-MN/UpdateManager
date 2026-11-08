#!/usr/bin/env python3
"""Update Manager — a small cross-platform application library / updater.

One GUI that lists the apps declared in ``config.ini`` and keeps them up to date from
their GitHub releases, and that also keeps **itself** up to date. The very same file
runs on Linux, Windows and macOS with ``python updatemanager.py`` — all platform
differences (paths, hidden temp dirs, window icon, app launching) are handled here.

How updating works (two steps, per app and for Update Manager itself):
  1. Detection by TAG: the latest release tag of the repo (GitHub API ``releases/latest``)
     is compared against the current version. That decides *whether* an update exists.
  2. Delivery by MANIFEST: an ``update.json`` published among the release assets lists the
     files to download and where each one goes. If no manifest is present, the source zip
     of the tag is downloaded and extracted as a fallback.

Security: the GitHub token is NEVER read from or written to ``config.ini``. It lives in a
separate ``secrets.ini`` in the project folder next to this script (gitignored, created with
``0600`` permissions), or in the environment variable ``UPDATEMANAGER_GITHUB_TOKEN``. If an old
``config.ini`` still carries a ``token`` under ``[GitHub]``, it is migrated to ``secrets.ini`` on
first run and stripped from the file (the file itself is kept — it is still the configuration).
"""
from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
import platform
import getpass
import queue
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tkinter
from tkinter import font as tkfont
import webbrowser
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from shutil import copy2, rmtree, which
from urllib.parse import quote

from customtkinter import (
    AppearanceModeTracker, CTk, CTkScrollbar, CTkToplevel, DrawEngine, ScalingTracker, ThemeManager,
    deactivate_automatic_dpi_awareness, get_appearance_mode, set_appearance_mode,
    set_widget_scaling, set_window_scaling,
)
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat, ImageTk

if TYPE_CHECKING:   # imported lazily (see github_session): ~140 ms off the startup
    import requests

# ── Identity ──────────────────────────────────────────────────────────────────────────
# Current version of Update Manager itself. Self-update compares this against the latest
# release tag of SELF_REPO; a new release must bump this value in the shipped file.
__version__ = "v1.1.0"
SELF_REPO = "UpdateManager"
APP_TITLE = "Update Manager"
APP_HEADING = "Application Library"

# ── Paths ─────────────────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
IMG_DIR = APP_DIR / "img"
# Downloaded managed-app icons land here (gitignored); img/ "fills itself".
ICON_CACHE = IMG_DIR / ".cache"
# Managed apps live next to this folder (…/Crypto/UpdateManager, …/Crypto/AutoClicker, …).
APPS_ROOT = APP_DIR.parent
CONFIG_PATH = APP_DIR / "config.ini"

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = not IS_WINDOWS and not IS_MAC

HTTP_TIMEOUT = 30


# The GitHub token lives in the project folder, next to this script, and is gitignored
# (see .gitignore) so it never reaches the repository.
SECRETS_PATH = APP_DIR / "secrets.ini"

# ── Theme ─────────────────────────────────────────────────────────────────────────────
FONT_TITLE = ("Segoe UI", 22, "bold")
FONT_SUBTITLE = ("Segoe UI", 16, "bold")
FONT_BODY = ("Segoe UI", 14, "normal")
FONT_BODY_BOLD = ("Segoe UI", 14, "bold")

PRIMARY = ("#009faa", "#29f7ff")
PRIMARY2 = ("#19a8b2", "#28e2e9")
SECONDARY = ("#fdfdfd", "#323232")
TERTIARY2 = ("#f2f2f2", "#494949")
QUATERNARY = ("#8a8a8a", "#9d9d9d")


# ── Secrets (token) ─────────────────────────────────────────────────────────────────--
def load_token() -> str:
    """Return the GitHub token from the environment or the external secrets file ("" if none)."""
    env = os.environ.get("UPDATEMANAGER_GITHUB_TOKEN", "").strip()
    if env:
        return env
    if SECRETS_PATH.is_file():
        secrets = configparser.ConfigParser()
        try:
            secrets.read(SECRETS_PATH, encoding="utf-8")
            return secrets.get("GitHub", "token", fallback="").strip()
        except configparser.Error:
            return ""
    return ""


def save_token(token: str) -> None:
    """Persist the token to the external secrets file with owner-only permissions."""
    secrets = configparser.ConfigParser()
    if SECRETS_PATH.is_file():
        secrets.read(SECRETS_PATH, encoding="utf-8")
    if not secrets.has_section("GitHub"):
        secrets.add_section("GitHub")
    secrets.set("GitHub", "token", token)
    with open(SECRETS_PATH, "w", encoding="utf-8") as fh:
        secrets.write(fh)
    try:  # best effort; Windows ignores POSIX modes
        os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def migrate_token_out_of_config(config: configparser.ConfigParser) -> None:
    """If config.ini still holds a [GitHub] token, move it to the secret store and strip it
    from the file (the file is KEPT — only the secret leaves it)."""
    if not config.has_option("GitHub", "token"):
        return
    token = config.get("GitHub", "token").strip()
    if token and not load_token():
        save_token(token)
    config.remove_option("GitHub", "token")
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            config.write(fh)
    except OSError as exc:  # non-fatal: the token is already secured
        print(f"config: could not rewrite without token: {exc}")


# ── Config ────────────────────────────────────────────────────────────────────────────
def read_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    return config


def write_config_version(config: configparser.ConfigParser, section: str, version: str) -> None:
    if not config.has_section(section):
        return
    config.set(section, "version", version)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            config.write(fh)
    except OSError as exc:
        print(f"config: could not persist version for {section}: {exc}")


def parse_apps(config: configparser.ConfigParser) -> list[dict]:
    apps = []
    for section in config.sections():
        if section.startswith("App"):
            apps.append({
                "section": section,
                "name": config.get(section, "name", fallback=section),
                "version": config.get(section, "version", fallback="").strip(),
                "url": config.get(section, "url", fallback="").strip(),
                "icon_light": config.get(section, "icon_light", fallback="").strip(),
                "icon_dark": config.get(section, "icon_dark", fallback="").strip(),
            })
    return apps


# ── Versioning ────────────────────────────────────────────────────────────────────────
def version_tuple(version: str) -> tuple[int, ...]:
    parts = version.strip().lstrip("vV").split(".")
    out = []
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def has_update(current: str, latest: str) -> bool:
    """True when ``latest`` is strictly newer than ``current`` (zero-padded tuple compare)."""
    if not current or not latest:
        return False
    cur, lat = version_tuple(current), version_tuple(latest)
    size = max(len(cur), len(lat))
    cur += (0,) * (size - len(cur))
    lat += (0,) * (size - len(lat))
    return lat > cur


# ── GitHub ────────────────────────────────────────────────────────────────────────────
def github_session(token: str) -> requests.Session:
    # requests (+ urllib3) takes ~140 ms to import on a Pi; it is only needed for network work,
    # which starts after the window is shown, so it is imported here instead of at the top.
    import requests
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    session.headers.update({"Accept": "application/vnd.github+json"})
    return session


def get_latest_release(session: requests.Session, owner: str, repo: str,
                       strict: bool = False) -> dict | None:
    """Full 'latest release' object from the GitHub API. Works for PRIVATE repos when the
    session carries a token with access. None if there is no release / no access / on error.
    With ``strict=True`` a failed check (offline, timeout, rate limit, server error) raises
    instead, so the UI can tell "could not check" apart from "no releases" (HTTP 404)."""
    import requests   # already loaded by github_session; cheap
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            if strict and resp.status_code != 404:
                raise ConnectionError(f"GitHub answered HTTP {resp.status_code}")
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        if strict:
            raise
        return None
    return data if isinstance(data, dict) and data.get("tag_name") else None


def get_latest_version(session: requests.Session, owner: str, repo: str,
                       strict: bool = False) -> str | None:
    """Latest release tag of owner/repo, or None if there are no releases / no access."""
    release = get_latest_release(session, owner, repo, strict)
    return release.get("tag_name") if release else None


class Cancelled(Exception):
    """Raised inside a download when the user pressed Cancel."""


def _download(session: requests.Session, url: str, dest: Path, headers: dict | None = None,
              progress=None) -> None:
    """Stream ``url`` to ``dest``. ``progress(done_bytes, total_bytes_or_0)`` is called per chunk;
    it may raise ``Cancelled`` to abort the transfer."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=HTTP_TIMEOUT, headers=headers) as resp:
        resp.raise_for_status()
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        done = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)


def _asset_by_name(release: dict, name: str) -> dict | None:
    for asset in release.get("assets", []) or []:
        if asset.get("name") == name:
            return asset
    return None


def _download_asset(session: requests.Session, asset: dict, dest: Path, progress=None) -> None:
    """Download a release asset through the API endpoint (PRIVATE-repo safe).

    The asset API redirects to signed storage; requests drops the Authorization header on the
    cross-host redirect, and the signed URL carries its own credentials — so this authenticates
    for private repos where the plain /releases/download/ URL would 404."""
    _download(session, asset["url"], dest, headers={"Accept": "application/octet-stream"},
              progress=progress)


def _parse_manifest(text: str) -> list[dict] | None:
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, list) else None


def apply_release(session: requests.Session, owner: str, repo: str,
                  latest_ver: str, target_dir: Path, progress=None) -> None:
    """Download the latest release of owner/repo into ``target_dir``. Works for PRIVATE repos
    (all transfers go through the authenticated GitHub API).

    Strategy: if the release ships an ``update.json`` asset, download exactly the files it lists;
    otherwise fall back to the tagged source archive (zipball API). Raises on failure.
    ``progress(done_bytes, total_bytes_or_0)`` reports the download of the release files and may
    raise ``Cancelled``. Everything is downloaded to a temp dir FIRST and only then copied into
    ``target_dir``, so a cancelled or failed download leaves the installed app untouched."""
    release = get_latest_release(session, owner, repo)
    if release is None:
        raise RuntimeError(f"no accessible 'latest' release for {owner}/{repo} "
                           f"(a private repo needs a token with access)")
    with tempfile.TemporaryDirectory(prefix="updatemanager-") as tmp:
        tmp_dir = Path(tmp)
        manifest_asset = _asset_by_name(release, "update.json")
        if manifest_asset is not None:
            staged_manifest = tmp_dir / "update.json"
            _download_asset(session, manifest_asset, staged_manifest)
            manifest = _parse_manifest(staged_manifest.read_text(encoding="utf-8"))
            if manifest:
                staged_files = []
                for entry in manifest:
                    asset = _asset_by_name(release, entry["filename"])
                    if asset is None:
                        raise RuntimeError(f"release asset not found: {entry['filename']}")
                    staged = tmp_dir / "assets" / entry["filename"]
                    _download_asset(session, asset, staged, progress)
                    staged_files.append((staged, target_dir / entry["output"]))
                for staged, dest in staged_files:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copy2(staged, dest)
                return
        # Fallback: tagged source archive via the API (zipball authenticates for private repos).
        tag = release.get("tag_name", latest_ver)
        archive = tmp_dir / "source.zip"
        _download(session, f"https://api.github.com/repos/{owner}/{repo}/zipball/{tag}", archive,
                  progress=progress)
        unpacked = tmp_dir / "unpacked"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(unpacked)
        roots = [p for p in unpacked.iterdir() if p.is_dir()]
        source = roots[0] if len(roots) == 1 else unpacked
        for item in source.rglob("*"):
            if item.is_file():
                dest = target_dir / item.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy2(item, dest)


def fetch_repo_file(session: requests.Session, owner: str, repo: str, path: str, dest: Path,
                    ref: str = "main") -> None:
    """Download ONE file from a repo (no clone) via the Contents API. Works for PRIVATE repos
    with a token; fetches just this file so an icon can be shown before the app is installed."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path)}?ref={ref}"
    _download(session, url, dest, headers={"Accept": "application/vnd.github.raw"})


# ── Platform helpers ──────────────────────────────────────────────────────────────────
def launcher_candidates(app_name: str) -> list[Path]:
    """Per-OS launchers to try when opening a managed app."""
    app_dir = APPS_ROOT / app_name
    if IS_WINDOWS:
        names = [f"{app_name}.bat", f"{app_name}.cmd", f"{app_name}.exe", f"{app_name}.pyw"]
    elif IS_MAC:
        names = [f"{app_name}.command", f"{app_name}.sh", f"{app_name}.pyw"]
    else:
        names = [f"{app_name}.sh", f"{app_name}.pyw"]
    return [app_dir / n for n in names]


def open_app(app_name: str) -> None:
    for launcher in launcher_candidates(app_name):
        if launcher.is_file():
            try:
                if launcher.suffix == ".pyw":
                    subprocess.Popen([sys.executable, str(launcher)])
                elif IS_WINDOWS:
                    os.startfile(str(launcher))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([str(launcher)])
            except OSError as exc:
                show_error("Open failed", f'Could not launch "{app_name}": {exc}')
            return
    show_error("Open failed", f'No launcher found for "{app_name}" in {APPS_ROOT / app_name}.')


def open_folder(path: Path) -> None:
    """Show a folder in the system file manager."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        show_error("Open folder failed", f"Could not open {path}: {exc}")


# ── Uninstall ─────────────────────────────────────────────────────────────────────────
# An app may ship its own uninstaller (removes menu entries / .desktop files, shortcuts,
# services…). It is looked for by NAME with the extensions THIS OS can run, in the app folder and
# up to 3 levels below. Names: anything starting with "uninstal" (so uninstal, uninstall,
# uninstaller, uninstall-linux…), or unins + digits (Inno Setup's unins000.exe), remove or
# cleanup, alone or followed by a separator. Scripts inside an OS-named folder (linux/, macos/,
# windows/…) are used only on that OS.
_UNINSTALL_NAME = re.compile(r"^(uninstal.*|(unins\d*|remove|cleanup)([\W_].*)?)$", re.IGNORECASE)
_UNINSTALL_EXTS = (
    (".exe", ".ps1", ".bat", ".cmd", ".vbs", ".py", ".pyw") if IS_WINDOWS else
    (".command", ".sh", ".py", "") if IS_MAC else
    (".sh", ".py", "")
)
_OS_DIRS = {"linux": "linux", "ubuntu": "linux", "debian": "linux", "raspberrypi": "linux",
            "macos": "mac", "mac": "mac", "osx": "mac", "darwin": "mac",
            "windows": "win", "win": "win", "win32": "win", "win64": "win"}
_THIS_OS = "win" if IS_WINDOWS else "mac" if IS_MAC else "linux"
_SKIP_DIRS = {"env", "venv", ".venv", ".git", "node_modules", "__pycache__", "site-packages",
              "img", "assets", "docs", "test", "tests"}


def find_uninstaller(app_dir: Path) -> Path | None:
    """Best uninstaller of the app for this OS, or None. Searches up to 3 levels deep."""
    if not app_dir.is_dir():
        return None
    found: list[tuple[int, int, str, Path]] = []

    def scan(folder: Path, depth: int, os_match: bool) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if depth >= 3 or name.lower() in _SKIP_DIRS or entry.is_symlink():
                    continue
                tag = _OS_DIRS.get(name.lower())
                if tag is not None and tag != _THIS_OS:
                    continue                      # another OS's scripts
                scan(entry, depth + 1, os_match or tag == _THIS_OS)
                continue
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in _UNINSTALL_EXTS or not _UNINSTALL_NAME.match(stem):
                continue
            if ext == "" and (IS_WINDOWS or not os.access(entry, os.X_OK)):
                continue                          # extension-less: only executables
            # Prefer: inside this OS's folder, then shallower, then the extension order above.
            found.append((0 if os_match else 1, depth, f"{_UNINSTALL_EXTS.index(ext):02d}", entry))

    scan(app_dir, 0, False)
    return min(found)[3] if found else None


def uninstaller_command(script: Path, app_dir: Path) -> list[str]:
    """Command line that runs ``script`` non-interactively on this OS."""
    ext = script.suffix.lower()
    if ext in (".py", ".pyw"):
        # The app's own virtualenv (or the shared one next to the apps), else our interpreter.
        python = sys.executable
        for env in (app_dir / "env", app_dir / "venv", app_dir / ".venv", APPS_ROOT / "env"):
            exe = env / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
            if exe.is_file():
                python = str(exe)
                break
        return [python, str(script)]
    if ext == ".ps1":
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script)]
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", str(script)]
    if ext == ".vbs":
        return ["cscript", "//nologo", str(script)]
    if ext in (".sh", ".command"):
        return ["bash", str(script)]
    return [str(script)]   # .exe or an executable without extension


def run_uninstaller(script: Path, app_dir: Path) -> tuple[int, str]:
    """Run the uninstaller from the app folder; returns (exit code, last lines of output)."""
    kwargs = {}
    if IS_WINDOWS and script.suffix.lower() != ".exe":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ, UPDATEMANAGER_UNINSTALL="1")
    proc = subprocess.run(uninstaller_command(script, app_dir), cwd=str(app_dir), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", timeout=600, **kwargs)
    out = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-8:])
    return proc.returncode, out


def remove_app_folder(app_dir: Path) -> None:
    """Delete a managed app's folder. Refuses anything that is not a direct child of APPS_ROOT
    (or is Update Manager itself); a symlinked app folder only loses the link."""
    root = APPS_ROOT.resolve()
    if app_dir.parent.resolve() != root or app_dir.name in ("", ".", "..") \
            or app_dir.resolve() == APP_DIR:
        raise RuntimeError(f"refusing to delete {app_dir}")
    if app_dir.is_symlink():
        app_dir.unlink()
        return
    if not app_dir.exists():
        return

    def make_writable(func, path, _exc) -> None:
        # Read-only files (Windows, .git objects) or a read-only folder holding them: add write
        # permission for the owner and retry once (symlinks themselves are never chmod-ed).
        for target in (os.path.dirname(path), path):
            try:
                if not os.path.islink(target):
                    os.chmod(target, os.stat(target).st_mode | stat.S_IWRITE | stat.S_IREAD
                             | (stat.S_IXUSR if os.path.isdir(target) else 0))
            except OSError:
                pass
        func(path)
    if sys.version_info >= (3, 12):
        rmtree(app_dir, onexc=make_writable)
    else:
        rmtree(app_dir, onerror=make_writable)


# ── Desktop notifications ─────────────────────────────────────────────────────────────
def notify_desktop(title: str, body: str, hwnd: int | None = None) -> None:
    """Best-effort system notification (Linux notify-send, macOS Notification Center); on Windows
    the taskbar button flashes instead. Blocking: call it from a worker thread."""
    try:
        if IS_LINUX and which("notify-send"):
            icon = IMG_DIR / "Logo.png"
            subprocess.run(["notify-send", "--app-name", APP_TITLE,
                            *(["--icon", str(icon)] if icon.is_file() else []), title, body],
                           timeout=10, capture_output=True)
        elif IS_MAC:
            def q(text: str) -> str:
                return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
            subprocess.run(["osascript", "-e", f"display notification {q(body)} with title {q(title)}"],
                           timeout=10, capture_output=True)
        elif IS_WINDOWS and hwnd:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                            ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                            ("dwTimeout", wintypes.DWORD)]
            # FLASHW_ALL | FLASHW_TIMERNOFG: flash until the window comes to the foreground.
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # noqa: BLE001 — notifications are a nicety
        pass


def restart_self() -> None:
    """Relaunch this program (used after a self-update)."""
    os.execl(sys.executable, sys.executable, str(Path(__file__).resolve()))


# Parent window for dialogs (set by UpdateManagerApp once its root exists).
_DIALOG_PARENT = None


# Dialog icons: (light-theme colour, dark-theme colour, glyph colour light, glyph colour dark, glyph)
_DIALOG_ICONS = {
    "error":    ("#d64541", "#ff6b5e", "#ffffff", "#ffffff", "x"),
    "warning":  ("#e69a17", "#ffb547", "#ffffff", "#1d1d1d", "!"),
    "question": ("#009faa", "#29f7ff", "#ffffff", "#1d1d1d", "?"),
    "success":  ("#1fa463", "#3ddc84", "#ffffff", "#1d1d1d", "check"),
    "info":     ("#009faa", "#29f7ff", "#ffffff", "#1d1d1d", "i"),
}
_dialog_icon_cache: dict = {}


def _draw_dialog_icon(circle: str, ink: str, glyph: str) -> Image.Image:
    """A filled circle with a glyph, drawn large (and shown small) so it stays sharp at any scale."""
    size = 192
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, size - 4, size - 4), fill=circle)
    c, w = size / 2, 18
    if glyph == "x":
        k = 44
        d.line((c - k, c - k, c + k, c + k), fill=ink, width=w)
        d.line((c - k, c + k, c + k, c - k), fill=ink, width=w)
    elif glyph == "check":
        d.line((c - 48, c + 2, c - 14, c + 38, c + 50, c - 34), fill=ink, width=w, joint="curve")
    elif glyph in ("!", "i"):
        top, bottom = (c - 56, c + 18) if glyph == "!" else (c - 14, c + 56)
        d.rounded_rectangle((c - w / 2, top, c + w / 2, bottom), radius=w / 2, fill=ink)
        dot = c + 46 if glyph == "!" else c - 44
        d.ellipse((c - 12, dot - 12, c + 12, dot + 12), fill=ink)
    else:   # "?" as text
        try:
            font = ImageFont.load_default(size=130)
        except Exception:  # noqa: BLE001 — very old Pillow / no FreeType
            font = ImageFont.load_default()
        d.text((c, c + 4), glyph, fill=ink, font=font, anchor="mm")
    return im


# ── Canvas-drawn dialogs ──────────────────────────────────────────────────────────────
# Why: under XWayland every child X window costs about one display frame the first time it is
# drawn (~33 ms on a 4K/30 Hz screen, measured with plain Tk too: 40 small canvases ~1.5 s, the
# same 40 shapes on ONE canvas ~0.35 s). A customtkinter widget is 2-3 X windows, so a small
# dialog took 0.5-1.5 s to appear. CanvasForm draws a whole dialog on a single Canvas: shapes
# are anti-aliased PIL images (crisp on every OS, cached), texts are canvas items, and only a
# text field is a real widget. Colours follow the light/dark theme.
_WIN_BG = ThemeManager.theme["CTkToplevel"]["fg_color"]
_TEXT = ThemeManager.theme["CTkLabel"]["text_color"]
_ERROR_TEXT = ("#c0392b", "#ff7b6b")
_shape_cache: dict = {}


class CanvasForm:
    def __init__(self, master, win=None) -> None:
        self.win = win or master
        try:
            self.k = ScalingTracker.get_widget_scaling(self.win)
        except Exception:  # noqa: BLE001
            self.k = 1.0
        self.c = tkinter.Canvas(master, highlightthickness=0, bd=0)
        self._themed: list = []      # callables(theme index) that re-apply colours/images
        self._n = 0
        self.themed(lambda i: self.c.configure(bg=self.hex(_WIN_BG, i)))
        AppearanceModeTracker.add(self.repaint)
        self.c.bind("<Destroy>", lambda _e: AppearanceModeTracker.remove(self.repaint), add="+")

    # -- basics ------------------------------------------------------------------------
    def s(self, value: float) -> int:
        """Logical (unscaled) size -> pixels."""
        return round(value * self.k)

    def font(self, size: int = FONT_BODY[1], bold: bool = False) -> tuple:
        return (FONT_BODY[0], -self.s(size), "bold") if bold else (FONT_BODY[0], -self.s(size))

    def measure(self, text: str, size: int = FONT_BODY[1], bold: bool = False) -> int:
        return tkfont.Font(root=self.c, font=self.font(size, bold)).measure(text)

    @staticmethod
    def theme_index() -> int:
        return 1 if get_appearance_mode() == "Dark" else 0

    def hex(self, color, i: int) -> str:
        """Theme colour (a (light, dark) pair or one name such as "gray14") as #rrggbb."""
        color = color[i] if isinstance(color, (list, tuple)) else color
        r, g, b = self.c.winfo_rgb(color)
        return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"

    def themed(self, apply) -> None:
        self._themed.append(apply)
        apply(self.theme_index())

    def repaint(self, _mode=None) -> None:
        i = self.theme_index()
        for apply in self._themed:
            try:
                apply(i)
            except tkinter.TclError:   # canvas being destroyed
                pass

    def new_tag(self) -> str:
        self._n += 1
        return f"w{self._n}"

    def bbox(self, item) -> tuple[int, int, int, int]:
        return self.c.bbox(item)

    def size(self, width: int, height: int) -> None:
        self.c.configure(width=width, height=height)

    @staticmethod
    def shape_pil(w: int, h: int, radius: int, bg: str, fill: str | None = None,
                  outline: str | None = None, line: int = 0, knob: tuple | None = None) -> Image.Image:
        """Anti-aliased rounded rectangle (drawn 4x, then reduced) on an opaque ``bg``.
        ``knob`` = (x, diameter, colour) adds a switch knob."""
        ss = 4
        im = Image.new("RGB", (w * ss, h * ss), bg)
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=fill,
                            outline=outline, width=line * ss if outline else 0)
        if knob is not None:
            kx, kd, kc = knob
            inset = (h - kd) / 2
            d.ellipse((kx * ss, inset * ss, (kx + kd) * ss, (inset + kd) * ss), fill=kc)
        return im.resize((w, h), Image.LANCZOS)

    def shape(self, i: int, w: int, h: int, radius: int, fill: str | None = None,
              outline: str | None = None, line: int = 0, knob: tuple | None = None, bg=None):
        """``shape_pil`` as a cached PhotoImage, on the window background of theme ``i`` (or on
        ``bg``). It has NO alpha channel on purpose: Tk on X11 blends a transparent image by
        reading the screen back (XGetImage), which costs a display frame per image per redraw."""
        bg = self.hex(_WIN_BG if bg is None else bg, i)
        key = (w, h, radius, fill, outline, line, knob, bg)
        image = _shape_cache.get(key)
        if image is None:
            image = _shape_cache[key] = ImageTk.PhotoImage(
                self.shape_pil(w, h, radius, bg, fill, outline, line, knob), master=self.c)
        return image

    def photo(self, im: Image.Image, size: int, bg: str, base: Image.Image | None = None):
        """A (transparent) PIL image as an opaque PhotoImage of ``size`` px, centred on ``base``
        (an RGB image) or on the plain colour ``bg``. Cached; see ``shape`` for why opaque."""
        key = ("photo", id(im), size, bg, id(base))
        hit = _shape_cache.get(key)
        if hit is None:
            canvas = base.copy() if base is not None else Image.new("RGB", (size, size), bg)
            icon = im.convert("RGBA")
            if icon.size != (size, size):
                icon = icon.resize((size, size), Image.LANCZOS)
            canvas.paste(icon, ((canvas.width - size) // 2, (canvas.height - size) // 2), icon)
            hit = _shape_cache[key] = (im, base, ImageTk.PhotoImage(canvas, master=self.c))  # keep im/base alive
        return hit[2]

    def hover(self, tag: str, on_change) -> None:
        def enter(_e) -> None:
            self.c.configure(cursor="hand2")
            on_change(True)

        def leave(_e) -> None:
            self.c.configure(cursor="")
            on_change(False)
        self.c.tag_bind(tag, "<Enter>", enter)
        self.c.tag_bind(tag, "<Leave>", leave)

    # -- elements ----------------------------------------------------------------------
    def text(self, x: int, y: int, text: str, color=_TEXT, size: int = FONT_BODY[1],
             bold: bool = False, anchor: str = "nw", width: int = 0) -> int:
        item = self.c.create_text(x, y, text=text, anchor=anchor, font=self.font(size, bold),
                                  width=width, justify="left")
        self.themed(lambda i: self.c.itemconfigure(item, fill=self.hex(color, i)))
        return item

    def image(self, x: int, y: int, light, dark) -> int:
        item = self.c.create_image(x, y, anchor="nw", image=light)
        self.themed(lambda i: self.c.itemconfigure(item, image=dark if i else light))
        return item

    def button_width(self, text: str, minimum: int = 110) -> int:
        return max(self.s(minimum), self.measure(text, bold=True) + self.s(28))

    def button(self, x: int, y: int, text: str, command, primary: bool = False,
               w: int = 0, h: int = 0, link: bool = False) -> str:
        """Filled (primary), outlined, or borderless ``link`` button; command runs on mouse
        release. Returns its canvas tag (e.g. to move it)."""
        w, h = w or self.button_width(text), h or self.s(30)
        tag = self.new_tag()
        bg = self.c.create_image(x, y, anchor="nw", tags=tag)
        label = self.c.create_text(x + w / 2, y + h / 2, text=text, font=self.font(bold=not link), tags=tag)
        state = {"hover": False}

        def apply(i: int) -> None:
            primary_c = self.hex(PRIMARY, i)
            if link:
                fill = self.hex(TERTIARY2 if state["hover"] else _WIN_BG, i)
                img = self.shape(i, w, h, self.s(6), fill=fill)
                ink = primary_c
            elif primary:
                fill = self.hex(PRIMARY2 if state["hover"] else PRIMARY, i)
                img = self.shape(i, w, h, self.s(6), fill=fill)
                ink = self.hex(SECONDARY, i)
            else:
                fill = self.hex(TERTIARY2, i) if state["hover"] else None
                img = self.shape(i, w, h, self.s(6), fill=fill, outline=primary_c, line=max(1, self.s(1)))
                ink = primary_c
            self.c.itemconfigure(bg, image=img)
            self.c.itemconfigure(label, fill=ink)
        self.themed(apply)
        self.hover(tag, lambda on: (state.update(hover=on), apply(self.theme_index())))
        self.c.tag_bind(tag, "<ButtonRelease-1>", lambda _e: command())
        return tag

    def switch(self, x: int, y: int, text: str) -> "_Switch":
        return _Switch(self, x, y, text)

    def option(self, x: int, y: int, values: list[str], w: int = 180) -> "_Option":
        return _Option(self, x, y, values, self.s(w))

    def entry(self, x: int, y: int, w: int, h: int = 0) -> "_CanvasEntry":
        entry = _CanvasEntry(self, w, h or self.s(30))
        entry.place_at(x, y)
        return entry


class _CanvasEntry(tkinter.Entry):
    """A real tk Entry (keyboard, selection, clipboard) inside a rounded border drawn on a
    CanvasForm, with an optional grey placeholder shown while it is empty and unfocused
    (``get()`` returns "" then).

    ``lazy=True`` (for a field in a resizable window): while unfocused, the Entry window is
    hidden and its text is drawn on the canvas instead; clicking the field shows the Entry.
    A real child window that MOVES when the window is resized is exactly what XWayland fails
    to repaint (it flashes black until _repaint_moved catches it), so this avoids the glitch."""

    def __init__(self, form: CanvasForm, w: int, h: int, radius: int | None = None, placeholder: str = "",
                 bg=None, fill=None, border=None, lazy: bool = False) -> None:
        super().__init__(form.c, relief="flat", bd=0, highlightthickness=0, font=form.font())
        theme = ThemeManager.theme["CTkEntry"]
        self.f, self.w, self.h, self.lazy = form, w, h, lazy
        self.radius = form.s(6) if radius is None else radius
        self.colors = (bg or _WIN_BG, fill or theme["fg_color"], border or theme["border_color"], theme["text_color"])
        self.placeholder, self._ph, self._focus = placeholder, False, False
        self.tag = form.new_tag()
        self._border = form.c.create_image(0, 0, anchor="nw", tags=self.tag)
        inset = max(form.s(9), self.radius // 2)
        self._inset = inset
        self._mirror = form.c.create_text(0, 0, anchor="w", font=form.font(), text="", tags=self.tag)
        self._win = form.c.create_window(0, 0, anchor="w", window=self, width=w - 2 * inset, height=h - form.s(8),
                                         state="hidden" if lazy else "normal")
        self.bind("<FocusIn>", lambda _e: self._on_focus(True), add="+")
        self.bind("<FocusOut>", lambda _e: self._on_focus(False), add="+")
        if lazy:
            self.bind("<Map>", self._on_map, add="+")
            form.c.tag_bind(self.tag, "<ButtonPress-1>", lambda _e: self._activate(force=True))
            form.c.tag_bind(self.tag, "<Enter>", lambda _e: form.c.configure(cursor="xterm"))
            form.c.tag_bind(self.tag, "<Leave>", lambda _e: form.c.configure(cursor=""))
        form.themed(self._apply)
        self._show_placeholder()
        self._sync_mirror()

    def place_at(self, x: int, y: int) -> None:
        self.f.c.coords(self._border, x, y)
        self.f.c.coords(self._win, x + self._inset, y + self.h / 2)
        self.f.c.coords(self._mirror, x + self._inset + self.f.s(1), y + self.h / 2)

    def _activate(self, force: bool = False) -> None:
        """Show the hidden Entry and give it the keyboard focus. X refuses the focus to a window
        that is not viewable yet, so for a just-shown Entry the focus is taken on its <Map>
        event. A click on the field is the user choosing this window, so force=True may take
        the focus from another application. If the focus never arrives (the window manager can
        refuse it), the Entry is hidden again instead of staying around."""
        if self.lazy and not self.winfo_viewable():
            self._want_focus = force
            self.f.c.itemconfigure(self._win, state="normal")
            self.after(600, self._give_up_focus)
            return
        self._take_focus(force)

    def _take_focus(self, force: bool) -> None:
        if force:
            super().focus_force()
        else:
            super().focus_set()

    def _on_map(self, _event=None) -> None:
        want, self._want_focus = getattr(self, "_want_focus", None), None
        if want is not None:
            self._take_focus(want)

    def _give_up_focus(self) -> None:
        if self.lazy and not self._focus and self.focus_get() is not self:
            self._sync_mirror()   # hides the Entry again

    def focus_set(self) -> None:
        self._activate()

    def focus_force(self) -> None:
        self._activate(force=True)

    focus = focus_set

    def _sync_mirror(self) -> None:
        """Draw the Entry's text (or placeholder) on the canvas while the Entry is hidden."""
        if not self.lazy:
            return
        text = tkinter.Entry.get(self)
        if not self._focus:
            room = self.w - 2 * self._inset
            while text and self.f.measure(text) > room:
                text = text[:-2] + "…"
        self.f.c.itemconfigure(self._mirror, text="" if self._focus else text)
        self.f.c.itemconfigure(self._win, state="normal" if self._focus else "hidden")

    def get(self) -> str:
        return "" if self._ph else super().get()

    def clear(self) -> None:
        self._ph = False
        self.delete(0, "end")
        self._show_placeholder()
        self._sync_mirror()

    def _show_placeholder(self) -> None:
        if self.placeholder and not self._focus and not super().get():
            self.insert(0, self.placeholder)
            self._ph = True
            self._apply(self.f.theme_index())

    def _on_focus(self, focused: bool) -> None:
        self._focus = focused
        if focused and self._ph:
            self.delete(0, "end")
            self._ph = False
        self._show_placeholder()
        self._apply(self.f.theme_index())
        self._sync_mirror()

    def _apply(self, i: int) -> None:
        f = self.f
        bg, fill, border, text = self.colors
        fill_c = f.hex(fill, i)
        edge = f.hex(PRIMARY if self._focus else border, i)
        f.c.itemconfigure(self._border, image=f.shape(i, self.w, self.h, self.radius, fill=fill_c, outline=edge,
                                                      line=max(1, f.s(1.5)), bg=bg))
        ink = f.hex(text, i)
        muted = f.hex(QUATERNARY, i)
        self.configure(bg=fill_c, fg=muted if self._ph else ink, insertbackground=ink,
                       selectbackground=f.hex(PRIMARY, i), selectforeground=f.hex(SECONDARY, i))
        f.c.itemconfigure(self._mirror, fill=muted if self._ph else ink)


class _Panel:
    """A rounded rectangle of any width: two anti-aliased end caps plus a plain rectangle, so
    resizing never re-renders an image (the caps are cached per height)."""

    def __init__(self, form: CanvasForm, color, radius: int, bg=None, tag: str = "") -> None:
        self.f, self.color, self.r, self.bg, self.h = form, color, radius, bg or _WIN_BG, 0
        c = form.c
        self.tag = tag or form.new_tag()
        self._left = c.create_image(0, 0, anchor="nw", tags=self.tag)
        self._right = c.create_image(0, 0, anchor="ne", tags=self.tag)
        self._rect = c.create_rectangle(0, 0, 0, 0, width=0, tags=self.tag)
        form.themed(self._apply)

    def _caps(self, i: int):
        key = ("caps", self.r, self.h, self.f.hex(self.color, i), self.f.hex(self.bg, i))
        caps = _shape_cache.get(key)
        if caps is None:
            whole = self.f.shape_pil(2 * self.r + 2, self.h, self.r, key[4], fill=key[3])
            caps = _shape_cache[key] = tuple(
                ImageTk.PhotoImage(whole.crop(box), master=self.f.c)
                for box in ((0, 0, self.r + 1, self.h), (self.r + 1, 0, 2 * self.r + 2, self.h)))
        return caps

    def _apply(self, i: int) -> None:
        if self.h:
            left, right = self._caps(i)
            self.f.c.itemconfigure(self._left, image=left)
            self.f.c.itemconfigure(self._right, image=right)
        self.f.c.itemconfigure(self._rect, fill=self.f.hex(self.color, i))

    def place(self, x: int, y: int, w: int, h: int) -> None:
        if h != self.h:
            self.h = h
            self._apply(self.f.theme_index())
        c, r = self.f.c, self.r
        c.coords(self._left, x, y)
        c.coords(self._right, x + w, y)
        c.coords(self._rect, x + r, y, x + w - r, y + h)


class _CanvasLabel:
    """A canvas text that answers the CTkLabel calls the app makes: configure(text=,
    text_color=, cursor=) and cget()."""

    def __init__(self, form: CanvasForm, text: str, font: tuple, color=_TEXT, anchor: str = "w",
                 tag: str = "") -> None:
        self.f, self.color, self.cursor = form, color, ""
        self.item = form.c.create_text(0, 0, text=text, font=font, anchor=anchor, tags=tag or ())
        form.themed(self._apply)
        form.c.tag_bind(self.item, "<Enter>", lambda _e: form.c.configure(cursor=self.cursor))
        form.c.tag_bind(self.item, "<Leave>", lambda _e: form.c.configure(cursor=""))

    def _apply(self, i: int) -> None:
        self.f.c.itemconfigure(self.item, fill=self.f.hex(self.color, i))

    def place(self, x: float, y: float) -> None:
        self.f.c.coords(self.item, x, y)

    def configure(self, text: str | None = None, text_color=None, cursor: str | None = None) -> None:
        if text is not None:
            self.f.c.itemconfigure(self.item, text=text)
        if text_color is not None:
            self.color = text_color
            self._apply(self.f.theme_index())
        if cursor is not None:
            self.cursor = cursor

    def cget(self, key: str):
        if key == "text":
            return self.f.c.itemcget(self.item, "text")
        return {"text_color": self.color, "cursor": self.cursor}[key]


class _CanvasButton:
    """A button drawn on a CanvasForm that answers the CTkButton calls the app makes:
    configure(text=, state=, command=, border_color=) and cget("text"/"state").
    Styles: "primary" (filled), "outline", "ghost" (hover background only) and "icon"
    (a (light, dark) PIL icon pair, hover background)."""

    def __init__(self, form: CanvasForm, text: str, w: int, h: int, command=None, style: str = "outline",
                 radius: int | None = None, bg=SECONDARY, font: tuple | None = None, icons=None,
                 tag: str = "") -> None:
        self.f, self.w, self.h, self.style, self.bg, self.icons = form, w, h, style, bg, icons
        self.r = h // 2 if radius is None else radius
        self.command, self.state, self.hover, self.border = command, "normal", False, PRIMARY
        self.x = self.y = 0
        c = form.c
        self.tag = form.new_tag()
        tags = (self.tag, tag) if tag else (self.tag,)
        self._img = c.create_image(0, 0, anchor="nw", tags=tags)
        self._txt = c.create_text(0, 0, text="" if icons else text, font=font or form.font(bold=True), tags=tags)
        form.themed(self._apply)
        c.tag_bind(self.tag, "<Enter>", lambda _e: self._set_hover(True))
        c.tag_bind(self.tag, "<Leave>", lambda _e: self._set_hover(False))
        c.tag_bind(self.tag, "<ButtonRelease-1>", self._click)

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        self.f.c.configure(cursor="hand2" if on and self.state == "normal" else "")
        self._apply(self.f.theme_index())

    def _click(self, _e) -> None:
        if self.state == "normal" and self.command is not None:
            self.command()

    def _apply(self, i: int) -> None:
        f, w, h, r = self.f, self.w, self.h, self.r
        active, enabled = self.hover and self.state == "normal", self.state == "normal"
        if self.style == "primary":
            img = f.shape(i, w, h, r, fill=f.hex(PRIMARY2 if active else PRIMARY, i), bg=self.bg)
            ink = SECONDARY
        elif self.style == "outline":
            img = f.shape(i, w, h, r, fill=f.hex(TERTIARY2, i) if active else None, bg=self.bg,
                          outline=f.hex(self.border if enabled else TERTIARY2, i), line=max(1, f.s(1)))
            ink = PRIMARY if enabled else QUATERNARY
        else:   # ghost / icon: only a hover background
            fill = f.hex(TERTIARY2, i) if active else None
            if self.icons is not None:
                bg = f.hex(self.bg, i)
                key = ("iconbtn", w, h, r, fill, bg)
                base = _shape_cache.get(key)
                if base is None:
                    base = _shape_cache[key] = f.shape_pil(w, h, r, bg, fill=fill)
                img = f.photo(self.icons[i], min(w, h) - f.s(6), bg, base=base)
            else:
                img = f.shape(i, w, h, r, fill=fill, bg=self.bg)
            ink = QUATERNARY
        f.c.itemconfigure(self._img, image=img)
        f.c.itemconfigure(self._txt, fill=f.hex(ink, i))

    def place(self, x: float, y: float) -> None:
        self.x, self.y = x, y
        self.f.c.coords(self._img, x, y)
        self.f.c.coords(self._txt, x + self.w / 2, y + self.h / 2)

    def configure(self, text: str | None = None, state: str | None = None, command=None,
                  border_color=None, **_ignored) -> None:
        if text is not None:
            self.f.c.itemconfigure(self._txt, text=text)
        if state is not None:
            self.state = state
            if state != "normal" and self.hover:
                self.f.c.configure(cursor="")
        if command is not None:
            self.command = command
        if border_color is not None:
            self.border = border_color
        self._apply(self.f.theme_index())

    def cget(self, key: str):
        return self.f.c.itemcget(self._txt, "text") if key == "text" else self.state

    def screen_box(self) -> tuple[int, int, int, int]:
        """(x1, y1, x2, y2) on screen, taking the canvas scroll position into account."""
        c = self.f.c
        x = c.winfo_rootx() + int(self.x - c.canvasx(0))
        y = c.winfo_rooty() + int(self.y - c.canvasy(0))
        return x, y, x + self.w, y + self.h


class _CanvasScrollbar:
    """Vertical scrollbar drawn inside a scrolling canvas, in a gutter at its right edge
    (kept in view as the canvas scrolls). Same look as before: a subtle rounded handle, shown
    only when the content overflows. Drag the handle, click the track to page, or use the
    wheel (bound by the owner). Drawing it on the canvas, instead of a separate scrollbar
    widget, keeps the window free of child windows that move on resize (see _CanvasEntry)."""

    def __init__(self, form: CanvasForm, gutter: int) -> None:
        self.f, self.gutter = form, gutter
        c, px = form.c, form.s
        self.thick = px(8)
        self._track = c.create_rectangle(0, 0, 0, 0, width=0, state="hidden")
        self._thumb = c.create_line(0, 0, 0, 0, width=self.thick, capstyle="round", state="hidden")
        self.hover, self._drag, self.first, self.last = False, None, 0.0, 1.0
        c.tag_bind(self._thumb, "<Enter>", lambda _e: self._set_hover(True))
        c.tag_bind(self._thumb, "<Leave>", lambda _e: self._set_hover(False))
        c.tag_bind(self._thumb, "<ButtonPress-1>", self._press)
        c.tag_bind(self._thumb, "<B1-Motion>", self._motion)
        c.tag_bind(self._thumb, "<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))
        c.tag_bind(self._track, "<ButtonPress-1>", self._page)
        form.themed(self._apply)

    def _apply(self, i: int) -> None:
        f = self.f
        f.c.itemconfigure(self._track, fill=f.hex(_WIN_BG, i))
        f.c.itemconfigure(self._thumb, fill=f.hex(QUATERNARY if self.hover or self._drag else SECONDARY, i))

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        self._apply(self.f.theme_index())

    def _track_span(self) -> tuple[float, float]:
        """(top, height) of the track in canvas coordinates."""
        c, pad = self.f.c, self.f.s(4) + self.thick / 2
        return c.canvasy(0) + pad, c.winfo_height() - 2 * pad

    def _press(self, event) -> None:
        self._drag = (event.y, self.first)

    def _motion(self, event) -> None:
        if self._drag is not None:
            _top, span = self._track_span()
            y0, first0 = self._drag
            self.f.c.yview_moveto(first0 + (event.y - y0) / max(1.0, span))

    def _page(self, event) -> None:
        top, span = self._track_span()
        thumb_top = top + self.first * span
        self.f.c.yview_scroll(-1 if self.f.c.canvasy(event.y) < thumb_top else 1, "pages")

    def set(self, first, last) -> None:
        """yscrollcommand of the canvas: place the handle for the visible fraction."""
        c = self.f.c
        self.first, self.last = float(first), float(last)
        if self.last - self.first >= 0.999:
            c.itemconfigure(self._track, state="hidden")
            c.itemconfigure(self._thumb, state="hidden")
            return
        width = c.winfo_width()
        top, span = self._track_span()
        x = width - self.gutter / 2
        c.coords(self._track, width - self.gutter, c.canvasy(0), width, c.canvasy(0) + c.winfo_height())
        c.coords(self._thumb, x, top + self.first * span, x, top + self.last * span)
        c.itemconfigure(self._track, state="normal")
        c.itemconfigure(self._thumb, state="normal")
        c.tag_raise(self._track)
        c.tag_raise(self._thumb)


class _CanvasTile:
    """App-icon tile: a rounded square showing a (light, dark) PIL icon pair, one PhotoImage
    (spinner frame) or a placeholder letter. configure(image=..., text=...) like CTkLabel."""
    _UNSET = object()

    def __init__(self, form: CanvasForm, size: int, radius: int, icon_size: int, bg=SECONDARY,
                 color=TERTIARY2, tag: str = "") -> None:
        self.f, self.size, self.r, self.icon_size, self.bg, self.color = form, size, radius, icon_size, bg, color
        self.content = None
        c = form.c
        tags = (tag,) if tag else ()
        self._tile = c.create_image(0, 0, anchor="nw", tags=tags)
        self._icon = c.create_image(0, 0, tags=tags)
        self._letter = c.create_text(0, 0, text="", font=form.font(FONT_SUBTITLE[1], bold=True), tags=tags)
        form.themed(self._apply)

    def _apply(self, i: int) -> None:
        f = self.f
        tile = f.hex(self.color, i)
        f.c.itemconfigure(self._tile, image=f.shape(i, self.size, self.size, self.r, fill=tile, bg=self.bg))
        content = self.content
        if isinstance(content, tuple):
            image = f.photo(content[i], self.icon_size, tile)
        else:
            image = content or ""
        f.c.itemconfigure(self._icon, image=image)
        f.c.itemconfigure(self._letter, fill=f.hex(_TEXT, i))

    def place(self, x: float, y: float) -> None:
        c, half = self.f.c, self.size / 2
        c.coords(self._tile, x, y)
        c.coords(self._icon, x + half, y + half)
        c.coords(self._letter, x + half, y + half)

    def configure(self, image=_UNSET, text: str | None = None) -> None:
        if image is not self._UNSET:
            self.content = image
        if text is not None:
            self.f.c.itemconfigure(self._letter, text=text)
        self._apply(self.f.theme_index())


class _Switch:
    """On/off switch drawn as one image, with its label; click anywhere on it to toggle."""

    def __init__(self, form: CanvasForm, x: int, y: int, text: str) -> None:
        self.f, self.value = form, False
        s = form.s
        self.w, self.h = s(36), s(18)
        self.tag = form.new_tag()
        self._img = form.c.create_image(x, y, anchor="nw", tags=self.tag)
        self._label = form.c.create_text(x + self.w + s(8), y + self.h / 2, anchor="w", text=text,
                                         font=form.font(), tags=self.tag)
        form.themed(self._apply)
        form.hover(self.tag, lambda _on: None)
        form.c.tag_bind(self.tag, "<ButtonRelease-1>", lambda _e: self.toggle())

    # Track colour when off: customtkinter's grey in the light theme; in the dark theme a lighter
    # grey than its default (#4A4D50), so the dark knob stays visible on it.
    OFF_TRACK = (ThemeManager.theme["CTkSwitch"]["fg_color"][0], "#6b6f73")

    def _apply(self, i: int) -> None:
        f = self.f
        track = f.hex(PRIMARY if self.value else self.OFF_TRACK, i)
        knob_d = self.h - f.s(6)
        knob_x = self.w - knob_d - f.s(3) if self.value else f.s(3)
        # Knob colour = SECONDARY, the same token as the text inside the buttons (white in the light
        # theme, the same dark grey in the dark theme); the CTkSwitch default read wrong on our track.
        f.c.itemconfigure(self._img, image=f.shape(i, self.w, self.h, self.h // 2, fill=track,
                                                   knob=(knob_x, knob_d, f.hex(SECONDARY, i))))
        f.c.itemconfigure(self._label, fill=f.hex(_TEXT, i))

    def get(self) -> bool:
        return self.value

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self._apply(self.f.theme_index())

    def toggle(self) -> None:
        self.set(not self.value)


class _Option:
    """Drop-down selector: a filled box showing the value; click opens a themed menu."""

    def __init__(self, form: CanvasForm, x: int, y: int, values: list[str], w: int) -> None:
        self.f, self.values, self.value = form, list(values), values[0] if values else ""
        s = form.s
        self.x, self.y, self.w, self.h = x, y, w, s(30)
        self.hovered = False
        self.tag = form.new_tag()
        self._img = form.c.create_image(x, y, anchor="nw", tags=self.tag)
        self._label = form.c.create_text(x + s(10), y + self.h / 2, anchor="w", text=self.value,
                                         font=form.font(), tags=self.tag)
        ax, ay = x + w - s(18), y + self.h / 2   # chevron
        self._arrow = form.c.create_line(ax - s(5), ay - s(2.5), ax, ay + s(2.5), ax + s(5), ay - s(2.5),
                                         width=max(1, s(2)), capstyle="round", joinstyle="round",
                                         tags=self.tag)
        form.themed(self._apply)
        form.hover(self.tag, self._hover)
        form.c.tag_bind(self.tag, "<ButtonRelease-1>", lambda _e: self._open())

    def _hover(self, on: bool) -> None:
        self.hovered = on
        self._apply(self.f.theme_index())

    def _apply(self, i: int) -> None:
        f = self.f
        fill = f.hex(PRIMARY2 if self.hovered else PRIMARY, i)
        f.c.itemconfigure(self._img, image=f.shape(i, self.w, self.h, f.s(6), fill=fill))
        ink = f.hex(SECONDARY, i)
        f.c.itemconfigure(self._label, fill=ink)
        f.c.itemconfigure(self._arrow, fill=ink)

    def _open(self) -> None:
        menu = make_menu(self.f.c, self.f.k)
        for value in self.values:
            menu.add_command(value, lambda v=value: self.set(v))
        c = self.f.c
        menu.popup(c.winfo_rootx() + self.x, c.winfo_rooty() + self.y + self.h)

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value
        self.f.c.itemconfigure(self._label, text=value)

    def set_values(self, values: list[str]) -> None:
        self.values = list(values)


_menu_pad_cache: dict = {}


def themed_menu(master, scale: float = 1.0) -> tkinter.Menu:
    """A tk Menu in the app's colours. On Linux its font is scaled like the rest of the UI."""
    i = 1 if get_appearance_mode() == "Dark" else 0
    k = scale if IS_LINUX else 1.0
    text = _TEXT[i] if isinstance(_TEXT, (list, tuple)) else _TEXT
    menu = tkinter.Menu(master, tearoff=0, bg=SECONDARY[i], fg=text, relief="flat",
                        activebackground=TERTIARY2[i], activeforeground=PRIMARY[i],
                        disabledforeground=QUATERNARY[i], activeborderwidth=0,
                        borderwidth=max(1, round(k)), font=(FONT_BODY[0], -round(FONT_BODY[1] * k)))
    menu._pad_scale = k
    return menu


def menu_item(menu: tkinter.Menu, label: str, command, enabled: bool = True) -> None:
    # Tk menus have no item padding option; a blank (transparent) image beside each label makes
    # the rows taller and indents the text, like the rest of the app.
    k = getattr(menu, "_pad_scale", 1.0)
    pad = _menu_pad_cache.get(k)
    if pad is None:
        pad = _menu_pad_cache[k] = tkinter.PhotoImage(master=menu, width=round(8 * k), height=round(30 * k))
    menu.add_command(label=f"{label}      ", image=pad, compound="left", command=command,
                     state="normal" if enabled else "disabled")


class _TkMenu:
    """Linux popup menu: a themed tkinter.Menu. It is kept on X11 because the native menu handles
    its own pointer grab there (that is what closes it on a click outside); see _row_menu."""

    def __init__(self, master, scale: float = 1.0, on_close=None) -> None:
        self.menu = themed_menu(master, scale)
        if on_close is not None:
            self.menu.bind("<Unmap>", lambda _e: on_close())

    def add_command(self, label: str, command, enabled: bool = True) -> None:
        menu_item(self.menu, label, command, enabled)

    def add_separator(self) -> None:
        self.menu.add_separator()

    @property
    def width(self) -> int:
        self.menu.update_idletasks()
        return self.menu.winfo_reqwidth()

    def popup(self, x: int, y: int) -> None:
        try:
            self.menu.tk_popup(x, y)
        finally:
            if not IS_LINUX:
                self.menu.grab_release()

    def destroy(self) -> None:
        try:
            self.menu.destroy()
        except Exception:  # noqa: BLE001
            pass


class _PopupMenu:
    """A rounded, borderless popup menu drawn on one canvas, in the app's colours. tkinter.Menu
    renders as a native, square, white-bordered popup on Windows/macOS; this matches the rest of
    the UI. Rows highlight on hover; a choice, a click elsewhere or Esc closes it. On Windows 11
    the window corners are rounded by DWM (the window is borderless, so there is no frame)."""

    def __init__(self, master, scale: float = 1.0, on_close=None) -> None:
        self.master, self.on_close = master, on_close
        self.items: list = []            # (label|None, command|None, enabled)
        self._win = None
        self._geom = None

    def add_command(self, label: str, command, enabled: bool = True) -> None:
        self.items.append((label, command, bool(enabled)))

    def add_separator(self) -> None:
        self.items.append((None, None, True))

    def _measure(self):
        if self._geom is not None:
            return self._geom
        probe = CanvasForm(self.master)        # only for scale/measuring; never shown
        s = probe.s
        pad_y, row_h, sep_h, indent = s(6), s(30), s(9), s(14)
        text_w = max((probe.measure(lbl) for lbl, _c, _e in self.items if lbl), default=s(80))
        width = indent * 2 + text_w
        rows, yy = [], pad_y
        for lbl, cmd, enabled in self.items:
            if lbl is None:
                rows.append(("sep", yy, sep_h, None, None, None)); yy += sep_h
            else:
                rows.append(("item", yy, row_h, lbl, cmd, enabled)); yy += row_h
        try:
            probe.c.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._geom = (width, yy + pad_y, rows, indent)
        return self._geom

    @property
    def width(self) -> int:
        return self._measure()[0]

    def _close(self) -> None:
        win, self._win = self._win, None
        if win is not None:
            for fn in (win.grab_release, win.destroy):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        if self.on_close is not None:
            self.on_close()

    def destroy(self) -> None:
        win, self._win = self._win, None
        if win is not None:
            for fn in (win.grab_release, win.destroy):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass

    def popup(self, x: int, y: int) -> None:
        width, height, rows, indent = self._measure()
        win = self._win = tkinter.Toplevel(self.master)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        f = CanvasForm(win)
        f.themed(lambda i: f.c.configure(bg=f.hex(SECONDARY, i)))   # panel colour (overrides _WIN_BG)
        f.size(width, height)
        f.c.pack(fill="both", expand=True)

        def choose(cmd):
            self._close()
            cmd()

        for kind, ry, rh, lbl, cmd, enabled in rows:
            if kind == "sep":
                ln = f.c.create_line(indent, ry + rh / 2, width - indent, ry + rh / 2)
                f.themed(lambda i, ln=ln: f.c.itemconfigure(ln, fill=f.hex(QUATERNARY, i)))
                continue
            tag = f.new_tag()
            hl = f.c.create_rectangle(f.s(5), ry, width - f.s(5), ry + rh, width=0, tags=tag)
            tx = f.c.create_text(indent, ry + rh / 2, anchor="w", text=lbl, font=f.font(), tags=tag)
            state = {"hover": False}

            def paint(i, hl=hl, tx=tx, enabled=enabled, state=state):
                if not enabled:
                    f.c.itemconfigure(hl, fill=f.hex(SECONDARY, i), outline=f.hex(SECONDARY, i))
                    f.c.itemconfigure(tx, fill=f.hex(QUATERNARY, i))
                elif state["hover"]:
                    f.c.itemconfigure(hl, fill=f.hex(TERTIARY2, i), outline=f.hex(TERTIARY2, i))
                    f.c.itemconfigure(tx, fill=f.hex(PRIMARY, i))
                else:
                    f.c.itemconfigure(hl, fill=f.hex(SECONDARY, i), outline=f.hex(SECONDARY, i))
                    f.c.itemconfigure(tx, fill=f.hex(_TEXT, i))
            f.themed(paint)
            if enabled:
                f.hover(tag, lambda on, state=state, paint=paint: (state.update(hover=on),
                                                                   paint(f.theme_index())))
                f.c.tag_bind(tag, "<ButtonRelease-1>", lambda _e, cmd=cmd: choose(cmd))

        # Keep the menu fully on screen.
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(int(x), sw - width))
        y = max(0, min(int(y), sh - height))
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.deiconify()
        if IS_WINDOWS:   # round the (borderless) window corners natively on Windows 11
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)   # GA_ROOT
                pref = ctypes.c_int(2)                                       # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref),
                                                           ctypes.sizeof(pref))
            except Exception:  # noqa: BLE001 — older Windows: stays square but still borderless
                pass
        for grab in (win.grab_set_global, win.grab_set):
            try:
                grab()
                break
            except Exception:  # noqa: BLE001
                continue
        win.focus_set()
        win.bind("<Escape>", lambda _e: self._close())
        win.bind("<Button-1>", self._maybe_outside, add="+")

    def _maybe_outside(self, event) -> None:
        win = self._win
        if win is None:
            return
        rx, ry = event.x_root - win.winfo_rootx(), event.y_root - win.winfo_rooty()
        if not (0 <= rx < win.winfo_width() and 0 <= ry < win.winfo_height()):
            self._close()


def make_menu(master, scale: float = 1.0, on_close=None):
    """A popup menu: the themed native menu on Linux (its X11 grab handling is relied on), the
    rounded, borderless canvas menu on Windows/macOS."""
    return _TkMenu(master, scale, on_close) if IS_LINUX else _PopupMenu(master, scale, on_close)


def _dialog_photos(form: CanvasForm, tone: str, size: int):
    """(light, dark) PhotoImages of a dialog icon at ``size`` pixels, or None."""
    spec = _DIALOG_ICONS.get(tone)
    if spec is None:
        return None
    key = (tone, size)
    if key not in _dialog_icon_cache:
        photos = []
        for t in (0, 1):   # opaque, on each theme's window background (see CanvasForm.shape)
            icon = _draw_dialog_icon(spec[t], spec[t + 2], spec[4]).resize((size, size), Image.LANCZOS)
            flat = Image.new("RGB", (size, size), form.hex(_WIN_BG, t))
            flat.paste(icon, (0, 0), icon)
            photos.append(ImageTk.PhotoImage(flat, master=form.c))
        _dialog_icon_cache[key] = tuple(photos)
    return _dialog_icon_cache[key]


def _set_toplevel_icon(win) -> None:
    """Give a Toplevel the app icon. On Windows this also stops customtkinter replacing it with
    its own icon 200 ms later (it only does that when iconbitmap was never called on the window)."""
    try:
        ico = IMG_DIR / "Favicon.ico"
        png = IMG_DIR / "Logo.png"
        if IS_WINDOWS and ico.is_file():
            win.iconbitmap(str(ico))
        elif png.is_file():
            from tkinter import PhotoImage
            win._app_icon_ref = PhotoImage(file=str(png), master=win)   # keep a reference alive
            win.iconphoto(False, win._app_icon_ref)
    except Exception:  # noqa: BLE001 — non-fatal cosmetic
        pass


def message_dialog(title: str, message: str, buttons: tuple[str, ...] = ("OK",),
                   tone: str = "info", wait: bool = True) -> str | None:
    """Small dialog in the app's own style. The LAST button is the primary one (Enter);
    Esc / closing returns None. (CTkMessagebox was replaced because it renders deformed under
    HiDPI scaling; this one is drawn on a single canvas, see CanvasForm.)"""
    parent = _DIALOG_PARENT
    win = CTkToplevel(parent)
    win.withdraw()
    _set_toplevel_icon(win)
    win.title(title)
    win.resizable(False, False)
    if parent is not None:
        win.transient(parent)
    result: dict = {"value": None}

    def finish(value) -> None:
        result["value"] = value
        try:
            win.grab_release()
        except Exception:  # noqa: BLE001
            pass
        win.destroy()

    f = CanvasForm(win)
    s = f.s
    pad_x, pad_y, icon = s(24), s(20), s(40)
    text_x = pad_x
    photos = _dialog_photos(f, tone, icon)
    if photos is not None:
        f.image(pad_x, pad_y + s(2), *photos)
        text_x += icon + s(16)
    head = f.text(text_x, pad_y, title, color=_ERROR_TEXT if tone == "error" else PRIMARY,
                  size=FONT_SUBTITLE[1], bold=True)
    body = f.text(text_x, f.bbox(head)[3] + s(6), message, width=s(420))
    widths = [f.button_width(label) for label in buttons]
    gap = s(10)
    width = max(max(f.bbox(head)[2], f.bbox(body)[2]) + pad_x,
                2 * pad_x + sum(widths) + gap * (len(buttons) - 1))
    y = max(f.bbox(body)[3], pad_y + s(2) + icon) + s(18)
    x = width - pad_x
    for i, (label, w) in enumerate(zip(reversed(buttons), reversed(widths))):
        x -= w
        f.button(x, y, label, lambda v=label: finish(v), primary=i == 0, w=w)
        x -= gap
    f.size(width, y + s(30) + s(20))
    f.c.pack(fill="both", expand=True)
    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    win.bind("<Escape>", lambda _e: finish(None))
    win.bind("<Return>", lambda _e: finish(buttons[-1]))
    win.update_idletasks()
    win.deiconify()
    win.lift()
    win.after(100, win.focus_force)
    if not wait:
        return None
    try:
        win.wait_visibility()
        win.grab_set()          # modal while waiting for an answer
    except Exception:  # noqa: BLE001
        pass
    win.wait_window()
    return result["value"]


def show_error(title: str, message: str) -> None:
    message_dialog(title, message, ("OK",), tone="error", wait=False)


def ask_yes_no(title: str, message: str, yes="Update", no="Later") -> bool:
    return message_dialog(title, message, (no, yes), tone="question") == yes


def plain_release_notes(markdown: str) -> str:
    """GitHub release notes (Markdown) as readable plain text: headings become UPPERCASE
    lines, list markers become bullets, emphasis/code markers and link targets are dropped."""
    lines = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            line = heading.group(1).strip().upper()
        else:
            line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)     # [text](url) -> text
        line = re.sub(r"(\*\*|__|`)", "", line)                    # bold / code markers
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ── Single instance ──────────────────────────────────────────────────────────────────
# Opening Update Manager while it is already running brings the existing window to the
# front instead of starting a second copy (any OS). The running instance listens on a
# localhost-only port derived from the user name; a new launch knocks, and quits if the
# running instance answers. Anything else on that port is ignored (it won't answer).
def _instance_id() -> tuple[int, bytes]:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha1(f"updatemanager:{user}".encode("utf-8")).digest()
    port = 40000 + int.from_bytes(digest[:2], "big") % 20000
    return port, b"updatemanager-raise:" + digest.hex()[:16].encode() + b"\n"


def signal_running_instance() -> bool:
    """True if another instance of this user is running (it has been asked to show itself)."""
    port, hello = _instance_id()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as conn:
            conn.settimeout(1.0)
            conn.sendall(hello)
            return conn.recv(64) == b"ok\n"
    except OSError:
        return False


# ── GUI ───────────────────────────────────────────────────────────────────────────────
class UpdateManagerApp:
    def __init__(self) -> None:
        self.config = read_config()
        migrate_token_out_of_config(self.config)
        self.owner = self.config.get("GitHub", "username", fallback="").strip()
        self.token = load_token()
        self._session = None                  # created on first network use (see session)
        self._session_lock = threading.Lock()
        self.apps = parse_apps(self.config)
        self.rows: dict[str, dict] = {}
        self._spin_running = False
        self._notified: set = set()          # (app, version) pairs already announced
        self._recheck_job = None
        self._settings_win = None
        # Results of background work, handed back to the Tk thread by _poll_bg (Tk is not
        # thread-safe, so worker threads never touch widgets directly).
        self._bg_results: queue.Queue = queue.Queue()
        self.app_title = self.config.get("UpdateManager", "title", fallback=APP_TITLE).strip() or APP_TITLE
        self.heading = self.config.get("UpdateManager", "heading", fallback=APP_HEADING).strip() or APP_HEADING

        # UI scaling ("auto" / "off" / a number; see _apply_ui_scale). On Linux "auto" reads the
        # desktop scale from Xft.dpi. Applied before the window is created.
        self._apply_ui_scale(self.config.get("UpdateManager", "ui_scale", fallback="").strip())
        # Theme chosen with the header button last time ("system" follows the desktop).
        appearance = self.config.get("UpdateManager", "appearance", fallback="system").strip().lower()
        if appearance in ("light", "dark"):
            set_appearance_mode(appearance)

        # customtkinter draws rounded corners on Linux with glyphs of its own shapes font
        # ("font_shapes"); under HiDPI/XWayland those glyphs come out at the wrong size, giving
        # square/clipped corners, missing button borders and a "two circles" scrollbar. Canvas
        # polygons render correctly at any scale (the method customtkinter already uses on macOS).
        if IS_LINUX:
            DrawEngine.preferred_drawing_method = "polygon_shapes"
            # In "system" appearance mode customtkinter polls the desktop theme every 30 ms, and on
            # Linux each poll spawns a `gsettings` process (darkdetect): ~30 processes/s of constant
            # CPU load, very noticeable on small machines. Once a second still follows theme changes.
            AppearanceModeTracker.update_loop_interval = 1000

        # Windows groups pythonw under Python's taskbar icon. Give the process its own
        # AppUserModelID (before the window exists) so the taskbar uses OUR window icon.
        if IS_WINDOWS:
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ariel-MN.UpdateManager")
            except Exception:  # noqa: BLE001
                pass

        # className sets the X11 WM_CLASS (-> "Updatemanager"); the .desktop's StartupWMClass
        # matches it so the Linux taskbar shows our icon and groups with the launcher.
        self.root = CTk(className="updatemanager")
        # Keep the window unmapped until it is fully built at its final size: if it is shown
        # earlier (any idle update maps it at the min size), the window manager centres THAT
        # size and the later resize leaves it off-centre / makes it jump. Shown in _show_window.
        self.root.withdraw()
        global _DIALOG_PARENT
        _DIALOG_PARENT = self.root
        self._fix_linux_tk_scaling()
        self.root.title(f"{self.app_title} {__version__}")
        self.root.minsize(*self.MIN_SIZE)
        self._set_window_icon()

        self._build_header()
        self._build_list()

        # Remember the window SIZE across runs (not the position); save it on close.
        self._restore_window_size()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Keyboard: F5 / Ctrl+R re-check every app, Ctrl+Q / Ctrl+W close (Command on macOS).
        mod = "Command" if IS_MAC else "Control"
        for seq in ("<F5>", f"<{mod}-r>", f"<{mod}-R>"):
            self.root.bind(seq, lambda _e: self._refresh_states())
        for seq in (f"<{mod}-q>", f"<{mod}-Q>", f"<{mod}-w>", f"<{mod}-W>"):
            self.root.bind(seq, lambda _e: self._on_close())
        for seq in (f"<{mod}-f>", f"<{mod}-F>"):   # Ctrl+F: search the list
            self.root.bind(seq, lambda _e: self._focus_search())
        self.root.bind(f"<{mod}-comma>", lambda _e: self._open_settings())
        if IS_LINUX:
            self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._show_window()
        self._poll_bg()
        self._start_instance_server()

        # After the window is shown: self-update prompt, then refresh managed-app states.
        self.root.after(250, self._startup_checks)

    @property
    def session(self) -> requests.Session:
        """The GitHub HTTP session, created on first use (often from a worker thread)."""
        with self._session_lock:
            if self._session is None:
                self._session = github_session(self.token)
            return self._session

    def _set_window_icon(self) -> None:
        """Give the window the app icon: Favicon.ico on Windows, Logo.png elsewhere; on macOS
        also set the Dock icon via PyObjC (if available)."""
        ico = IMG_DIR / "Favicon.ico"
        png = IMG_DIR / "Logo.png"
        try:
            if IS_WINDOWS and ico.is_file():
                self.root.iconbitmap(str(ico))
            elif png.is_file():
                from tkinter import PhotoImage
                self._icon_img = PhotoImage(file=str(png))  # keep a reference alive
                self.root.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001 — non-fatal cosmetic
            pass
        if IS_MAC and png.is_file():
            try:  # the Dock icon of a plain script is Python's unless we set it (needs pyobjc)
                from AppKit import NSApplication, NSImage
                image = NSImage.alloc().initByReferencingFile_(str(png))
                NSApplication.sharedApplication().setApplicationIconImage_(image)
            except Exception:  # noqa: BLE001 — pyobjc missing or unavailable: keep default icon
                pass

    _desktop_scale: float | None = None   # cached: xrdb is queried once per run

    @staticmethod
    def _read_xft_dpi() -> float | None:
        """Xft.dpi from the X resource database; None if unset; raises OSError if no xrdb."""
        try:
            out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True,
                                 timeout=3).stdout
        except subprocess.SubprocessError:
            return None
        m = re.search(r"^Xft\.dpi:\s*([\d.]+)", out, re.MULTILINE)
        try:
            return float(m.group(1)) if m and float(m.group(1)) > 0 else None
        except ValueError:
            return None

    @classmethod
    def _linux_desktop_scale(cls) -> float:
        """Desktop scale factor an X11/XWayland client should use, from ``Xft.dpi`` (96 = 1.0).

        GNOME/KDE publish the effective scale there (e.g. 192 on Wayland at 150 %/200 % with
        native XWayland scaling, where X clients are drawn at 2x). Tk and customtkinter do not
        apply it to widget geometry on Linux, so we read it ourselves. Falls back to
        GDK_SCALE / QT_SCALE_FACTOR, then 1.0."""
        if cls._desktop_scale is not None:
            return cls._desktop_scale
        dpi = None
        try:
            dpi = cls._read_xft_dpi()
            # On Wayland, XWayland is started on demand by the first X client and the desktop
            # publishes Xft.dpi a moment later. Right after login this app can BE that first
            # client (it then came up unscaled), so give the desktop a few seconds.
            if dpi is None and os.environ.get("WAYLAND_DISPLAY"):
                deadline = time.monotonic() + 3.0
                while dpi is None and time.monotonic() < deadline:
                    time.sleep(0.15)
                    dpi = cls._read_xft_dpi()
        except OSError:          # xrdb not installed
            pass
        scale = dpi / 96.0 if dpi else 0.0
        if not scale:
            for var in ("GDK_SCALE", "QT_SCALE_FACTOR"):
                try:
                    value = float(os.environ.get(var, "") or 0)
                except ValueError:
                    value = 0.0
                if value > 0:
                    scale = value
                    break
        cls._desktop_scale = scale or 1.0
        return cls._desktop_scale

    def _apply_ui_scale(self, raw: str) -> None:
        """Decide how the app scales (called before the window is created).

        - "auto"/blank: automatic. Windows/macOS keep customtkinter's own DPI handling (works
          there). On Linux customtkinter has no DPI detection, so the desktop scale is read from
          Xft.dpi and applied to widgets + window (see ``_fix_linux_tk_scaling`` for fonts).
        - "off"/"native"/"none": no scaling at all (1.0).
        - a number (1.5, 2.0): force that scale."""
        value = raw.lower()
        self._ui_scale = 1.0
        if value in ("off", "native", "none"):
            deactivate_automatic_dpi_awareness()
            return
        if not value or value == "auto":
            if not IS_LINUX:
                return
            deactivate_automatic_dpi_awareness()
            scale = self._linux_desktop_scale()
        else:
            try:
                scale = float(value.replace(",", "."))
            except ValueError:
                return
        if scale > 0:
            self._ui_scale = scale
            set_widget_scaling(scale)
            set_window_scaling(scale)

    def _fix_linux_tk_scaling(self) -> None:
        """Make Tk's font pixels match real pixels on Linux (call right after creating the root).

        customtkinter gives fonts in pixels; Tk converts them to points with ``tk scaling`` (from
        the physical DPI, usually 96) and Xft then renders those points at Xft.dpi. With
        Xft.dpi=192 that doubles every font on top of our own widget scaling, overflowing and
        clipping buttons/labels. Setting ``tk scaling`` to Xft.dpi/72 makes the round trip exact."""
        if not IS_LINUX:
            return
        try:
            self.root.tk.call("tk", "scaling", self._linux_desktop_scale() * 96.0 / 72.0)
        except Exception:  # noqa: BLE001
            pass

    # -- window size memory (remembers SIZE across runs, not position) ------------------
    # Sizes are stored in LOGICAL units (CTk.geometry undoes customtkinter's window scaling), so
    # the remembered size stays right if the desktop scale changes between runs.
    DEFAULT_SIZE = (900, 600)
    MIN_SIZE = (820, 420)

    def _restore_window_size(self) -> None:
        size = self.config.get("UpdateManager", "window_size", fallback="").strip()
        m = re.fullmatch(r"(\d+)x(\d+)", size) if size else None
        w, h = (int(m.group(1)), int(m.group(2))) if m else self.DEFAULT_SIZE
        w, h = max(w, self.MIN_SIZE[0]), max(h, self.MIN_SIZE[1])
        try:
            # Size only, no "+x+y": the window manager places it (centred on GNOME/Windows).
            self.root.geometry(f"{w}x{h}")
        except Exception:  # noqa: BLE001
            pass

    # -- Linux/XWayland repaint workaround ---------------------------------------------
    # When the window is resized, widgets that MOVE (everything packed to the right) are not
    # repainted under XWayland: the server believes it copied the content of the moved window's
    # sub-windows and never sends them an Expose, so they stay blank (background colour) until
    # something else redraws them. Every customtkinter widget is a Frame holding a Canvas/Label,
    # i.e. exactly that case. Reproduced with plain Tk (Frame > Canvas) on Raspberry Pi 5 /
    # GNOME 50. Fix: after a size change, ask Tk to redraw the Canvas/Label widgets that moved.
    # Only the moved ones: redrawing everything (image labels especially) costs 0.3-0.7 s per
    # resize on a 4K window and made the app feel sluggish. Two passes, because a redraw done
    # right after the resize can land before the compositor picked up the new window buffer.
    _REPAINT_PASSES_MS = (60, 350)

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if size == getattr(self, "_last_root_size", None):
            return
        self._last_root_size = size
        for job in getattr(self, "_repaint_jobs", ()):
            self.root.after_cancel(job)
        self._repaint_jobs = [self.root.after(self._REPAINT_PASSES_MS[0], self._repaint_moved)]

    def _drawable_widgets(self, path: str = "."):
        """Yield (path, class, x, y) of every Canvas/Label/Button/Entry, x/y relative to the window."""
        tk = self.root.tk
        try:
            children = tk.splitlist(tk.call("winfo", "children", path))
        except Exception:  # noqa: BLE001 — widget destroyed meanwhile
            return
        for child in children:
            try:
                if not int(tk.call("winfo", "ismapped", child)):
                    continue          # hidden (e.g. the unfocused search field): nothing to repaint
                cls = str(tk.call("winfo", "class", child))
                if cls in ("Canvas", "Label", "Button", "Entry"):
                    yield (str(child), cls, int(tk.call("winfo", "rootx", child)),
                           int(tk.call("winfo", "rooty", child)))
            except Exception:  # noqa: BLE001
                continue
            yield from self._drawable_widgets(child)

    def _widget_positions(self) -> dict:
        ox, oy = self.root.winfo_rootx(), self.root.winfo_rooty()
        return {w: (cls, x - ox, y - oy) for w, cls, x, y in self._drawable_widgets()}

    def _repaint_moved(self) -> None:
        try:
            self.root.update_idletasks()  # let geometry managers place everything first
            now = self._widget_positions()
        except Exception:  # noqa: BLE001 — window closing
            return
        before = getattr(self, "_widget_pos", {})
        self._widget_pos = now
        moved = [(w, cls) for w, (cls, x, y) in now.items()
                 if w not in before or before[w][1:] != (x, y)]
        if not moved:
            return
        self._repaint(moved)
        self._repaint_jobs = [self.root.after(self._REPAINT_PASSES_MS[1], self._repaint, moved)]

    def _repaint(self, widgets) -> None:
        tk = self.root.tk
        for w, cls in widgets:
            try:
                # Re-setting an option to its own value makes Tk schedule a full redraw.
                opt = "-text" if cls in ("Label", "Button") else "-background"
                tk.call(w, "configure", opt, tk.call(w, "cget", opt))
            except Exception:  # noqa: BLE001 — widget destroyed meanwhile
                continue

    # -- single instance: another launch asks this window to come to the front ------------
    def _start_instance_server(self) -> None:
        port, hello = _instance_id()
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if IS_WINDOWS:   # Windows SO_REUSEADDR would let a second process bind the same port
                srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
            else:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(4)
        except OSError:
            return   # port busy (e.g. a simultaneous start): just run without it

        def serve() -> None:
            while True:
                try:
                    conn, _addr = srv.accept()
                except OSError:
                    return
                with conn:
                    try:
                        conn.settimeout(1.0)
                        if conn.recv(64) == hello:
                            conn.sendall(b"ok\n")
                            self._bg_results.put((lambda _r, _e: self._bring_to_front(), None, None))
                    except OSError:
                        pass
        threading.Thread(target=serve, daemon=True).start()

    def _bring_to_front(self) -> None:
        try:
            if IS_LINUX and self.root.state() == "normal":
                # A window minimised by the compositor (XWayland) still reports "normal", so a
                # plain deiconify is a no-op; withdraw + deiconify re-maps it and the window
                # manager shows and activates it (size is kept).
                self.root.withdraw()
                self.root.update_idletasks()
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)    # some WMs only raise a "topmost" window
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass

    # -- background work: network never runs on the Tk thread ---------------------------
    def _run_bg(self, work, done=None) -> None:
        """Run ``work()`` in a daemon thread, then call ``done(result, error)`` on the Tk thread."""
        def target() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # noqa: BLE001
                result, error = None, exc
            if done is not None:
                self._bg_results.put((done, result, error))
        threading.Thread(target=target, daemon=True).start()

    def _poll_bg(self) -> None:
        try:
            while True:
                done, result, error = self._bg_results.get_nowait()
                try:
                    done(result, error)
                except Exception as exc:  # noqa: BLE001
                    print(f"background callback failed: {exc}")
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._poll_bg)
        except Exception:  # noqa: BLE001 — window destroyed
            pass

    def _show_window(self) -> None:
        """Map the window once, already at its final size, so the WM places it (centred) for good."""
        try:
            self.root.update_idletasks()
            self._layout_header()
            self._layout_rows()
            if IS_LINUX:
                self._widget_pos = self._widget_positions()  # baseline for _repaint_moved
            self.root.deiconify()
            # Keyboard focus on the window itself, never on the search field: it only becomes
            # active when clicked or with Ctrl+F (typing elsewhere must not start a search).
            self.root.focus_set()
            if IS_WINDOWS:
                # Must run from inside the event loop: Tk maps the window (honouring the inherited
                # SW_HIDE) on its first idle pass, AFTER this method returns, so a synchronous call
                # here would be overridden. Scheduled twice in case the first runs before the map.
                self.root.after(50, self._force_show_windows)
                self.root.after(300, self._force_show_windows)
        except Exception:  # noqa: BLE001
            pass

    def _force_show_windows(self) -> None:
        """A silent launcher (e.g. the .vbs uses Run(..., 0) so no console flashes) starts us with
        STARTUPINFO wShowWindow = SW_HIDE, which Tk applies to its first window; deiconify() then
        leaves it withdrawn. Force it visible with the Win32 call so first launch isn't invisible."""
        try:
            if self.root.state() == "normal" and self.root.winfo_ismapped():
                return
            import ctypes
            self.root.deiconify()
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT -> the top-level window
            user32.ShowWindow(hwnd, 1)                          # SW_SHOWNORMAL
            user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass

    def _save_window_size(self) -> None:
        try:
            # Don't remember maximised/fullscreen sizes. Windows/macOS report maximised as
            # state "zoomed"; on X11 the state stays "normal", so check the -zoomed attribute.
            if self.root.state() != "normal":
                return
            for attr in ("-zoomed", "-fullscreen"):
                try:
                    if int(self.root.attributes(attr)):
                        return
                except Exception:  # noqa: BLE001 — attribute not supported on this platform
                    pass
            m = re.match(r"(\d+)x(\d+)", self.root.geometry())
            if not m:
                return
            if not self.config.has_section("UpdateManager"):
                self.config.add_section("UpdateManager")
            self._save_setting("window_size", f"{m.group(1)}x{m.group(2)}")
        except Exception:  # noqa: BLE001
            pass

    def _save_setting(self, key: str, value: str, section: str = "UpdateManager") -> None:
        """Persist one option in config.ini (best effort)."""
        try:
            if not self.config.has_section(section):
                self.config.add_section(section)
            self.config.set(section, key, value)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                self.config.write(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"config: could not save {key}: {exc}")

    def _on_close(self) -> None:
        busy = [r["app"]["name"] for r in self.rows.values() if r.get("_busy")]
        if busy and message_dialog(
                "Update in progress",
                f"{', '.join(busy)} is still being installed, updated or uninstalled. Closing "
                f"now can leave it half-done.", ("Close anyway", "Wait"), tone="warning") != "Close anyway":
            return   # "Wait" (the default for Enter) or Esc keeps the window open
        self._save_window_size()
        self.root.destroy()

    # -- construction ------------------------------------------------------------------
    # The header and the list are each ONE canvas (CanvasForm): the window used to be 91 child
    # X windows (customtkinter widgets are 2-3 each) and took ~5 s to appear under XWayland,
    # where every child window costs a display frame (see the note above CanvasForm). Widgets
    # that need it stay real: the search field (an Entry) and the scrollbar.
    HEADER_H = 150      # logical px, header panel height
    ROW_H = 70          # logical px, one app row
    ROW_GAP = 4

    @staticmethod
    def _icon_pair(name: str):
        """(light, dark) PIL images of img/<name>_L.png / _D.png (either one for both), or None."""
        light, dark = IMG_DIR / f"{name}_L.png", IMG_DIR / f"{name}_D.png"
        if not (light.is_file() or dark.is_file()):
            return None
        try:
            return (Image.open(light if light.is_file() else dark),
                    Image.open(dark if dark.is_file() else light))
        except OSError:
            return None

    def _build_header(self) -> None:
        f = self._head = CanvasForm(self.root)
        px = f.s
        f.c.configure(height=px(self.HEADER_H + 10))
        f.c.pack(fill="x")
        self._head_panel = _Panel(f, SECONDARY, px(6))
        logo = IMG_DIR / "Logo.png"
        if logo.is_file():
            try:
                im = Image.open(logo)
                pair = [f.photo(im, px(70), f.hex(SECONDARY, t)) for t in (0, 1)]
                f.image(px(25), px(5) + (px(self.HEADER_H) - px(70)) // 2, *pair)
            except OSError:
                pass
        f.text(px(115), px(5 + 62), self.heading, size=FONT_TITLE[1], bold=True, anchor="w")
        f.text(px(115), px(5 + 112), 'Download and install app updates by clicking "Get updates"',
               size=FONT_SUBTITLE[1], bold=True, anchor="w")
        icon = px(30)
        theme_icons, cog_icons = self._icon_pair("Theme"), self._icon_pair("Cog")
        self._theme_btn = _CanvasButton(f, "Theme", icon if theme_icons else px(70), icon, self._toggle_theme,
                                        style="icon" if theme_icons else "ghost", radius=px(6), icons=theme_icons)
        self._cog_btn = _CanvasButton(f, "Settings", icon if cog_icons else px(80), icon, self._open_settings,
                                      style="icon" if cog_icons else "ghost", radius=px(6), icons=cog_icons)
        self._search = _CanvasEntry(f, px(210), px(28), radius=px(14), placeholder="Search apps",
                                    bg=SECONDARY, fill=TERTIARY2, border=TERTIARY2, lazy=True)
        self._search.bind("<KeyRelease>", lambda _e: self._apply_filter())
        self._search.bind("<Escape>", lambda _e: self._clear_search())
        self._get_btn = _CanvasButton(f, "Get updates", px(150), px(30), self._get_all_updates,
                                      style="primary", radius=px(6))
        f.c.bind("<Configure>", lambda _e: self._layout_header(), add="+")

    def _layout_header(self) -> None:
        f = self._head
        px, width = f.s, f.c.winfo_width()
        if width <= 1:
            return
        right = width - px(5)
        self._head_panel.place(px(5), px(5), right - px(5), px(self.HEADER_H))
        x = right - px(8) - self._theme_btn.w
        self._theme_btn.place(x, px(10))
        x -= px(4) + self._cog_btn.w
        self._cog_btn.place(x, px(10))
        x -= px(10) + self._search.w
        self._search.place_at(x, px(10) + (px(30) - self._search.h) // 2)
        g = self._get_btn
        g.place(right - px(20) - g.w, px(5 + 112) - g.h // 2)

    def _build_list(self) -> None:
        # The scrollbar is drawn in a gutter at the right of the list canvas; the gutter is
        # always reserved, so showing the scrollbar never shifts the rows.
        f = self._list = CanvasForm(self.root)
        px = f.s
        self._sb_gutter = px(16)
        self._scrollbar = _CanvasScrollbar(f, self._sb_gutter)
        f.c.configure(yscrollcommand=self._scrollbar.set, yscrollincrement=px(24), width=1, height=1)
        f.c.pack(fill="both", expand=True, padx=px(5), pady=(0, px(5)))
        f.c.bind("<Configure>", lambda _e: self._layout_rows(), add="+")
        if IS_LINUX:
            f.c.bind("<Button-4>", lambda _e: self._wheel(-3))
            f.c.bind("<Button-5>", lambda _e: self._wheel(3))
        else:   # Windows reports multiples of 120, macOS small steps
            f.c.bind("<MouseWheel>", lambda e: self._wheel(-e.delta // 120 if IS_WINDOWS else -e.delta))
        self._list_note = f.text(0, 0, "", color=QUATERNARY, anchor="n", width=px(560))
        f.c.itemconfigure(self._list_note, justify="center")

        menu_seqs = ("<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)
        for app in self.apps:
            tag = f.new_tag()
            row = {"app": app, "tag": tag, "hidden": False,
                   "panel": _Panel(f, SECONDARY, px(6), tag=tag),
                   "_icon_label": _CanvasTile(f, px(50), px(8), px(40), tag=tag),
                   "name": _CanvasLabel(f, app["name"], f.font(FONT_SUBTITLE[1], bold=True), anchor="nw", tag=tag),
                   "ver": _CanvasLabel(f, app["version"] or "—", f.font(), anchor="nw", tag=tag),
                   "msg": _CanvasLabel(f, "Checking…", f.font(bold=True), anchor="e", tag=tag),
                   "btn": _CanvasButton(f, "…", px(140), px(28), tag=tag),
                   "more": _CanvasButton(f, "⋯", px(32), px(32), style="ghost", tag=tag,
                                         font=f.font(FONT_SUBTITLE[1]))}
            row["btn"].configure(state="disabled")
            self.rows[app["section"]] = row
            row["more"].configure(command=lambda r=row: self._row_menu(r))
            f.c.tag_bind(row["msg"].item, "<ButtonRelease-1>", lambda _e, r=row: self._on_message_click(r))
            # Right-click anywhere on the row opens the same menu (Ctrl+click / button 2 on macOS).
            for seq in menu_seqs:
                f.c.tag_bind(tag, seq, lambda e, r=row: self._row_menu(r, e))
            self._init_app_icon(row)

    def _layout_rows(self) -> None:
        """Place every visible row for the current canvas width (cheap: it only moves items)."""
        f = self._list
        c, px = f.c, f.s
        width = c.winfo_width()
        if width <= 1:
            return
        x0, x1, y, h = px(2), width - self._sb_gutter, px(2), px(self.ROW_H)
        for row in self.rows.values():
            if row["hidden"]:
                c.itemconfigure(row["tag"], state="hidden")
                continue
            c.itemconfigure(row["tag"], state="normal")
            row["panel"].place(x0, y, x1 - x0, h)
            row["_icon_label"].place(x0 + px(15), y + px(10))
            row["name"].place(x0 + px(80), y + px(12))
            row["ver"].place(x0 + px(80), y + px(38))
            more, btn = row["more"], row["btn"]
            more.place(x1 - px(8) - more.w, y + (h - more.h) // 2)
            btn.place(more.x - px(4) - btn.w, y + (h - btn.h) // 2)
            row["msg"].place(btn.x - px(50), y + h / 2)
            y += h + px(self.ROW_GAP)
        if not self.rows:
            note = ("No apps configured yet.\nAdd an [App1] section with the app's GitHub repo name "
                    "to config.ini, then restart Update Manager.")
        elif all(r["hidden"] for r in self.rows.values()):
            note = "No apps match your search."
        else:
            note = ""
        c.itemconfigure(self._list_note, text=note)
        c.coords(self._list_note, (width - self._sb_gutter) / 2, px(40))
        self._content_h = y
        c.configure(scrollregion=(0, 0, width, max(y, c.winfo_height())))
        self._update_scrollbar()

    def _overflows(self) -> bool:
        return getattr(self, "_content_h", 0) > self._list.c.winfo_height() + 1

    def _wheel(self, steps: int) -> None:
        if self._overflows():
            self._list.c.yview_scroll(steps, "units")

    def _update_scrollbar(self) -> None:
        """Re-place the drawn scrollbar (hidden when the rows fit; its gutter stays reserved)."""
        c = self._list.c
        if not self._overflows():
            c.yview_moveto(0)
        self._scrollbar.set(*c.yview())

    # -- app icons (downloaded on demand; img/ fills itself) ---------------------------
    @staticmethod
    def _resolve_specs(app: dict) -> tuple[str, str]:
        """Light/dark icon specs after the single-colour rule: if only one is given, it is
        used for both themes."""
        light, dark = app.get("icon_light", ""), app.get("icon_dark", "")
        if light and not dark:
            dark = light
        if dark and not light:
            light = dark
        return light, dark

    @staticmethod
    def _cache_path(name: str, theme: str, spec: str) -> Path:
        tag = hashlib.sha1(spec.encode("utf-8")).hexdigest()[:10]
        return ICON_CACHE / f"{name}_{theme}_{tag}.png"

    def _image_path(self, app: dict, theme: str, spec: str) -> Path | None:
        """Where the image for this theme currently is, or None if not available yet."""
        if spec:
            p = self._cache_path(app["name"], theme, spec)
            return p if p.is_file() else None
        local = IMG_DIR / f"{app['name']}_{theme}.png"   # hand-placed fallback
        return local if local.is_file() else None

    def _assemble_icon(self, app: dict) -> tuple | None:
        """(light, dark) PIL icons for the app's tile, or None if none is available yet."""
        light, dark = self._resolve_specs(app)
        lp = self._image_path(app, "L", light)
        dp = self._image_path(app, "D", dark)
        if lp and not dp:
            dp = lp
        if dp and not lp:
            lp = dp
        if not lp:
            return None
        try:
            l_img, d_img = self._small_icon(lp), self._small_icon(dp)
            if light == dark or lp == dp:   # one icon for both themes: keep a grey glyph visible on both tiles
                l_img = self._contrast_for(l_img, TERTIARY2[0])
                d_img = self._contrast_for(d_img, TERTIARY2[1])
            return l_img, d_img
        except Exception:  # noqa: BLE001
            return None

    def _small_icon(self, path: Path) -> Image.Image:
        """The icon already reduced to its on-screen size, cached in img/.cache: decoding and
        reducing the original (often a 1024 px PNG) on every start cost ~20 ms per icon."""
        size = self._list.s(40)
        small = ICON_CACHE / f"{path.stem}@{size}.png"
        try:
            if small.is_file() and small.stat().st_mtime >= path.stat().st_mtime:
                return Image.open(small)
        except OSError:
            pass
        img = Image.open(path).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        try:
            ICON_CACHE.mkdir(parents=True, exist_ok=True)
            img.save(small)
        except OSError:
            pass
        return img

    @classmethod
    def _contrast_for(cls, img: Image.Image, bg_hex: str) -> Image.Image:
        """A single-colour (grey) icon that is too close in brightness to the tile behind it
        (e.g. a white glyph on the light theme) gets its tones inverted for that theme.
        Coloured icons are returned untouched."""
        rgba = img.convert("RGBA")
        small = rgba.resize((48, 48))
        mask = small.getchannel("A").point(lambda a: 255 if a > 128 else 0)   # opaque pixels
        if mask.getbbox() is None:
            return img
        red, green, blue = small.convert("RGB").split()
        spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(red, green), blue),
                                     ImageChops.darker(ImageChops.darker(red, green), blue))
        if ImageStat.Stat(spread, mask).extrema[0][1] > 40:   # has colour: leave it alone
            return img
        lum = ImageStat.Stat(small.convert("L"), mask).mean[0]   # L = 0.299 R + 0.587 G + 0.114 B
        r, g, b = cls._hex_rgb(bg_hex)
        if abs(lum - (r * 0.299 + g * 0.587 + b * 0.114)) >= 90:
            return img
        red, green, blue, alpha = rgba.split()
        inverted = Image.merge("RGB", (red, green, blue)).point(lambda v: 255 - v)
        inverted.putalpha(alpha)
        return inverted

    def _init_app_icon(self, row: dict) -> None:
        app = row["app"]
        light, dark = self._resolve_specs(app)
        needed = []
        if self.owner:
            for theme, spec in (("L", light), ("D", dark)):
                if spec and not self._cache_path(app["name"], theme, spec).is_file():
                    needed.append((theme, spec))
        if needed:
            self._start_spinner(row)
            threading.Thread(target=self._icon_worker, args=(row, needed), daemon=True).start()
        else:
            self._apply_icon(row, self._assemble_icon(app))

    def _icon_worker(self, row: dict, needed: list[tuple[str, str]]) -> None:
        app = row["app"]
        try:
            ICON_CACHE.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for theme, spec in needed:
            dest = self._cache_path(app["name"], theme, spec)
            try:
                if spec.startswith(("http://", "https://")):
                    _download(self.session, spec, dest)
                else:
                    fetch_repo_file(self.session, self.owner, app["name"], spec, dest)
                Image.open(dest).verify()           # reject non-images
            except Exception:                        # noqa: BLE001
                try:
                    dest.unlink()
                except OSError:
                    pass
        self._bg_results.put((lambda _res, _err: self._finish_icon(row), None, None))

    def _finish_icon(self, row: dict) -> None:
        row["_spinning"] = False
        self._apply_icon(row, self._assemble_icon(row["app"]))

    def _apply_icon(self, row: dict, image: tuple | None) -> None:
        label = row["_icon_label"]
        try:
            if image is not None:
                row["_icon_ctk"] = image
                label.configure(image=image, text="")
            else:                                  # placeholder: first letter
                label.configure(image=None, text=(row["app"]["name"][:1] or "?").upper())
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _hex_rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    def _build_spinner_frames(self) -> None:
        """Pre-render a rotating ring whose tail fades out (a spinning arc with a gradient tail),
        as opaque frames on the icon tile of the current theme."""
        mode = get_appearance_mode()
        r, g, b = self._hex_rgb(PRIMARY[1] if mode == "Dark" else PRIMARY[0])
        size, frames_n, segments = 48, 12, 72
        box = [6, 6, size - 6, size - 6]
        seg = 360 / segments
        frames = []
        for f in range(frames_n):
            im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            head = f * (360 / frames_n)         # advance the other way (reverse spin direction)
            for s in range(segments):
                a = s * seg
                rel = (head - a) % 360          # angular distance behind the head (tail)
                alpha = int(255 * max(0.0, 1.0 - rel / 300.0))   # bright head -> transparent tail
                if alpha <= 0:
                    continue
                d.arc(box, a, a + seg + 1.5, fill=(r, g, b, alpha), width=5)
            i = 1 if mode == "Dark" else 0
            frames.append(self._list.photo(im, self._list.s(26), self._list.hex(TERTIARY2, i)))
        self._spin_frames = frames
        self._spin_mode = mode

    def _start_spinner(self, row: dict) -> None:
        row["_spinning"] = True
        if not self._spin_running:
            self._spin_running = True
            self._spin_i = 0
            self._tick_spinner()

    def _tick_spinner(self) -> None:
        active = [r for r in self.rows.values() if r.get("_spinning")]
        if not active:
            self._spin_running = False
            return
        if getattr(self, "_spin_frames", None) is None or self._spin_mode != get_appearance_mode():
            self._build_spinner_frames()
        frame = self._spin_frames[self._spin_i % len(self._spin_frames)]
        self._spin_i += 1
        for r in active:
            try:
                r["_icon_label"].configure(image=frame, text="")
            except Exception:  # noqa: BLE001
                pass
        self.root.after(90, self._tick_spinner)

    # -- behaviour ---------------------------------------------------------------------
    def _toggle_theme(self) -> None:
        mode = "light" if get_appearance_mode() == "Dark" else "dark"
        set_appearance_mode(mode)
        self._save_setting("appearance", mode)   # remembered for the next launch

    # Re-check every app periodically while the window stays open (skipped while busy).
    # [UpdateManager] recheck_minutes in config.ini (0 = never); editable in Settings.
    RECHECK_CHOICES = (("15 minutes", 15), ("30 minutes", 30), ("1 hour", 60), ("2 hours", 120),
                       ("6 hours", 360), ("Never", 0))

    def _recheck_minutes(self) -> int:
        try:
            return max(0, self.config.getint("UpdateManager", "recheck_minutes", fallback=30))
        except ValueError:
            return 30

    def _notifications_on(self) -> bool:
        try:
            return self.config.getboolean("UpdateManager", "notifications", fallback=True)
        except ValueError:
            return True

    def _schedule_recheck(self) -> None:
        if self._recheck_job is not None:
            self.root.after_cancel(self._recheck_job)
            self._recheck_job = None
        minutes = self._recheck_minutes()
        if minutes:
            self._recheck_job = self.root.after(minutes * 60 * 1000, self._periodic_recheck)

    def _periodic_recheck(self) -> None:
        self._recheck_job = None
        self._schedule_recheck()
        if self.owner and not any(r.get("_busy") or r.get("_checking") for r in self.rows.values()):
            self._refresh_states()

    def _show_no_owner(self) -> None:
        for row in self.rows.values():
            if row.get("_busy"):
                continue
            row["_error"] = None
            self._apply_state(row, None)
            self._set_message(row, "Set your GitHub username in Settings", "warn")

    def _startup_checks(self) -> None:
        self._schedule_recheck()
        if not self.owner:   # nothing can be checked without the GitHub account
            self._show_no_owner()
            return
        if self.owner and self.config.getboolean("UpdateManager", "check_on_startup", fallback=True):
            self._run_bg(lambda: get_latest_version(self.session, self.owner, SELF_REPO),
                         self._on_self_latest)
        self._refresh_states()

    def _on_self_latest(self, latest: str | None, _error) -> None:
        if latest and has_update(__version__, latest):
            if ask_yes_no(f"Update available {latest}",
                          f"A new version of {self.app_title} is available ({latest}).\n"
                          f"You are on {__version__}. Update now?"):
                self._self_update(latest)

    def _self_update(self, latest: str) -> None:
        self._run_bg(lambda: apply_release(self.session, self.owner, SELF_REPO, latest, APP_DIR),
                     lambda _res, error: self._self_update_done(latest, error))

    def _self_update_done(self, latest: str, error) -> None:
        if error is not None:
            show_error("Update failed", f"Could not update {self.app_title}: {error}")
            return
        # Waits for OK, so the message is actually readable before the restart.
        message_dialog("Update completed",
                       f"{self.app_title} was updated to {latest}. It will now restart.",
                       tone="success")
        self._save_window_size()
        self.root.destroy()
        restart_self()

    def _refresh_states(self, auto_update: bool = False) -> None:
        if not self.owner:
            return
        for row in self.rows.values():
            self._refresh_one(row, auto_update)

    def _refresh_one(self, row: dict, auto_update: bool = False) -> None:
        if row.get("_busy") or row.get("_checking"):
            return
        row["_checking"] = True
        self._set_message(row, "Checking…")
        name = row["app"]["name"]

        def work():
            release = get_latest_release(self.session, self.owner, name, strict=True)
            if not release:
                return None, "", ""
            return release.get("tag_name"), release.get("body") or "", release.get("html_url") or ""

        def done(result, error) -> None:
            if result:
                row["_notes"] = result[1:]
            self._apply_state(row, result[0] if result else None, error, auto_update)
        self._run_bg(work, done)

    def _apply_state(self, row: dict, latest: str | None, error=None, auto_update: bool = False) -> None:
        row["_checking"] = False
        app = row["app"]
        if error is not None:
            # Could not reach GitHub: keep what we knew, say so, and let the user act anyway.
            # Clicking the red text shows the reason (offline, rate limit, HTTP error…).
            row["_error"] = str(error) or error.__class__.__name__
            label = "Couldn't check ⓘ"
            if not app["version"]:
                self._set_action(row, "Install", lambda r=row: self._install(r), label, "warn")
            else:
                self._set_action(row, "Open", lambda r=row: self._open(r), label, "warn")
            self._maybe_notify()
            return
        row["_error"] = None
        row["_latest"] = latest
        # A version in config.ini whose folder is gone (deleted by hand) simply counts as not installed.
        if not app["version"] or not (APPS_ROOT / app["name"]).is_dir():
            row["ver"].configure(text="—")
            self._set_action(row, "Install", lambda r=row: self._install(r), "No version installed")
        elif latest and has_update(app["version"], latest):
            self._set_action(row, "Update", lambda r=row: self._update(r), f"Update available ({latest})", "accent")
            if auto_update:
                self._update(row)
        elif latest is None:
            self._set_action(row, "Open", lambda r=row: self._open(r), "No releases", "muted")
        else:
            self._set_action(row, "Open", lambda r=row: self._open(r), "Up to date")
        self._maybe_notify()

    # Status text colours: accent = needs attention, warn = problem, muted = nothing to do.
    _MSG_COLORS = {"accent": PRIMARY, "warn": ("#c0392b", "#ff7b6b"), "muted": QUATERNARY}

    def _set_message(self, row: dict, message: str, tone: str = "") -> None:
        color = self._MSG_COLORS.get(tone) or ThemeManager.theme["CTkLabel"]["text_color"]
        clickable = ((tone == "warn" and row.get("_error")) or message.startswith("Update available")
                     or message.startswith("Set your GitHub"))
        row["msg"].configure(text=message, text_color=color, cursor="hand2" if clickable else "")
        self._sync_header()

    def _set_action(self, row: dict, text: str, command, message: str, tone: str = "") -> None:
        row["btn"].configure(text=text, state="normal", border_color=PRIMARY, command=command)
        self._set_message(row, message, tone)

    def _on_message_click(self, row: dict) -> None:
        text = row["msg"].cget("text")
        if text.startswith("Update available"):
            self._show_notes(row)
        elif text.startswith("Couldn't check"):
            self._show_row_error(row)
        elif text.startswith("Set your GitHub"):
            self._open_settings()

    def _show_notes(self, row: dict) -> None:
        """What's new in the available version (the GitHub release notes), with Update now.
        A plain Text + scrollbar and a canvas-drawn button bar: 4 X windows instead of 17
        (see the note above CanvasForm), so it opens about twice as fast under XWayland."""
        old = row.get("_notes_win")
        if old is not None and old.winfo_exists():
            old.focus_force()
            return
        name, tag = row["app"]["name"], row.get("_latest") or ""
        body, url = row.get("_notes") or ("", "")
        win = CTkToplevel(self.root)
        win.withdraw()
        _set_toplevel_icon(win)
        row["_notes_win"] = win
        win.title(f"{name} {tag} — what's new")
        win.transient(self.root)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)

        bar = CanvasForm(win)
        px = bar.s
        theme = ThemeManager.theme["CTkTextbox"]
        box = tkinter.Text(win, wrap="word", font=bar.font(), relief="flat", bd=0, highlightthickness=0,
                           padx=px(10), pady=px(8), width=1, height=1)
        box.insert("1.0", plain_release_notes(body) or "No release notes were published for this version.")
        box.configure(state="disabled")
        scroll = CTkScrollbar(win, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        bar.themed(lambda i: box.configure(bg=bar.hex(theme["fg_color"], i), fg=bar.hex(theme["text_color"], i),
                                           selectbackground=bar.hex(PRIMARY, i),
                                           selectforeground=bar.hex(SECONDARY, i)))
        box.grid(row=0, column=0, sticky="nsew", padx=(px(12), 0), pady=(px(12), px(6)))
        scroll.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=(12, 6))   # customtkinter scales these
        bar.c.grid(row=1, column=0, columnspan=2, sticky="ew")

        def update_now() -> None:
            win.destroy()
            self._update(row)
        pad, y, gap = px(12), 0, px(10)
        w_update, w_close = bar.button_width("Update now"), bar.button_width("Close")
        right = [bar.button(-w_update, y, "Update now", update_now, primary=True, w=w_update),
                 bar.button(-w_update - gap - w_close, y, "Close", win.destroy, w=w_close)]
        if url:
            bar.button(pad, y, "Open on GitHub", lambda: webbrowser.open(url), link=True,
                       w=bar.measure("Open on GitHub") + px(20))
        bar.size(px(560) + 2 * pad, px(30) + px(12))
        placed = {"x": 0}

        def keep_right(event) -> None:   # the window is resizable: keep Update now/Close right-aligned
            target = event.width - pad
            for t in right:
                bar.c.move(t, target - placed["x"], 0)
            placed["x"] = target
        bar.c.bind("<Configure>", keep_right, add="+")
        line = tkfont.Font(root=win, font=bar.font()).metrics("linespace")
        box.configure(height=max(8, round(px(340) / line)))   # ~340 px of text
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        win.deiconify()
        win.after(100, win.focus_force)

    def _show_row_error(self, row: dict) -> None:
        if row.get("_error") and row["msg"].cget("text").startswith("Couldn't check"):
            show_error(f"Couldn't check {row['app']['name']}",
                       f"GitHub could not be reached or refused the request:\n{row['_error']}\n\n"
                       f"Check the connection and press F5 to try again.")

    def _open(self, row: dict) -> None:
        """Launch the app; briefly show "Launching…" and ignore repeat clicks for a moment, since
        an app can take a few seconds to show its window (avoids opening it twice)."""
        if row.get("_launching"):
            return
        row["_launching"] = True
        previous = (row["msg"].cget("text"), row["msg"].cget("text_color"))
        row["msg"].configure(text="Launching…", text_color=PRIMARY)
        self.root.update_idletasks()
        open_app(row["app"]["name"])

        def restore() -> None:
            row["_launching"] = False
            if row["msg"].cget("text") == "Launching…":
                row["msg"].configure(text=previous[0], text_color=previous[1])
        self.root.after(2500, restore)

    @staticmethod
    def _installed(row: dict) -> bool:
        """Installed = has a version in config.ini AND its folder exists next to Update Manager."""
        return bool(row["app"]["version"]) and (APPS_ROOT / row["app"]["name"]).is_dir()

    def _sync_header(self) -> None:
        """Window title shows how many updates are pending; the "Get updates" button says what
        is going on and is disabled while it would have nothing new to do."""
        rows = list(self.rows.values())
        pending = sum(1 for r in rows if self._installed(r) and r.get("_latest")
                      and has_update(r["app"]["version"], r["_latest"]) and not r.get("_busy"))
        title = f"{self.app_title} {__version__}"
        if pending:
            title += f" — {pending} update{'s' if pending != 1 else ''} available"
        if self.root.title() != title:
            self.root.title(title)
        if any(r.get("_busy") for r in rows):
            text, state = "Updating…", "disabled"
        elif any(r.get("_checking") for r in rows):
            text, state = "Checking…", "disabled"
        else:
            text, state = (f"Get updates ({pending})" if pending else "Get updates"), "normal"
        btn = getattr(self, "_get_btn", None)
        if btn is not None and (btn.cget("text"), btn.cget("state")) != (text, state):
            btn.configure(text=text, state=state)

    def _get_all_updates(self) -> None:
        """Update every app with a known update; if none is known, check again first and
        update whatever turns out to be outdated (so the button always does something)."""
        pending = [r for r in self.rows.values()
                   if r.get("_latest") and self._installed(r) and has_update(r["app"]["version"], r["_latest"])]
        if pending:
            for row in pending:
                self._update(row)
        else:
            self._refresh_states(auto_update=True)

    def _install(self, row: dict) -> None:
        self._apply_to_app(row, row.get("_latest"), installing=True)

    def _update(self, row: dict) -> None:
        self._apply_to_app(row, row.get("_latest"), installing=False)

    def _apply_to_app(self, row: dict, latest: str | None, installing: bool) -> None:
        """Download/install in the background; the window stays responsive meanwhile."""
        if row.get("_busy"):
            return
        row["_busy"] = True
        row["_cancel"] = False
        app = row["app"]
        name = app["name"]
        # While downloading, the row's button cancels (nothing is copied until the download is done).
        row["btn"].configure(text="Cancel", state="normal", border_color=PRIMARY,
                             command=lambda r=row: self._cancel(r))
        self._set_message(row, "Installing…" if installing else "Updating…", "accent")

        verb = "Installing" if installing else "Updating"
        last = [0.0]

        def progress(done: int, total: int) -> None:   # worker thread: hand text to the Tk thread
            if row.get("_cancel"):
                raise Cancelled()
            now = time.monotonic()
            if now - last[0] < 0.2 and done != total:
                return
            last[0] = now
            text = (f"{verb}… {done * 100 // total} %" if total
                    else f"{verb}… {done / 1048576:.1f} MB")
            self._bg_results.put((lambda _r, _e, t=text: row["_busy"] and not row.get("_cancel")
                                  and row["msg"].configure(text=t), None, None))

        def work() -> str:
            version = latest or get_latest_version(self.session, self.owner, name)
            if row.get("_cancel"):
                raise Cancelled()
            if not version:
                raise LookupError(f'No release found for "{name}".')
            apply_release(self.session, self.owner, name, version, APPS_ROOT / name, progress)
            return version

        self._run_bg(work, lambda version, error: self._apply_done(row, version, error, installing))

    def _cancel(self, row: dict) -> None:
        if row.get("_busy") and not row.get("_cancel"):
            row["_cancel"] = True
            row["btn"].configure(state="disabled", border_color=TERTIARY2)
            self._set_message(row, "Cancelling…", "muted")

    def _apply_done(self, row: dict, version: str | None, error, installing: bool = False) -> None:
        row["_busy"] = False
        app = row["app"]
        if isinstance(error, Cancelled):
            self._apply_state(row, row.get("_latest"))   # back to what it was; nothing was changed
            return
        if error is not None:
            if isinstance(error, LookupError):
                show_error("Not available", str(error))
            else:
                show_error("Update failed",
                           f'While installing "{app["name"]}", the following error occurred: {error}')
            self._refresh_one(row)
            return
        app["version"] = version
        write_config_version(self.config, app["section"], version)
        row["ver"].configure(text=version)
        self._set_action(row, "Open", lambda r=row: self._open(r),
                         "Installed" if installing else "Update completed")

    # -- search ------------------------------------------------------------------------
    def _focus_search(self) -> None:
        self._search.focus_set()
        self._search.select_range(0, "end")

    def _clear_search(self) -> None:
        self._search.clear()
        self._apply_filter()
        self.root.focus_set()

    def _apply_filter(self) -> None:
        query = self._search.get().strip().lower()
        changed = False
        for row in self.rows.values():
            hidden = bool(query) and query not in row["app"]["name"].lower()
            if row["hidden"] != hidden:
                row["hidden"], changed = hidden, True
        if changed:
            self._list.c.yview_moveto(0)
            self._layout_rows()

    # -- per-app menu (⋯ button / right-click) -------------------------------------------
    def _row_menu(self, row: dict, event=None) -> None:
        # A click on ⋯ while its menu is open first closes the menu (Tk unposts it on the press);
        # don't let the same click's release reopen it right away.
        last = getattr(self, "_menu_closed", (None, 0.0))
        if event is None and last[0] is row and time.monotonic() - last[1] < 0.4:
            return
        old = getattr(self, "_menu", None)
        if old is not None:
            old.destroy()
        app = row["app"]
        app_dir = APPS_ROOT / app["name"]
        installed = app_dir.is_dir()
        idle = not row.get("_busy") and not row.get("_checking")
        menu = make_menu(self.root, getattr(self, "_ui_scale", 1.0),
                         on_close=lambda: setattr(self, "_menu_closed", (row, time.monotonic())))
        menu.add_command("Open", lambda: self._open(row), installed and idle)
        menu.add_command("Open folder", lambda: open_folder(app_dir), app_dir.is_dir())
        if row.get("_latest") and installed and has_update(app["version"] or "", row["_latest"]):
            menu.add_command(f"What's new in {row['_latest']}", lambda: self._show_notes(row))
        menu.add_command("Check again", lambda: self._refresh_one(row), bool(self.owner) and idle)
        menu.add_separator()
        menu.add_command("Uninstall…", lambda: self._uninstall(row), installed and idle)
        if event is not None:
            x, y = event.x_root, event.y_root
        else:   # under the ⋯ button, right-aligned with it
            _x1, _y1, x2, y2 = row["more"].screen_box()
            x, y = x2 - menu.width, y2
        self._menu = menu
        menu.popup(x, y)

    # -- uninstall ---------------------------------------------------------------------
    def _uninstall(self, row: dict) -> None:
        if row.get("_busy") or row.get("_checking"):
            return
        app = row["app"]
        name = app["name"]
        app_dir = APPS_ROOT / name
        script = find_uninstaller(app_dir)
        lines = []
        if script is not None:
            lines.append(f"• Run its uninstaller: {script.relative_to(app_dir)}")
        if app_dir.exists():
            lines.append(f"• Delete the folder {app_dir}, including its settings and data")
        else:
            lines.append("• Its folder is already gone; only the record in config.ini is cleared")
        if message_dialog(f"Uninstall {name}?", "This will:\n" + "\n".join(lines),
                          ("Uninstall", "Cancel"), tone="warning") != "Uninstall":
            return   # Cancel is the default (Enter) for a destructive action
        row["_busy"] = True
        row["btn"].configure(state="disabled", border_color=TERTIARY2)
        self._set_message(row, "Uninstalling…", "accent")

        def work():
            if script is not None:
                code, output = run_uninstaller(script, app_dir)
                if code != 0:
                    return code, output
            remove_app_folder(app_dir)
            return None

        def done(failed, error) -> None:
            if error is None and failed is not None:
                code, output = failed
                if message_dialog(
                        "Uninstaller reported an error",
                        f"{script.name} exited with code {code}."
                        + (f"\n\n{output}" if output else "")
                        + f"\n\nDelete the folder {app_dir} anyway?",
                        ("Delete anyway", "Keep folder"), tone="warning") == "Delete anyway":
                    self._run_bg(lambda: remove_app_folder(app_dir),
                                 lambda _r, err: self._uninstall_done(row, err))
                else:
                    self._uninstall_done(row, None, removed=False)
                return
            self._uninstall_done(row, error)
        self._run_bg(work, done)

    def _uninstall_done(self, row: dict, error, removed: bool = True) -> None:
        row["_busy"] = False
        app = row["app"]
        if error is not None:
            show_error("Uninstall failed", f'Could not uninstall "{app["name"]}": {error}')
        elif removed:
            app["version"] = ""
            write_config_version(self.config, app["section"], "")
            row["ver"].configure(text="—")
            notes = row.get("_notes_win")
            if notes is not None and notes.winfo_exists():
                notes.destroy()
        self._apply_state(row, row.get("_latest"))
        if error is None and removed:
            self._set_message(row, "Uninstalled", "muted")

    # -- desktop notification when updates show up while you are elsewhere ----------------
    def _maybe_notify(self) -> None:
        rows = list(self.rows.values())
        if any(r.get("_checking") for r in rows):
            return   # wait for the whole round, then announce once
        new = [(r["app"]["name"], r["_latest"]) for r in rows
               if self._installed(r) and r.get("_latest") and not r.get("_busy")
               and has_update(r["app"]["version"], r["_latest"])
               and (r["app"]["name"], r["_latest"]) not in self._notified]
        if not new:
            return
        self._notified.update(new)
        if not self._notifications_on():
            return
        try:
            focused = self.root.focus_displayof() is not None and self.root.state() != "iconic"
        except Exception:  # noqa: BLE001
            focused = False
        if focused:
            return   # the window is in front: the list already shows it
        title = f"{len(new)} update{'s' if len(new) != 1 else ''} available"
        body = ", ".join(f"{name} {version}" for name, version in new)
        hwnd = None
        if IS_WINDOWS:
            try:
                hwnd = int(self.root.wm_frame(), 16)
            except Exception:  # noqa: BLE001
                pass
        self._run_bg(lambda: notify_desktop(title, body, hwnd))

    # -- settings ----------------------------------------------------------------------
    # Drawn on one canvas (CanvasForm: ~3 X windows instead of 45 customtkinter ones, see the
    # note above CanvasForm), built once and then only hidden/shown, refreshing its values.
    def _open_settings(self) -> None:
        win = self._settings_win
        if win is None or not win.winfo_exists():
            win = self._build_settings()
        s = self._settings
        s["user"].delete(0, "end")
        s["user"].insert(0, self.owner)
        labels = [text for text, _m in self.RECHECK_CHOICES]
        current = self._recheck_minutes()
        chosen = next((t for t, m in self.RECHECK_CHOICES if m == current), f"{current} minutes")
        s["interval"].set_values(labels if chosen in labels else [chosen, *labels])
        s["interval"].set(chosen)
        s["notify"].set(self._notifications_on())
        s["startup"].set(self.config.getboolean("UpdateManager", "check_on_startup", fallback=True))
        saved = self.config.get("UpdateManager", "appearance", fallback="system").strip().lower()
        s["theme"].set({"light": "Light", "dark": "Dark"}.get(saved, "System"))
        if win.state() == "withdrawn":
            win.update_idletasks()        # final size first
            self._center_on_main(win)     # centre over the main window (same monitor and scale)
            win.deiconify()
        win.lift()
        win.focus_force()
        win.after(100, s["user"].focus_set)

    def _center_on_main(self, win) -> None:
        """Place a child window centred over the main window, so it opens on the same monitor (and
        thus the same DPI/scale) and never on another screen. On Windows it is clamped to that
        monitor's work area so no part lands off-screen."""
        try:
            win.update_idletasks()
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
            x, y = px + (pw - ww) // 2, py + (ph - wh) // 2
            if IS_WINDOWS:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32

                class _MI(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
                hmon = u.MonitorFromWindow(u.GetAncestor(self.root.winfo_id(), 2), 2)  # GA_ROOT, NEAREST
                mi = _MI(); mi.cbSize = ctypes.sizeof(_MI); u.GetMonitorInfoW(hmon, ctypes.byref(mi))
                w = mi.rcWork
                x = max(w.left, min(x, w.right - ww))
                y = max(w.top, min(y, w.bottom - wh))
            win.geometry(f"+{int(x)}+{int(y)}")
        except Exception:  # noqa: BLE001 — fall back to the window manager's placement
            pass

    def _build_settings(self) -> CTkToplevel:
        win = CTkToplevel(self.root)
        win.withdraw()
        _set_toplevel_icon(win)
        self._settings_win = win
        win.title("Settings")
        win.transient(self.root)
        win.resizable(True, True)   # allow manual resize; a minimum size (below) prevents clipping
        f = CanvasForm(win)
        px = f.s
        pad = px(24)
        names = ("GitHub username", "Check for updates", "Notify me", "At startup", "Theme")
        col = pad + max(f.measure(n, bold=True) for n in names) + px(24)   # controls column

        def label(y: int, h: int, text: str) -> None:
            f.text(pad, y + h / 2, text, bold=True, anchor="w")

        y, h = px(22), px(30)
        label(y, h, names[0])
        user = f.entry(col, y, px(260))
        y += h + px(5)
        hint = f.text(col, y, "Owner of the app repositories. The token is not stored here\n"
                      "(environment variable or secrets.ini, see README).", color=QUATERNARY)
        y = f.bbox(hint)[3] + px(14)
        label(y, h, names[1])
        interval = f.option(col, y, [t for t, _m in self.RECHECK_CHOICES])
        y += h + px(14)
        label(y, px(18), names[2])
        notify = f.switch(col, y, "when updates are found and the window is in the background")
        y += px(18) + px(18)
        label(y, px(18), names[3])
        startup = f.switch(col, y, f"check for a new version of {self.app_title}")
        y += px(18) + px(18)
        label(y, h, names[4])
        theme = f.option(col, y, ["System", "Light", "Dark"])
        y += h + px(24)
        width = max(f.bbox(t)[2] for t in (hint, notify.tag, startup.tag)) + pad

        def close(_e=None) -> None:
            win.withdraw()
            self.root.focus_set()

        def save(_e=None) -> None:
            minutes = dict(self.RECHECK_CHOICES).get(interval.get(), self._recheck_minutes())
            mode = theme.get().lower()
            new_owner = user.get().strip()
            self._save_setting("recheck_minutes", str(minutes))
            self._save_setting("notifications", "true" if notify.get() else "false")
            self._save_setting("check_on_startup", "true" if startup.get() else "false")
            self._save_setting("appearance", mode)
            close()
            set_appearance_mode(mode)
            self._schedule_recheck()
            if new_owner != self.owner:
                self._save_setting("username", new_owner, section="GitHub")
                self.owner = new_owner
                self._notified.clear()
                if not new_owner:
                    self._show_no_owner()
                    return
                for row in self.rows.values():
                    if row.get("_icon_ctk") is None and not row.get("_spinning"):
                        self._init_app_icon(row)
                self._refresh_states()

        bw = f.button_width("Cancel")
        f.button(width - pad - bw, y, "Save", save, primary=True, w=bw)
        f.button(width - pad - 2 * bw - px(10), y, "Cancel", close, w=bw)
        f.size(width, y + h + px(20))
        f.c.pack(fill="both", expand=True)
        # Minimum size = the content size (physical px), so the window can be enlarged but never
        # shrunk small enough to clip its controls. wm_minsize bypasses customtkinter's scaling.
        win.update_idletasks()
        win.wm_minsize(win.winfo_reqwidth(), win.winfo_reqheight())
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", close)
        win.bind("<Return>", save)
        self._settings = {"user": user, "interval": interval, "notify": notify,
                          "startup": startup, "theme": theme}
        return win

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if signal_running_instance():
        return   # the running window was brought to the front
    UpdateManagerApp().run()


if __name__ == "__main__":
    main()
