"""Small process-level lock shared by the R4 batch command-line runners."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


def acquire_execution_lock(output_dir: Path) -> IO[str]:
    """Lock one output directory for a single model-executing process."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".r4_model_execution.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError("batch_execution_already_running:" + str(output_dir)) from error
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def release_execution_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
