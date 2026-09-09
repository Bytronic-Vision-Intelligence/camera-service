"""
Create the project virtualenv and install dependencies.

Run from the project root:
    python tools/setup.py
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
VENV_DIR = ROOT / ".venv"
# requirements-dev.txt pulls in requirements.txt via `-r`, so this installs
# both: a developer needs the test tooling, which the shipped binary does not.
REQUIREMENTS = ROOT / "requirements-dev.txt"


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main() -> int:
    if not VENV_DIR.is_dir():
        print(f"Creating virtualenv at {VENV_DIR}")
        try:
            venv.create(VENV_DIR, with_pip=True)
        except Exception as exc:
            print(f"ERROR: failed to create virtualenv: {exc}", file=sys.stderr)
            if sys.platform != "win32":
                print(
                    "On Debian/Ubuntu, install the venv package first:\n"
                    "  sudo apt install python3-venv",
                    file=sys.stderr,
                )
            return 1
    else:
        print(f"Using existing virtualenv at {VENV_DIR}")

    py = venv_python(VENV_DIR)
    if not py.is_file():
        print(f"ERROR: missing interpreter {py}", file=sys.stderr)
        return 1

    subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip"])

    if REQUIREMENTS.is_file():
        deps = requirements_lines(REQUIREMENTS)
        if deps:
            print(f"Installing dependencies from {REQUIREMENTS.name}")
            subprocess.check_call([str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        else:
            print(f"No packages listed in {REQUIREMENTS.name}")
    else:
        print(f"No {REQUIREMENTS.name} found; skipping dependency install")

    if sys.platform == "win32":
        print(f"Done. Activate with: {VENV_DIR}\\Scripts\\Activate.ps1")
    else:
        print(f"Done. Activate with: source {VENV_DIR}/bin/activate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
