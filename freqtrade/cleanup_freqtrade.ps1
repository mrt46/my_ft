# ============================================================
# cleanup_freqtrade.ps1
# Freqtrade repository minimal production cleanup
# Calistirma: cd freqtrade && .\cleanup_freqtrade.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$FREQTRADE_ROOT = $PSScriptRoot

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  Freqtrade Minimal Production Cleanup" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Bu script gereksiz Freqtrade dosyalarini silerek" -ForegroundColor Yellow
Write-Host "repoyu minimal production setup'a indirecek." -ForegroundColor Yellow
Write-Host ""

# ─────────────────────────────────────────────────────────────
# ADIM 1: Kullanicidan 3 kritik soru
# ─────────────────────────────────────────────────────────────

Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "ADIM 1: Kullanim senaryonuzu belirleyin" -ForegroundColor White
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# Soru 1: Backtesting
Write-Host "Soru 1/3: Backtesting kullanacak misiniz?" -ForegroundColor White
Write-Host "  (DynamicGridStrategy'yi canli oncesi test etmek icin)" -ForegroundColor DarkGray
Write-Host "  Oneri: EVET - Canli trading oncesi backtest sartli" -ForegroundColor Green
$backtest = Read-Host "  Backtest tutulsun mu? [E/h] (Enter = Evet)"
$keepBacktest = ($backtest -eq "" -or $backtest -match "^[Ee]")

Write-Host ""

# Soru 2: Plotting
Write-Host "Soru 2/3: Grafik/Plot cizecek misiniz?" -ForegroundColor White
Write-Host "  (Telegram bot zaten trade bildirimleri yapiyor)" -ForegroundColor DarkGray
Write-Host "  Oneri: HAYIR - Telegram yeterli, plot gereksiz" -ForegroundColor Green
$plot = Read-Host "  Plot tutulsun mu? [e/H] (Enter = Hayir)"
$keepPlot = ($plot -match "^[Ee]")

Write-Host ""

# Soru 3: FreqAI
Write-Host "Soru 3/3: FreqAI (ML sinyaller) kullanacak misiniz?" -ForegroundColor White
Write-Host "  (Biz kendi AI modulumuzu kullaniyoruz: DeepSeek + GPT-4o + Gemini)" -ForegroundColor DarkGray
Write-Host "  Oneri: HAYIR - custom_modules/sentiment_analyzer.py yeterli" -ForegroundColor Green
$freqai = Read-Host "  FreqAI tutulsun mu? [e/H] (Enter = Hayir)"
$keepFreqAI = ($freqai -match "^[Ee]")

# ─────────────────────────────────────────────────────────────
# ADIM 2: Silinecekleri listele + onay al
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "ADIM 2: Silinecekler ozeti" -ForegroundColor White
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

Write-Host "HER ZAMAN SILINECEKLER:" -ForegroundColor Red
$alwaysDelete = @(
    "docs",
    "tests",
    ".github",
    "build_helpers",
    "docker",
    "docker-compose.yml",
    "Dockerfile",
    ".dockerignore",
    ".gitattributes",
    ".pre-commit-config.yaml",
    ".readthedocs.yml",
    "mkdocs.yml",
    "mypy.ini",
    "pyrightconfig.json",
    "pytest.ini",
    "tox.ini",
    ".coveragerc",
    "codecov.yml",
    "MANIFEST.in",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "requirements-dev.txt",
    "requirements-freqai.txt",
    "requirements-freqai-rl.txt"
)

