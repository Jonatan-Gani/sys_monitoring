# Bootstrap for sys_monitoring on Windows.
# Run from an elevated PowerShell prompt if you want service install to succeed:
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python not found. Install Python 3.10+ from python.org and re-run."
    exit 1
}

$verStr = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$verStr -lt [version]"3.10") {
    Write-Error "Python 3.10+ required (found $verStr)."
    exit 1
}

if (-not $env:SKIP_VENV) {
    if (-not (Test-Path .venv)) {
        Write-Host ">>> Creating virtualenv at .venv"
        python -m venv .venv
    }
    $py = ".\.venv\Scripts\python.exe"
} else {
    $py = "python"
}

Write-Host ">>> Installing dependencies"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -e . --quiet

Write-Host ">>> Launching sysmon init"
& $py sysmon.py init @args
