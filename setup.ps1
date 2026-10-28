# Windows setup.  Run in PowerShell:  .\setup.ps1
# If blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
$ErrorActionPreference = "Stop"

Write-Host "Net of Fees - setup (Windows)"
Write-Host ""

# Find a Python between 3.10 and 3.12. 3.13+ has no wheels yet for the
# audio stack, which is why the newest version is not used.
$py = $null
foreach ($v in @("3.12", "3.11", "3.10")) {
    try {
        $out = & py "-$v" -c "import sys; print(sys.version.split()[0])" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break }
    } catch { }
}
if (-not $py) {
    try {
        $ver = & python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
        if ($ver -in @("3.10", "3.11", "3.12")) { $py = @("python") }
    } catch { }
}
if (-not $py) {
    Write-Host "ERROR: need Python 3.10, 3.11 or 3.12." -ForegroundColor Red
    Write-Host "  winget install Python.Python.3.11"
    exit 1
}
Write-Host "  python     $(& $py[0] $py[1..$py.Length] --version)"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "ERROR: FFmpeg not found on PATH." -ForegroundColor Red
    Write-Host "  winget install Gyan.FFmpeg"
    Write-Host "  then CLOSE AND REOPEN this terminal so PATH refreshes."
    exit 1
}
Write-Host "  ffmpeg     found"

Write-Host ""
Write-Host "  creating .venv ..."
& $py[0] $py[1..$py.Length] -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
Write-Host "  installing requirements ..."
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "  created .env from .env.example"
}

Write-Host ""
& .\.venv\Scripts\python.exe run.py doctor
Write-Host "Next:"
Write-Host "  .\.venv\Scripts\python.exe run.py new --recipe fee_drag"
Write-Host "  .\.venv\Scripts\python.exe run.py 001"
