"""Start-up checks for single-process hosts. Standard library only.

Runs before any third-party import so it can explain a wrong Python image
or install the requirements itself when the egg's startup command did not.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
SENTINEL_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "alembic", "pydantic_settings", "PIL", "numpy", "imagehash", "httpx", "anthropic", "discord")


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            f"\nRank Recovery needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this container runs "
            f"{sys.version.split()[0]}.\nOn a Pterodactyl/Bloom panel: Startup page -> Docker Image -> choose the Python 3.12 image, then Start.\n\n"
        )
        sys.exit(2)


def local_site_packages(root: Path) -> Path:
    """The ``pip install --prefix .local`` location for the *running* interpreter only."""
    return root / ".local" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def add_paths(root: Path) -> None:
    for path in (str(root), str(local_site_packages(root))):
        if path not in sys.path:
            sys.path.insert(0, path)
    bin_dir = root / ".local" / "bin"
    if bin_dir.exists():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def missing_modules() -> list[str]:
    missing = []
    for name in SENTINEL_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    return missing


def install_requirements(root: Path, requirements: str = "requirements.txt") -> bool:
    req = root / requirements
    if not req.exists():
        sys.stderr.write(f"{req} not found; cannot install dependencies\n")
        return False
    prefix = root / ".local"
    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--prefix", str(prefix), "-r", str(req)]
    sys.stderr.write("Installing requirements (first start, this takes a few minutes)...\n")
    try:
        subprocess.check_call(cmd, cwd=str(root))
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"pip failed with exit code {exc.returncode}\n")
        return False
    importlib.invalidate_caches()
    return True


def bootstrap(root: Path) -> None:
    check_python()
    os.chdir(root)
    add_paths(root)
    missing = missing_modules()
    if missing:
        sys.stderr.write(f"Missing packages for this Python: {', '.join(missing)}\n")
        if not install_requirements(root):
            sys.exit(3)
        add_paths(root)
        still = missing_modules()
        if still:
            sys.stderr.write(f"Still missing after install: {', '.join(still)}\n")
            sys.exit(3)
