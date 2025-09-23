Param(
  [string]$ConfigPath = "pull_monthly/config_rg.yml",
  [string]$OutPath = $null,                 # e.g., "outputs/costs_2025-08.csv"
  [double]$Sleep = 1.0,
  [int]$MaxRetries = 8,
  [string]$ClientType = "tony-cost-collector",
  [string]$VenvPath = ".\.venv"             # adjust if your venv lives elsewhere
)

$ErrorActionPreference = "Stop"

Write-Host "==> Activating virtual environment..." -ForegroundColor Cyan
$activateWin = Join-Path $VenvPath "Scripts\Activate.ps1"
$activateNix = Join-Path $VenvPath "bin\activate.ps1"
if (Test-Path $activateWin) {
  & $activateWin
} elseif (Test-Path $activateNix) {
  . $activateNix
} else {
  Write-Error "Could not find venv activate script at $activateWin or $activateNix"
  exit 1
}

# Ensure outputs/ exists if user relies on default output path
if (-not $OutPath) {
  if (-not (Test-Path "outputs")) {
    New-Item -ItemType Directory -Path "outputs" | Out-Null
  }
}

Write-Host "==> Checking Azure login..." -ForegroundColor Cyan
az account show --only-show-errors | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "No active session. Starting 'az login --use-device-code'..." -ForegroundColor Yellow
  az login --use-device-code | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Azure login failed."
    exit 1
  }
}

Write-Host "==> Running monthly pull..." -ForegroundColor Cyan
$scriptPath = "pull_monthly\rg_monthly_costs.py"
if (-not (Test-Path $scriptPath)) {
  Write-Error "Could not find $scriptPath"
  exit 1
}

# Build args
$pyArgs = @("--config", $ConfigPath, "--sleep", $Sleep, "--maxretries", $MaxRetries, "--clienttype", $ClientType)
if ($OutPath) { $pyArgs += @("--out", $OutPath) }

python $scriptPath @pyArgs
if ($LASTEXITCODE -ne 0) {
  Write-Error "rg_monthly_costs.py failed with exit code $LASTEXITCODE"
  exit $LASTEXITCODE
}

Write-Host "==> Done." -ForegroundColor Green
