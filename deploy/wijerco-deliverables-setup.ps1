# One-time setup on wijerco for Deliverables (documents from the Command Centre).
# Safe to run more than once. Run it AFTER deploy\wijerco-update.bat has pulled
# the Deliverables code (this script arrives with that pull). The update's import
# check passes without these packages because the renderers import them lazily;
# documents simply cannot render until this has run.
#
#   1. Install the Python packages the renderers need (python-pptx, markdown-it-py).
#   2. Install Open Sans for this Windows user if it is missing.
#   3. Probe Word: render a one-page test document and convert it to PDF, the
#      same way an approved deliverable is finalised.
#
# Everything is logged to deploy\deliverables-setup-log.txt.

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $PSScriptRoot 'deliverables-setup-log.txt'
$py = Join-Path $repo '.venv\Scripts\python.exe'
Start-Transcript -Path $log -Force | Out-Null
Write-Host "Deliverables setup, repo: $repo" -ForegroundColor Cyan

# 1. Packages ---------------------------------------------------------------
Write-Host "[1/3] Installing Python packages ..."
& $py -m pip install --disable-pip-version-check -q "python-pptx>=1.0,<2" "markdown-it-py>=3.0"
& $py -c "import docx, markdown_it, pptx; print('   packages OK: python-docx, markdown-it-py, python-pptx')"
if ($LASTEXITCODE -ne 0) { Write-Host "   PACKAGE CHECK FAILED. Documents cannot render until this passes." -ForegroundColor Red }

# 2. Open Sans --------------------------------------------------------------
Write-Host "[2/3] Checking Open Sans ..."
$userFonts = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts'
$have = (Test-Path 'C:\Windows\Fonts\OpenSans-Regular.ttf') -or (Test-Path (Join-Path $userFonts 'OpenSans-Regular.ttf'))
if ($have) {
  Write-Host "   Open Sans already installed."
} else {
  New-Item -ItemType Directory -Force $userFonts | Out-Null
  $base = 'https://github.com/googlefonts/opensans/raw/main/fonts/ttf'
  $faces = @{ 'OpenSans-Regular.ttf' = 'Open Sans (TrueType)'; 'OpenSans-Bold.ttf' = 'Open Sans Bold (TrueType)';
              'OpenSans-Italic.ttf' = 'Open Sans Italic (TrueType)'; 'OpenSans-BoldItalic.ttf' = 'Open Sans Bold Italic (TrueType)';
              'OpenSans-SemiBold.ttf' = 'Open Sans SemiBold (TrueType)' }
  foreach ($file in $faces.Keys) {
    $dest = Join-Path $userFonts $file
    try {
      Invoke-WebRequest "$base/$file" -OutFile $dest -UseBasicParsing -TimeoutSec 60
      New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts' -Name $faces[$file] -Value $dest -PropertyType String -Force | Out-Null
      Write-Host "   installed $file"
    } catch { Write-Host "   could not install $file : $($_.Exception.Message)" -ForegroundColor Yellow }
  }
  Write-Host "   Open Sans installed for this user. Word picks it up on its next start."
}

# 3. Word probe -------------------------------------------------------------
Write-Host "[3/3] Probing Word (renders a test document and converts it to PDF) ..."
$probe = @"
import tempfile, pathlib, sys
sys.path.insert(0, r'$repo')
from media import doc_render, office_convert
d = pathlib.Path(tempfile.mkdtemp())
src = d / 'probe-v1.docx'
doc_render.render_docx('# Probe\n\n## Check\n\nWord conversion works.\n', src, {'title': 'Probe', 'version': 1})
r = office_convert.convert(src)
print('   Word result:', r)
sys.exit(0 if r.get('ok') else 3)
"@
$tmp = Join-Path $env:TEMP 'wj_deliverables_probe.py'
Set-Content -Path $tmp -Value $probe -Encoding UTF8
& $py $tmp
if ($LASTEXITCODE -eq 0) {
  Write-Host "   Word conversion OK. Approved deliverables will get a PDF." -ForegroundColor Green
} else {
  Write-Host "   Word conversion did not complete. Open Word once on this machine, clear any" -ForegroundColor Yellow
  Write-Host "   sign-in, activation or first-run prompts, close it, then run this script again." -ForegroundColor Yellow
  Write-Host "   Deliverables still work without it; approved documents just have no PDF." -ForegroundColor Yellow
}

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "Done. Log: $log"
Write-Host "No restart needed: the renderers load these packages when a document is made."