foreach ($item in $alwaysDelete) {
    $fullPath = Join-Path $FREQTRADE_ROOT $item
    if (Test-Path $fullPath) {
        Write-Host "  [SIL] $item" -ForegroundColor Red
    } else {
        Write-Host "  [YOK] $item" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "FREQTRADE IC KLASORLER (secimlerinize gore):" -ForegroundColor Yellow

$ftRoot = Join-Path $FREQTRADE_ROOT "freqtrade"

if (-not $keepBacktest) {
    Write-Host "  [SIL] freqtrade/optimize/  (backtest=hayir)" -ForegroundColor Red
    Write-Host "  [SIL] requirements-hyperopt.txt" -ForegroundColor Red
} else {
    Write-Host "  [TUT] freqtrade/optimize/  (backtest=evet)" -ForegroundColor Green
    Write-Host "  [TUT] requirements-hyperopt.txt" -ForegroundColor Green
}

if (-not $keepPlot) {
    Write-Host "  [SIL] freqtrade/plot/      (plot=hayir)" -ForegroundColor Red
    Write-Host "  [SIL] requirements-plot.txt" -ForegroundColor Red
} else {
    Write-Host "  [TUT] freqtrade/plot/      (plot=evet)" -ForegroundColor Green
    Write-Host "  [TUT] requirements-plot.txt" -ForegroundColor Green
}

if (-not $keepFreqAI) {
    Write-Host "  [SIL] freqtrade/freqai/    (freqai=hayir)" -ForegroundColor Red
} else {
    Write-Host "  [TUT] freqtrade/freqai/    (freqai=evet)" -ForegroundColor Green
}

Write-Host ""
Write-Host "FREQTRADE IC KLASORLER (her zaman silinecek):" -ForegroundColor Red
Write-Host "  [SIL] freqtrade/templates/  (ornek strategy'ler)" -ForegroundColor Red

Write-Host ""
Write-Host "USER_DATA TEMIZLIGI:" -ForegroundColor Red
Write-Host "  [SIL] user_data/notebooks/" -ForegroundColor Red
Write-Host "  [SIL] user_data/freqaimodels/" -ForegroundColor Red
Write-Host "  [SIL] user_data/hyperopts/" -ForegroundColor Red
Write-Host "  [SIL] user_data/backtest_results/ (bos)" -ForegroundColor Red

Write-Host ""
Write-Host "ASLA SILINMEYECEKLER:" -ForegroundColor Green
Write-Host "  [TUT] pyproject.toml         <- Freqtrade CLI icin kritik!" -ForegroundColor Green
Write-Host "  [TUT] scripts/               <- api_wrapper referansi" -ForegroundColor Green
Write-Host "  [TUT] ft_client/             <- REST API client" -ForegroundColor Green
Write-Host "  [TUT] freqtrade.service      <- Deployment" -ForegroundColor Green
Write-Host "  [TUT] setup.ps1 / setup.sh   <- Kurulum" -ForegroundColor Green
Write-Host "  [TUT] requirements.txt       <- Core deps" -ForegroundColor Green
Write-Host "  [TUT] config_examples/       <- config.json referansi" -ForegroundColor Green
Write-Host "  [TUT] LICENSE, README.md     <- Legal + info" -ForegroundColor Green

# ─────────────────────────────────────────────────────────────
# ADIM 3: Son onay
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "ADIM 3: Onay" -ForegroundColor White
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "⚠️  UYARI: Bu islem geri alinamaz!" -ForegroundColor Red
Write-Host ""
$confirm = Read-Host "Devam etmek istiyor musunuz? Evet icin 'EVET' yazin"

if ($confirm -ne "EVET") {
    Write-Host ""
    Write-Host "❌ Iptal edildi. Hicbir dosya silinmedi." -ForegroundColor Yellow
    exit 0
}

# ─────────────────────────────────────────────────────────────
# ADIM 4: Silme islemi
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "ADIM 4: Siliniyor..." -ForegroundColor White
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

function Remove-SafeItem {
    param($Path, $Label)
    if (Test-Path $Path) {
        Remove-Item -Recurse -Force $Path
        Write-Host "  ✅ Silindi: $Label" -ForegroundColor Green
    } else {
        Write-Host "  ⏭  Zaten yok: $Label" -ForegroundColor DarkGray
    }
}

# --- Root dizin temizligi ---
Write-Host "Root dizin temizleniyor..." -ForegroundColor Cyan

Remove-SafeItem (Join-Path $FREQTRADE_ROOT "docs") "docs/"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "tests") "tests/"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".github") ".github/"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "build_helpers") "build_helpers/"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "docker") "docker/"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "docker-compose.yml") "docker-compose.yml"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "Dockerfile") "Dockerfile"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".dockerignore") ".dockerignore"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".gitattributes") ".gitattributes"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".pre-commit-config.yaml") ".pre-commit-config.yaml"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".readthedocs.yml") ".readthedocs.yml"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "mkdocs.yml") "mkdocs.yml"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "mypy.ini") "mypy.ini"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "pyrightconfig.json") "pyrightconfig.json"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "pytest.ini") "pytest.ini"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "tox.ini") "tox.ini"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT ".coveragerc") ".coveragerc"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "codecov.yml") "codecov.yml"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "MANIFEST.in") "MANIFEST.in"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "CONTRIBUTING.md") "CONTRIBUTING.md"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "CODE_OF_CONDUCT.md") "CODE_OF_CONDUCT.md"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "SECURITY.md") "SECURITY.md"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "CHANGELOG.md") "CHANGELOG.md"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "requirements-dev.txt") "requirements-dev.txt"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "requirements-freqai.txt") "requirements-freqai.txt"
Remove-SafeItem (Join-Path $FREQTRADE_ROOT "requirements-freqai-rl.txt") "requirements-freqai-rl.txt"

