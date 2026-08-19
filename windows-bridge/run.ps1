# Loads ../.env and runs the native MT5 bridge on Windows (uvicorn on :8000).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"

Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)\s*=\s*(.*)\s*$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
}

Set-Location $PSScriptRoot
python -m uvicorn app:app --host 0.0.0.0 --port 8000
