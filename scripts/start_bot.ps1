#!/usr/bin/env pwsh
# ============================================================
# start_bot.ps1 - Akilli Grid Trading Bot Launcher (Windows)
# .env'den API keylerini okur, main.py baslatir
#
# Kullanim:
#   .\scripts\start_bot.ps1              # Dry-run (guvenli)
#   .\scripts\start_bot.ps1 -Live        # CANLI (dikkatli ol!)
# ============================================================
param(
    [switch]$Live,
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Akilli Grid Trading Bot - Launcher" -ForegroundColor Cyan
if ($Live) {
    Write-Host "  MODE: LIVE TRADING  *** GERCEK PARA! ***" -ForegroundColor Red
} else {
    Write-Host "  MODE: DRY-RUN (Kagit islem)" -ForegroundColor Green
}
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------------
# 1. .env dosyasini oku
# ------------------------------------------------------------------
$envFile = Join-Path $ROOT ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[HATA] .env dosyasi bulunamadi: $envFile" -ForegroundColor Red
    Write-Host "       Lutfen .env.example dosyasini kopyala ve doldur." -ForegroundColor Yellow
    exit 1
}

$envVars = @{}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $envVars[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
}

# Kritik keyler var mi kontrol et
$requiredKeys = @("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
$missing = @()
foreach ($key in $requiredKeys) {
    if (-not $envVars.ContainsKey($key) -or $envVars[$key] -eq "" -or $envVars[$key] -like "*your_*") {
        $missing += $key
    }
}

if ($missing.Count -gt 0) {
    Write-Host "[HATA] Asagidaki API keyleri .env'de eksik veya doldurulmamis:" -ForegroundColor Red
    foreach ($k in $missing) { Write-Host "  - $k" -ForegroundColor Yellow }
    exit 1
}

Write-Host "[OK] .env dosyasi okundu - tum API keyleri mevcut" -ForegroundColor Green

# ------------------------------------------------------------------
# 2. Env vars ayarla
# ------------------------------------------------------------------
$env:FREQTRADE__EXCHANGE__KEY    = $envVars["BINANCE_API_KEY"]
$env:FREQTRADE__EXCHANGE__SECRET = $envVars["BINANCE_API_SECRET"]
$env:PYTHONIOENCODING = "utf-8"

foreach ($kv in $envVars.GetEnumerator()) {
    [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value, "Process")
}

Write-Host "[OK] API keyleri ortam degiskenlerine aktarildi" -ForegroundColor Green

# ------------------------------------------------------------------
# 3. venv aktif et
# ------------------------------------------------------------------
$venvActivate = Join-Path $ROOT "venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
    Write-Host "[OK] Python venv aktif edildi" -ForegroundColor Green
} else {
    Write-Host "[UYARI] venv bulunamadi, sistem Python kullaniliyor" -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# 4. data/final_grid.json kontrolu
# ------------------------------------------------------------------
$gridFile = Join-Path $ROOT "data\final_grid.json"
if (Test-Path $gridFile) {
    $gridData = Get-Content $gridFile -Raw | ConvertFrom-Json
    $pairCount = ($gridData.PSObject.Properties | Where-Object { $_.Name -ne "_note" -and $_.Name -ne "_generated" }).Count
    Write-Host "[OK] Grid data: $pairCount coin yuklendi" -ForegroundColor Green
} else {
    Write-Host "[UYARI] data/final_grid.json bulunamadi - ilk grid analizi bekleniyor" -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# 5. main.py (orchestrator) baslatma
# ------------------------------------------------------------------
Write-Host ""
Write-Host "Bot baslatiliyor..." -ForegroundColor Cyan
Write-Host ""
Write-Host "Faydali komutlar:" -ForegroundColor Cyan
Write-Host "  Telegram: /health  /status  /report" -ForegroundColor White
Write-Host "  Terminal: type logs\status.json" -ForegroundColor White
Write-Host "  Log izle: Get-Content logs\trades.log -Wait -Tail 20" -ForegroundColor White
Write-Host ""
Write-Host "Durdurmak icin Ctrl+C basin." -ForegroundColor Green
Write-Host ""

Set-Location $ROOT
$mainArgs = @("main.py", "--log-level", $LogLevel)
if (-not $Live) { $mainArgs += "--dry-run" }

try {
    & python @mainArgs
} finally {
    Write-Host ""
    Write-Host "Bot durduruldu." -ForegroundColor Green
}
