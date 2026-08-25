#!/usr/bin/env python3
"""Create a local virtual environment for the training scripts.

The repository does not store .venv. Run this file after cloning the
project on a new machine.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
LOCK_FILE = ROOT / "requirements-lock.txt"
REQ_FILE = ROOT / "requirements.txt"


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.check_call(command, cwd=ROOT)


def python_in_venv() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def activation_hint() -> str:
    if platform.system() == "Windows":
        return r".venv\Scripts\activate"
    return "source .venv/bin/activate"


def main() -> int:
    use_lock = "--locked" in sys.argv[1:]
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 或更高版本。")
        return 1

    if not VENV_DIR.exists():
        print(f"创建虚拟环境: {VENV_DIR}")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    else:
        print(f"虚拟环境已存在: {VENV_DIR}")

    py = python_in_venv()
    if not py.exists():
        print(f"没有找到虚拟环境 Python: {py}")
        return 1

    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    req = LOCK_FILE if use_lock and LOCK_FILE.exists() else REQ_FILE
    run([str(py), "-m", "pip", "install", "-r", str(req)])

    print()
    print("环境准备好了。下一步：")
    print(f"  {activation_hint()}")
    print("  python ai/train_all.py")
    print("  python ai/export_rollouts.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
