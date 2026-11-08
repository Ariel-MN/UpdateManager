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
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from shutil import copy2
from urllib.parse import quote

import requests
from customtkinter import (
    CTk, CTkButton, CTkFrame, CTkImage, CTkLabel, CTkScrollableFrame, ThemeManager,
    get_appearance_mode, set_appearance_mode, set_widget_scaling, set_window_scaling,
)
from CTkMessagebox import CTkMessagebox
from PIL import Image, ImageDraw

# ── Identity ──────────────────────────────────────────────────────────────────────────
# Current version of Update Manager itself. Self-update compares this against the latest
# release tag of SELF_REPO; a new release must bump this value in the shipped file.
__version__ = "v1.0.1"
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
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    session.headers.update({"Accept": "application/vnd.github+json"})
    return session


def get_latest_release(session: requests.Session, owner: str, repo: str) -> dict | None:
    """Full 'latest release' object from the GitHub API. Works for PRIVATE repos when the
    session carries a token with access. None if there is no release / no access / on error."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("tag_name") else None


def get_latest_version(session: requests.Session, owner: str, repo: str) -> str | None:
    """Latest release tag of owner/repo, or None if there are no releases / no access."""
    release = get_latest_release(session, owner, repo)
    return release.get("tag_name") if release else None


def _download(session: requests.Session, url: str, dest: Path, headers: dict | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=HTTP_TIMEOUT, headers=headers) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)


def _asset_by_name(release: dict, name: str) -> dict | None:
    for asset in release.get("assets", []) or []:
        if asset.get("name") == name:
            return asset
    return None


def _download_asset(session: requests.Session, asset: dict, dest: Path) -> None:
    """Download a release asset through the API endpoint (PRIVATE-repo safe).

    The asset API redirects to signed storage; requests drops the Authorization header on the
    cross-host redirect, and the signed URL carries its own credentials — so this authenticates
    for private repos where the plain /releases/download/ URL would 404."""
    _download(session, asset["url"], dest, headers={"Accept": "application/octet-stream"})


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
                  latest_ver: str, target_dir: Path) -> None:
    """Download the latest release of owner/repo into ``target_dir``. Works for PRIVATE repos
    (all transfers go through the authenticated GitHub API).

    Strategy: if the release ships an ``update.json`` asset, download exactly the files it lists;
    otherwise fall back to the tagged source archive (zipball API). Raises on failure."""
    target_dir.mkdir(parents=True, exist_ok=True)
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
                for entry in manifest:
                    asset = _asset_by_name(release, entry["filename"])
                    if asset is None:
                        raise RuntimeError(f"release asset not found: {entry['filename']}")
                    staged = tmp_dir / entry["filename"]
                    _download_asset(session, asset, staged)
                    dest = target_dir / entry["output"]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copy2(staged, dest)
                return
        # Fallback: tagged source archive via the API (zipball authenticates for private repos).
        tag = release.get("tag_name", latest_ver)
        archive = tmp_dir / "source.zip"
        _download(session, f"https://api.github.com/repos/{owner}/{repo}/zipball/{tag}", archive)
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


def restart_self() -> None:
    """Relaunch this program (used after a self-update)."""
    os.execl(sys.executable, sys.executable, str(Path(__file__).resolve()))


def show_error(title: str, message: str) -> None:
    CTkMessagebox(title=title, message=message, icon="cancel")


def ask_yes_no(title: str, message: str, yes="Update", no="Later") -> bool:
    box = CTkMessagebox(title=title, message=message, icon="question",
                        option_1=no, option_2=yes)
    return box.get() == yes


# ── GUI ───────────────────────────────────────────────────────────────────────────────
class UpdateManagerApp:
    def __init__(self) -> None:
        self.config = read_config()
        migrate_token_out_of_config(self.config)
        self.owner = self.config.get("GitHub", "username", fallback="").strip()
        self.token = load_token()
        self.session = github_session(self.token)
        self.apps = parse_apps(self.config)
        self.rows: dict[str, dict] = {}
        self._spin_running = False
        # Colour used to make the list scrollbar's handle invisible without removing it (so its
        # column width stays reserved and nothing shifts). It matches the window background.
        try:
            self._sb_hidden = ThemeManager.theme["CTk"]["fg_color"]
        except Exception:  # noqa: BLE001
            self._sb_hidden = ("gray92", "gray14")
        self.app_title = self.config.get("UpdateManager", "title", fallback=APP_TITLE).strip() or APP_TITLE
        self.heading = self.config.get("UpdateManager", "heading", fallback=APP_HEADING).strip() or APP_HEADING

        # UI scaling. "auto"/blank leaves customtkinter's automatic DPI scaling (which can read 1.0
        # on some Linux/HiDPI sessions, making everything tiny on a 4K screen). Set a number like
        # 1.5 or 2.0 to enlarge widgets + window. Applied before the window is created.
        self._apply_ui_scale(self.config.get("UpdateManager", "ui_scale", fallback="").strip())

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
        self.root.title(f"{self.app_title} {__version__}")
        self.root.minsize(820, 420)
        self._set_window_icon()

        self._build_header()
        self._build_list()

        # After the window is shown: self-update prompt, then refresh managed-app states.
        self.root.after(250, self._startup_checks)

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

    @staticmethod
    def _apply_ui_scale(raw: str) -> None:
        """Apply a manual UI scale from config ("auto"/blank → leave automatic DPI scaling)."""
        value = raw.lower()
        if not value or value == "auto":
            return
        try:
            scale = float(value.replace(",", "."))
        except ValueError:
            return
        if scale > 0:
            set_widget_scaling(scale)
            set_window_scaling(scale)

    # -- construction ------------------------------------------------------------------
    def _img(self, name: str, size: tuple[int, int]) -> CTkImage | None:
        light = IMG_DIR / f"{name}_L.png"
        dark = IMG_DIR / f"{name}_D.png"
        if light.is_file() or dark.is_file():
            l_img = Image.open(light if light.is_file() else dark)
            d_img = Image.open(dark if dark.is_file() else light)
            return CTkImage(light_image=l_img, dark_image=d_img, size=size)
        return None

    def _build_header(self) -> None:
        header = CTkFrame(master=self.root, fg_color=SECONDARY, height=40)
        header.pack(expand=False, fill="both", pady=5, padx=5)

        logo = IMG_DIR / "Logo.png"
        if logo.is_file():
            logo_img = CTkImage(size=(70, 70), light_image=Image.open(logo))
            CTkLabel(master=header, text="", image=logo_img).pack(pady=10, padx=(20, 0), side="left")

        theme_img = self._img("Theme", (25, 25))
        CTkButton(master=header, text="" if theme_img else "Theme", image=theme_img,
                  fg_color="transparent", width=25, height=25, hover_color=TERTIARY2,
                  command=self._toggle_theme).pack(pady=5, padx=5, anchor="ne")
        CTkLabel(master=header, text=self.heading, font=FONT_TITLE).pack(pady=20, padx=20, anchor="nw")
        CTkLabel(master=header, text='Download and install app updates by clicking "Get updates"',
                 font=FONT_SUBTITLE).pack(pady=(0, 20), padx=20, side="left")
        CTkButton(master=header, text="Get updates", font=FONT_BODY_BOLD, text_color=SECONDARY,
                  fg_color=PRIMARY, hover_color=PRIMARY2,
                  command=self._get_all_updates).pack(pady=(0, 20), padx=20, side="right")

    def _build_list(self) -> None:
        # Scrollbar starts "hidden" (handle painted the window-bg colour), but it stays in place so
        # its column width is always reserved — showing it later never shifts the content.
        center = CTkScrollableFrame(master=self.root, fg_color="transparent", border_color=SECONDARY,
                                    border_width=1, orientation="vertical",
                                    scrollbar_button_color=self._sb_hidden, scrollbar_button_hover_color=self._sb_hidden)
        center.pack(expand=True, fill="both", pady=(0, 5), padx=5)
        self._list_frame = center
        try:  # recompute scrollbar visibility whenever the viewport resizes
            center._parent_canvas.bind("<Configure>", lambda _e: self._update_scrollbar(), add="+")
        except Exception:  # noqa: BLE001
            pass

        for app in self.apps:
            name = app["name"]
            frame = CTkFrame(master=center, fg_color=SECONDARY)
            frame.pack(expand=False, fill="x", pady=2, padx=2)

            icon_label = CTkLabel(master=frame, text="", image=None, fg_color=TERTIARY2,
                                  corner_radius=8, width=50, height=50, font=FONT_SUBTITLE)
            icon_label.pack(pady=10, padx=15, side="left")

            btn = CTkButton(master=frame, text="…", corner_radius=32, text_color=PRIMARY,
                            border_color=PRIMARY, fg_color="transparent", border_width=1,
                            hover_color=TERTIARY2, font=FONT_BODY_BOLD, state="disabled")
            btn.pack(padx=(0, 15), side="right")
            msg = CTkLabel(master=frame, text="Checking…", font=FONT_BODY_BOLD)
            msg.pack(pady=(0, 5), padx=(0, 50), side="right")

            CTkLabel(master=frame, text=name, font=FONT_SUBTITLE).pack(pady=(5, 0), padx=(0, 15), anchor="nw")
            ver = CTkLabel(master=frame, text=app["version"] or "—", font=FONT_BODY)
            ver.pack(pady=(0, 5), padx=(0, 15), anchor="nw")

            row = {"app": app, "btn": btn, "msg": msg, "ver": ver, "_icon_label": icon_label}
            self.rows[app["section"]] = row
            self._init_app_icon(row)

        self.root.after(150, self._update_scrollbar)

    def _update_scrollbar(self) -> None:
        """Make the list scrollbar's handle visible only when the content overflows. The
        scrollbar keeps its reserved column width either way, so nothing shifts around it."""
        frame = getattr(self, "_list_frame", None)
        if frame is None:
            return
        try:
            canvas = frame._parent_canvas
            bbox = canvas.bbox("all")
            if bbox is None:
                return
            overflow = (bbox[3] - bbox[1]) > canvas.winfo_height() + 1
            if overflow:   # original (subtle) scrollbar style
                frame._scrollbar.configure(button_color=SECONDARY, button_hover_color=QUATERNARY)
            else:
                frame._scrollbar.configure(button_color=self._sb_hidden, button_hover_color=self._sb_hidden)
        except Exception:  # noqa: BLE001
            pass

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

    def _assemble_icon(self, app: dict) -> CTkImage | None:
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
            return CTkImage(light_image=Image.open(lp), dark_image=Image.open(dp), size=(40, 40))
        except Exception:  # noqa: BLE001
            return None

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
        self.root.after(0, self._finish_icon, row)

    def _finish_icon(self, row: dict) -> None:
        row["_spinning"] = False
        self._apply_icon(row, self._assemble_icon(row["app"]))

    def _apply_icon(self, row: dict, image: CTkImage | None) -> None:
        label = row["_icon_label"]
        try:
            if image is not None:
                row["_icon_ctk"] = image          # keep a reference alive
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
        as a set of CTkImage frames for the current theme."""
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
            frames.append(CTkImage(light_image=im, dark_image=im, size=(26, 26)))
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
        set_appearance_mode("light" if get_appearance_mode() == "Dark" else "dark")

    def _startup_checks(self) -> None:
        if self.config.getboolean("UpdateManager", "check_on_startup", fallback=True):
            self._check_self_update()
        self._refresh_states()

    def _check_self_update(self) -> None:
        if not self.owner:
            return
        latest = get_latest_version(self.session, self.owner, SELF_REPO)
        if latest and has_update(__version__, latest):
            if ask_yes_no(f"Update available {latest}",
                          f"A new version of {self.app_title} is available ({latest}).\n"
                          f"You are on {__version__}. Update now?"):
                self._self_update(latest)

    def _self_update(self, latest: str) -> None:
        try:
            apply_release(self.session, self.owner, SELF_REPO, latest, APP_DIR)
        except Exception as exc:  # noqa: BLE001
            show_error("Update failed", f"Could not update {self.app_title}: {exc}")
            return
        CTkMessagebox(title="Update completed",
                      message=f"{self.app_title} was updated to {latest}. It will now restart.",
                      icon="check")
        self.root.destroy()
        restart_self()

    def _refresh_states(self) -> None:
        for row in self.rows.values():
            self._refresh_one(row)

    def _refresh_one(self, row: dict) -> None:
        app = row["app"]
        latest = get_latest_version(self.session, self.owner, app["name"])
        row["_latest"] = latest
        if not app["version"]:
            self._set_action(row, "Install", lambda r=row: self._install(r), "No version installed")
        elif latest and has_update(app["version"], latest):
            self._set_action(row, "Update", lambda r=row: self._update(r), "Update available")
        elif latest is None:
            self._set_action(row, "Open", lambda r=row: open_app(r["app"]["name"]), "No releases")
        else:
            self._set_action(row, "Open", lambda r=row: open_app(r["app"]["name"]), "Up to date")

    def _set_action(self, row: dict, text: str, command, message: str) -> None:
        row["btn"].configure(text=text, state="normal", border_color=PRIMARY, command=command)
        row["msg"].configure(text=message)

    def _get_all_updates(self) -> None:
        for row in self.rows.values():
            app = row["app"]
            latest = row.get("_latest")
            if latest and has_update(app["version"], latest):
                self._update(row)

    def _install(self, row: dict) -> None:
        self._apply_to_app(row, row.get("_latest"), installing=True)

    def _update(self, row: dict) -> None:
        self._apply_to_app(row, row.get("_latest"), installing=False)

    def _apply_to_app(self, row: dict, latest: str | None, installing: bool) -> None:
        app = row["app"]
        if not latest:
            latest = get_latest_version(self.session, self.owner, app["name"])
        if not latest:
            show_error("Not available", f'No release found for "{app["name"]}".')
            return
        row["btn"].configure(state="disabled", border_color=TERTIARY2)
        row["msg"].configure(text="Installing…" if installing else "Updating…")
        self.root.update_idletasks()
        try:
            apply_release(self.session, self.owner, app["name"], latest,
                          APPS_ROOT / app["name"])
        except Exception as exc:  # noqa: BLE001
            show_error("Update failed",
                       f'While installing "{app["name"]}", the following error occurred: {exc}')
            self._refresh_one(row)
            return
        app["version"] = latest
        write_config_version(self.config, app["section"], latest)
        row["ver"].configure(text=latest)
        self._set_action(row, "Open", lambda r=row: open_app(r["app"]["name"]), "Update completed")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    UpdateManagerApp().run()


if __name__ == "__main__":
    main()
