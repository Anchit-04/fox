import json
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import paths
import transport
from browser.cdp import CDPConnection, CDPError

CANDIDATES = {
    "chrome": [Path("Google/Chrome/Application/chrome.exe")],
    "brave": [Path("BraveSoftware/Brave-Browser/Application/brave.exe")],
}
ROOT_VARS = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
PORT_FILE = "DevToolsActivePort"
WINDOW_SIZE = (1280, 800)


class BrowserNotFound(Exception):
    pass


class BrowserLaunchFailed(Exception):
    pass


@dataclass
class Browser:
    process: subprocess.Popen | None
    port: int
    ws_url: str
    profile: Path


def _install_roots() -> list[Path]:
    return [Path(os.environ[var]) for var in ROOT_VARS if os.environ.get(var)]


def find_browser(preferred: str | None = None) -> Path:
    preferred = preferred or os.environ.get("FOX_BROWSER")
    order = list(CANDIDATES)
    if preferred:
        if Path(preferred).is_file():
            return Path(preferred)
        name = preferred.strip().lower()
        if name not in CANDIDATES:
            raise BrowserNotFound(
                f"FOX_BROWSER is {preferred!r}, which is neither a known browser "
                f"({', '.join(CANDIDATES)}) nor a file that exists."
            )
        order.remove(name)
        order.insert(0, name)
    roots = _install_roots()
    for name in order:
        for relative in CANDIDATES[name]:
            for root in roots:
                candidate = root / relative
                if candidate.is_file():
                    return candidate
    raise BrowserNotFound(
        f"No {' or '.join(CANDIDATES)} found under {', '.join(str(r) for r in roots) or 'any install folder'}. "
        "Install one, or set FOX_BROWSER to the path of a Chromium-based browser."
    )


def profile_dir(name: str = "default") -> Path:
    path = paths.FOX_HOME / "browser" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_port_file(profile: Path) -> tuple[int, str] | None:
    try:
        lines = (profile / PORT_FILE).read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not path.startswith("/devtools/browser/"):
        return None
    return port, path


_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _version_info(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with _no_proxy.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def attach(profile: Path) -> Browser | None:
    found = read_port_file(profile)
    if found is None:
        return None
    port, path = found
    info = _version_info(port)
    if not info or not info.get("webSocketDebuggerUrl", "").endswith(path):
        return None
    return Browser(process=None, port=port, ws_url=info["webSocketDebuggerUrl"], profile=profile)


def _launch_args(executable: Path, profile: Path, headless: bool) -> list[str]:
    width, height = WINDOW_SIZE
    args = [
        str(executable),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--hide-crash-restore-bubble",
        f"--window-size={width},{height}",
    ]
    if headless:
        args += ["--headless=new", "about:blank"]
    else:
        args.append("--no-startup-window")
    return args


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def headless_by_default() -> bool:
    return os.environ.get("FOX_BROWSER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def launch(executable: Path, profile: Path, headless: bool | None = None, timeout: float = 10.0) -> Browser:
    if headless is None:
        headless = headless_by_default()
    (profile / PORT_FILE).unlink(missing_ok=True)
    process = subprocess.Popen(
        _launch_args(executable, profile, headless),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_detached(),
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        browser = attach(profile)
        if browser is not None:
            browser.process = process
            return browser
        code = process.poll()
        if code is not None:
            raise BrowserLaunchFailed(
                f"{executable.name} exited with code {code} before opening its debugging port. "
                f"Another browser may already be using the profile at {profile}."
            )
        time.sleep(0.1)
    stop(Browser(process=process, port=0, ws_url="", profile=profile))
    raise BrowserLaunchFailed(f"{executable.name} did not open its debugging port within {timeout:g}s.")


@contextmanager
def _launch_lock(profile: Path, timeout: float):
    lock_path = profile.parent / f"{profile.name}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise BrowserLaunchFailed(f"Another Fox process held {lock_path} for over {timeout:g}s.")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def attach_or_launch(
    executable: Path | None = None,
    profile: Path | None = None,
    headless: bool | None = None,
    timeout: float = 10.0,
) -> Browser:
    profile = profile or profile_dir()
    browser = attach(profile)
    if browser is not None:
        return browser
    with _launch_lock(profile, timeout + 5):
        browser = attach(profile)
        if browser is not None:
            return browser
        return launch(executable or find_browser(), profile, headless, timeout)


def close(browser: Browser, timeout: float = 5.0) -> None:
    try:
        with CDPConnection(browser.ws_url, open_timeout=timeout) as conn:
            conn.send("Browser.close", timeout=timeout)
    except CDPError:
        pass
    if browser.process is not None:
        try:
            browser.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(browser)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _version_info(browser.port, timeout=0.5) is not None:
        time.sleep(0.1)


def close_shared_browser(profile: Path | None = None) -> bool:
    browser = attach(profile or profile_dir())
    if browser is None:
        return False
    close(browser)
    return True


def stop(browser: Browser, timeout: float = 5.0) -> None:
    process = browser.process
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **transport.hidden(),
        )
    else:
        process.kill()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
