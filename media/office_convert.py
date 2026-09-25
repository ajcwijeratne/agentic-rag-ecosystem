"""Convert rendered Office files to PDF with the Office apps on this machine.

Word (and PowerPoint for decks) are driven through PowerShell COM, so no extra
Python package is needed. Microsoft does not support Office automation with no
person present, so every conversion:

- runs one at a time (a module lock),
- opens its own Office instance with alerts off,
- has a hard timeout, after which only the Office process this call started is
  killed, never one the user has open.

On the same pass the file is re-saved with its TrueType fonts embedded, so a
client without Open Sans still sees Open Sans.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

_WORD_PS = r"""
param([string]$In, [string]$Out, [string]$PidFile, [int]$Embed = 1)
$ErrorActionPreference = 'Stop'
$before = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = New-Object -ComObject Word.Application
$after = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$new = @($after | Where-Object { $before -notcontains $_ })
if ($new.Count -gt 0) { Set-Content -LiteralPath $PidFile -Value ($new -join ',') }
try {
  $app.Visible = $false
  $app.DisplayAlerts = 0
  $doc = $app.Documents.Open($In, $false, $false, $false)
  if ($Embed -eq 1) {
    $doc.EmbedTrueTypeFonts = $true
    $doc.SaveSubsetFonts = $false
    $doc.Save()
  }
  $doc.ExportAsFixedFormat($Out, 17)
  $pages = $doc.ComputeStatistics(2)
  $doc.Close(0)
  Write-Output "OK pages=$pages"
} finally {
  try { $app.Quit() } catch {}
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""

_POWERPOINT_PS = r"""
param([string]$In, [string]$Out, [string]$PidFile, [int]$Embed = 1)
$ErrorActionPreference = 'Stop'
$before = @(Get-Process POWERPNT -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = New-Object -ComObject PowerPoint.Application
$after = @(Get-Process POWERPNT -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$new = @($after | Where-Object { $before -notcontains $_ })
if ($new.Count -gt 0) { Set-Content -LiteralPath $PidFile -Value ($new -join ',') }
try {
  $app.DisplayAlerts = 1
  $pres = $app.Presentations.Open($In, 0, 0, 0)
  if ($Embed -eq 1) { $pres.SaveAs($In, 24, -1) }
  $pres.SaveAs($Out, 32)
  $slides = $pres.Slides.Count
  $pres.Close()
  Write-Output "OK pages=$slides"
} finally {
  if ($new.Count -gt 0) { try { $app.Quit() } catch {} }
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
"""


def enabled() -> bool:
    """PDF export is on by default on Windows. DELIVERABLES_PDF=off disables it."""
    mode = os.getenv("DELIVERABLES_PDF", "word").strip().lower()
    return sys.platform == "win32" and mode not in ("off", "0", "false", "none", "")


def _timeout() -> int:
    try:
        return max(30, int(os.getenv("DELIVERABLES_PDF_TIMEOUT", "180")))
    except ValueError:
        return 180


def _kill_started(pidfile: Path) -> None:
    try:
        pids = [p for p in pidfile.read_text(encoding="utf-8").strip().split(",") if p.strip().isdigit()]
    except OSError:
        return
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=30)


def convert(source: Path | str, out_pdf: Path | str | None = None, *, embed_fonts: bool = True) -> dict[str, Any]:
    """Convert a .docx or .pptx to PDF. Returns {ok, pdf, pages, embedded} or
    {ok: False, error}. Never raises for an Office failure."""
    src = Path(source).resolve()
    if not enabled():
        return {"ok": False, "skipped": True, "error": "PDF export is off on this machine"}
    if not src.is_file():
        return {"ok": False, "error": f"missing source file: {src.name}"}
    suffix = src.suffix.lower()
    if suffix == ".docx":
        script = _WORD_PS
    elif suffix == ".pptx":
        script = _POWERPOINT_PS
    else:
        return {"ok": False, "error": f"cannot convert {suffix} files"}
    out = Path(out_pdf).resolve() if out_pdf else src.with_suffix(".pdf")
    if out.exists():
        return {"ok": False, "error": f"refusing to overwrite {out.name}"}

    size_before = src.stat().st_size
    with _LOCK, tempfile.TemporaryDirectory() as tmp:
        ps1 = Path(tmp) / "convert.ps1"
        ps1.write_text(script, encoding="utf-8")
        pidfile = Path(tmp) / "office.pid"
        cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(ps1), "-In", str(src), "-Out", str(out), "-PidFile", str(pidfile),
            "-Embed", "1" if embed_fonts else "0",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_timeout(), creationflags=flags)
        except subprocess.TimeoutExpired:
            _kill_started(pidfile)
            return {"ok": False, "error": f"Office did not finish within {_timeout()} seconds"}
        except OSError as exc:
            return {"ok": False, "error": f"could not start PowerShell: {exc}"}
        stdout = (proc.stdout or "").strip()
        if proc.returncode != 0 or "OK" not in stdout or not out.is_file():
            _kill_started(pidfile)
            detail = (proc.stderr or stdout or "no output").strip()
            return {"ok": False, "error": detail[-500:]}
        m = re.search(r"pages=(\d+)", stdout)
        return {
            "ok": True,
            "pdf": str(out),
            "pages": int(m.group(1)) if m else None,
            # Word always embeds on request. PowerPoint may decline silently,
            # so report embedding only when the file actually grew.
            "embedded": bool(embed_fonts) and (suffix == ".docx" or src.stat().st_size > size_before),
        }
