import os
import shutil
from pathlib import Path


def _npm_global() -> Path | None:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "npm" if appdata else None


def find_cli(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    npm = _npm_global()
    if npm is not None:
        for suffix in (".cmd", ".exe"):
            candidate = npm / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None
