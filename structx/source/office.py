"""Runs one of the fixed PDF-export PowerShell scripts next to this file
(`export_pdf.ps1` for PowerPoint, `export_pdf_word.ps1` for Word) with
`-File` and every value as a separate parameter: no command string is
assembled from input and no execution-policy override is used. Windows +
the Office application only; anything else raises `OfficeError`.

PowerPoint and Word are single-instance COM servers and the scripts quit
the application when no document is left open, so two exports of the same
application at once can lose their instance. One lock per application is
held around the subprocess call only: exports of the same application
never overlap (API requests run in threads), gateway calls stay concurrent.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

_TIMEOUT = 600
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(app: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(app, threading.Lock())


class OfficeError(RuntimeError):
    """The local Office application could not save the file as PDF."""


def run_export(script: Path, input_path: Path, output_path: Path, *, app: str) -> Path:
    """Runs `script` on `input_path` (a throwaway copy in the caller's temp
    dir) and returns `output_path` once it exists; `app` names the
    application for the error text."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise OfficeError(f"PowerShell not found; PDF export needs Windows + {app}.")
    args = [
        powershell, "-NoProfile", "-NonInteractive", "-File", str(script),
        "-InputPath", str(input_path.resolve()), "-OutputPath", str(output_path.resolve()),
    ]
    try:
        with _lock_for(app):
            completed = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfficeError(f"{app} did not complete: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise OfficeError(
            f"{app} PDF export failed (is Microsoft {app} installed, and are local "
            "PowerShell scripts allowed to run?): " + " ".join(detail[-3:])
        )
    if not output_path.is_file():
        raise OfficeError(f"{app} reported success but wrote no PDF for {input_path.name}.")
    return output_path
