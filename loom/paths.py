
import os
from pathlib import Path

FOX_HOME = Path(__file__).resolve().parent.parent


class WorkspaceError(Exception):
    """The requested workspace can't be used. Raised at startup, never mid-run."""


def _initial_workspace() -> Path:
    """FOX_WORKSPACE if set, else the launch directory. Unvalidated — the cwd
    always exists, and an env var pointing somewhere bad should surface when
    it's used, not crash at import."""
    return Path(os.environ.get("FOX_WORKSPACE") or Path.cwd()).resolve()


_workspace: Path = _initial_workspace()


def workspace() -> Path:
    """The directory the agent is confined to. Call this; don't cache it."""
    return _workspace


def set_workspace(path: str | Path, allow_unsafe: bool = False) -> Path:
    """Validate and install the workspace. Call once at startup, before any
    agent runs. Fails loudly here so a bad path can't surface later as a
    mid-session tool error."""
    global _workspace
    resolved = Path(path).expanduser().resolve()

    if not resolved.exists():
        raise WorkspaceError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {resolved}")
    if not allow_unsafe:
        _reject_dangerous(resolved)

    _workspace = resolved
    return resolved


def _reject_dangerous(path: Path) -> None:
    #Refuse roots whose blast radius is the whole machine
    if path == Path(path.anchor):
        raise WorkspaceError(
            f"Refusing to use a drive/filesystem root as the workspace ({path}). "
            "Point Fox at a specific project directory, or pass "
            "--allow-unsafe-workspace if you really mean it."
        )
    if path == Path.home().resolve():
        raise WorkspaceError(
            f"Refusing to use your home directory as the workspace ({path}) — "
            "it contains .ssh, .aws and every other project. Point Fox at a "
            "specific project, or pass --allow-unsafe-workspace."
        )


def db_path() -> Path:
    return FOX_HOME / "fox.db"