# Freqtrade iç temizlik
Write-Host ""
Write-Host "Freqtrade ic klasorler temizleniyor..." -ForegroundColor Cyan

Remove-SafeItem (Join-Path $ftRoot "templates") "freqtrade/templates/"

if (-not $keepBacktest) {
    Remove-SafeItem (Join-Path $ftRoot "optimize") "freqtrade/optimize/"
    Remove-SafeItem (Join-Path $FREQTRADE_ROOT "requirements-hyperopt.txt") "requirements-hyperopt.txt"
}

if (-not $keepPlot) {
    Remove-SafeItem (Join-Path $ftRoot "plot") "freqtrade/plot/"
    Remove-SafeItem (Join-Path $FREQTRADE_ROOT "requirements-plot.txt") "requirements-plot.txt"
}

if (-not $keepFreqAI) {
    Remove-SafeItem (Join-Path $ftRoot "freqai") "freqtrade/freqai/"
}

# user_data temizlik
Write-Host ""
Write-Host "user_data temizleniyor..." -ForegroundColor Cyan

$userData = Join-Path $FREQTRADE_ROOT "user_data"
Remove-SafeItem (Join-Path $userData "notebooks") "user_data/notebooks/"
Remove-SafeItem (Join-Path $userData "freqaimodels") "user_data/freqaimodels/"
Remove-SafeItem (Join-Path $userData "hyperopts") "user_data/hyperopts/"
Remove-SafeItem (Join-Path $userData "backtest_results") "user_data/backtest_results/"

# Strategies klasoru olustur (bos dahi olsa)
$strategiesDir = Join-Path $userData "strategies"
if (-not (Test-Path $strategiesDir)) {
    New-Item -ItemType Directory -Path $strategiesDir | Out-Null
    Write-Host "  ✅ Olusturuldu: user_data/strategies/" -ForegroundColor Green
}

# ─────────────────────────────────────────────────────────────
# ADIM 5: Dogrulama
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "ADIM 5: Dogrulama" -ForegroundColor White
Write-Host "─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# pyproject.toml kontrolu
if (Test-Path (Join-Path $FREQTRADE_ROOT "pyproject.toml")) {
    Write-Host "  ✅ pyproject.toml mevcut" -ForegroundColor Green
} else {
    Write-Host "  ❌ HATA: pyproject.toml silindi! Freqtrade calismaycak!" -ForegroundColor Red
}

# scripts/ kontrolu
if (Test-Path (Join-Path $FREQTRADE_ROOT "scripts")) {
    Write-Host "  ✅ scripts/ mevcut (api_wrapper referansi)" -ForegroundColor Green
} else {
    Write-Host "  ⚠  scripts/ bulunamadi" -ForegroundColor Yellow
}

# ft_client/ kontrolu
if (Test-Path (Join-Path $FREQTRADE_ROOT "ft_client")) {
    Write-Host "  ✅ ft_client/ mevcut (REST API client)" -ForegroundColor Green
} else {
    Write-Host "  ⚠  ft_client/ bulunamadi" -ForegroundColor Yellow
}

# Core freqtrade package kontrolu
$coreFiles = @("freqtradebot.py", "main.py", "worker.py", "wallets.py")
$allCoreOk = $true
foreach ($file in $coreFiles) {
    if (-not (Test-Path (Join-Path $ftRoot $file))) {
        $allCoreOk = $false
        Write-Host "  ❌ HATA: freqtrade/$file eksik!" -ForegroundColor Red
    }
}
if ($allCoreOk) {
    Write-Host "  ✅ Core freqtrade dosyalari tam" -ForegroundColor Green
}

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  ✅ Cleanup tamamlandi!" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Siradaki adim:" -ForegroundColor White
Write-Host "  1. user_data/strategies/DynamicGridStrategy.py olustur" -ForegroundColor Yellow
Write-Host "  2. user_data/config.json olustur (config_examples/config_binance.example.json'dan)" -ForegroundColor Yellow
Write-Host "  3. freqtrade trade --dry-run ile test et" -ForegroundColor Yellow
Write-Host ""
