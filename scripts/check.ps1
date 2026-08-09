# Local CI gate: runs the same checks as .github/workflows/ci.yml (except HACS
# validation, which needs the GitHub API). Requires Docker Desktop.
#
#   .\scripts\check.ps1          # everything
#   .\scripts\check.ps1 -Fast    # ruff + pytest only, skip hassfest

param(
    [switch]$Fast
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

Write-Host "== Ruff + pytest (python:3.14 container) ==" -ForegroundColor Cyan
docker run --rm -v "${repo}:/app" -w /app -v aecc-pip-cache:/root/.cache/pip python:3.14 sh -c @"
pip install -q -r requirements_test.txt 2>&1 | tail -1
ruff check custom_components/aecc_battery tests || exit 1
ruff format --check custom_components/aecc_battery tests || exit 1
pytest tests/ -q --tb=short
"@
if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: ruff/pytest" -ForegroundColor Red; exit 1 }

if (-not $Fast) {
    Write-Host "== Hassfest (ghcr.io/home-assistant/hassfest) ==" -ForegroundColor Cyan
    docker run --rm -v "${repo}:/github/workspace" ghcr.io/home-assistant/hassfest
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: hassfest" -ForegroundColor Red; exit 1 }
}

Write-Host "All checks passed." -ForegroundColor Green
