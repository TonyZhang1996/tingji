"""Separate immutable application resources from each user's private data.

Source checkouts keep their existing layout. Installed builds never consult the
developer's Python/model paths and never store private results beside the EXE.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def install_root() -> Path:
    return Path(sys.executable).resolve().parent if is_frozen() else source_root()


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", install_root())).resolve()
    return source_root()


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def user_data_root() -> Path:
    if not is_frozen():
        return source_root()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "Tingji").resolve()


def speaker_root() -> Path:
    return resource_path("models", "3d-speaker")


def speaker_executable() -> Path:
    # A console-subsystem helper supports the JSON progress pipe. The parent
    # starts it with CREATE_NO_WINDOW so no command window is shown to users.
    return install_root() / "TingjiSpeaker.exe"


def speaker_child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", OMP_NUM_THREADS="4", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if is_frozen():
        # This is a new top-level frozen process, not a multiprocessing child.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for key in ("PYTHONHOME", "PYTHONPATH", "MEETING_SPEAKER_PYTHON", "MEETING_SPEAKER_ROOT"):
            env.pop(key, None)
        for key in list(env):
            if key.upper().endswith("_API_KEY"):
                env.pop(key, None)
    return env
