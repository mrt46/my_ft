#!/bin/bash
# ============================================================
# cleanup_freqtrade.sh
# Freqtrade repository minimal production cleanup
# Kullanim: cd freqtrade && chmod +x cleanup_freqtrade.sh && ./cleanup_freqtrade.sh
# ============================================================

set -e

FREQTRADE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FT_ROOT="$FREQTRADE_ROOT/freqtrade"

# Renkler
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m' # No Color

echo ""
echo -e "${CYAN}======================================================"
echo -e "  Freqtrade Minimal Production Cleanup"
echo -e "======================================================${NC}"
echo ""
echo -e "${YELLOW}Bu script gereksiz Freqtrade dosyalarini silerek"
echo -e "repoyu minimal production setup'a indirecek.${NC}"
echo ""

# ─────────────────────────────────────────────────────────────
# ADIM 1: 3 kritik soru
# ─────────────────────────────────────────────────────────────

echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo -e "${NC}ADIM 1: Kullanim senaryonuzu belirleyin${NC}"
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo ""

# Soru 1
echo -e "${NC}Soru 1/3: Backtesting kullanacak misiniz?"
echo -e "${GRAY}  (DynamicGridStrategy'yi canli oncesi test icin)"
echo -e "${GREEN}  Oneri: EVET - Canli trading oncesi backtest sartli${NC}"
read -p "  Backtest tutulsun mu? [E/h] (Enter = Evet): " backtest_ans
if [[ "$backtest_ans" =~ ^[Hh] ]]; then
    KEEP_BACKTEST=false
else
    KEEP_BACKTEST=true
fi

echo ""

# Soru 2
echo -e "${NC}Soru 2/3: Grafik/Plot cizecek misiniz?"
echo -e "${GRAY}  (Telegram bot zaten trade bildirimleri yapiyor)"
echo -e "${GREEN}  Oneri: HAYIR - Telegram yeterli, plot gereksiz${NC}"
read -p "  Plot tutulsun mu? [e/H] (Enter = Hayir): " plot_ans
if [[ "$plot_ans" =~ ^[Ee] ]]; then
    KEEP_PLOT=true
else
    KEEP_PLOT=false
fi

echo ""

# Soru 3
echo -e "${NC}Soru 3/3: FreqAI (ML sinyaller) kullanacak misiniz?"
echo -e "${GRAY}  (Kendi AI modulumuz var: DeepSeek + GPT-4o + Gemini)"
echo -e "${GREEN}  Oneri: HAYIR - custom_modules/sentiment_analyzer.py yeterli${NC}"
read -p "  FreqAI tutulsun mu? [e/H] (Enter = Hayir): " freqai_ans
if [[ "$freqai_ans" =~ ^[Ee] ]]; then
    KEEP_FREQAI=true
else
    KEEP_FREQAI=false
fi

# ─────────────────────────────────────────────────────────────
# ADIM 2: Silinecekleri listele
# ─────────────────────────────────────────────────────────────

echo ""
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo -e "ADIM 2: Silinecekler ozeti"
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo ""

echo -e "${RED}HER ZAMAN SILINECEKLER:${NC}"
for item in docs tests .github build_helpers docker \
    docker-compose.yml Dockerfile .dockerignore .gitattributes \
    .pre-commit-config.yaml .readthedocs.yml mkdocs.yml mypy.ini \
    pyrightconfig.json pytest.ini tox.ini .coveragerc codecov.yml \
    MANIFEST.in CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md CHANGELOG.md \
    requirements-dev.txt requirements-freqai.txt requirements-freqai-rl.txt; do
    if [ -e "$FREQTRADE_ROOT/$item" ]; then
        echo -e "  ${RED}[SIL]${NC} $item"
    else
        echo -e "  ${GRAY}[YOK]${NC} $item"
    fi
done

echo ""
echo -e "${YELLOW}FREQTRADE IC KLASORLER (secimlerinize gore):${NC}"

if [ "$KEEP_BACKTEST" = false ]; then
    echo -e "  ${RED}[SIL]${NC} freqtrade/optimize/  (backtest=hayir)"
    echo -e "  ${RED}[SIL]${NC} requirements-hyperopt.txt"
else
    echo -e "  ${GREEN}[TUT]${NC} freqtrade/optimize/  (backtest=evet)"
    echo -e "  ${GREEN}[TUT]${NC} requirements-hyperopt.txt"
fi

if [ "$KEEP_PLOT" = false ]; then
    echo -e "  ${RED}[SIL]${NC} freqtrade/plot/      (plot=hayir)"
    echo -e "  ${RED}[SIL]${NC} requirements-plot.txt"
else
    echo -e "  ${GREEN}[TUT]${NC} freqtrade/plot/      (plot=evet)"
    echo -e "  ${GREEN}[TUT]${NC} requirements-plot.txt"
fi

if [ "$KEEP_FREQAI" = false ]; then
    echo -e "  ${RED}[SIL]${NC} freqtrade/freqai/    (freqai=hayir)"
else
    echo -e "  ${GREEN}[TUT]${NC} freqtrade/freqai/    (freqai=evet)"
fi

echo ""
echo -e "${RED}FREQTRADE IC (her zaman silinecek):${NC}"
echo -e "  ${RED}[SIL]${NC} freqtrade/templates/"

echo ""
echo -e "${RED}USER_DATA TEMIZLIGI:${NC}"
echo -e "  ${RED}[SIL]${NC} user_data/notebooks/"
echo -e "  ${RED}[SIL]${NC} user_data/freqaimodels/"
echo -e "  ${RED}[SIL]${NC} user_data/hyperopts/"
echo -e "  ${RED}[SIL]${NC} user_data/backtest_results/"

echo ""
echo -e "${GREEN}ASLA SILINMEYECEKLER:${NC}"
echo -e "  ${GREEN}[TUT]${NC} pyproject.toml         <- Freqtrade CLI icin kritik!"
echo -e "  ${GREEN}[TUT]${NC} scripts/               <- api_wrapper referansi"
echo -e "  ${GREEN}[TUT]${NC} ft_client/             <- REST API client"
echo -e "  ${GREEN}[TUT]${NC} freqtrade.service      <- Deployment"
echo -e "  ${GREEN}[TUT]${NC} setup.sh               <- Linux kurulum"
echo -e "  ${GREEN}[TUT]${NC} requirements.txt       <- Core deps"
echo -e "  ${GREEN}[TUT]${NC} config_examples/       <- config.json referansi"
echo -e "  ${GREEN}[TUT]${NC} LICENSE, README.md"

# ─────────────────────────────────────────────────────────────
# ADIM 3: Son onay
# ─────────────────────────────────────────────────────────────

echo ""
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo -e "ADIM 3: Onay"
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo ""
echo -e "${RED}⚠️  UYARI: Bu islem geri alinamaz!${NC}"
echo ""
read -p "Devam etmek istiyor musunuz? Evet icin 'EVET' yazin: " confirm

if [ "$confirm" != "EVET" ]; then
    echo ""
    echo -e "${YELLOW}❌ Iptal edildi. Hicbir dosya silinmedi.${NC}"
    exit 0
fi

# ─────────────────────────────────────────────────────────────
# ADIM 4: Silme islemi
# ─────────────────────────────────────────────────────────────

echo ""
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo -e "ADIM 4: Siliniyor..."
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo ""

remove_safe() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ]; then
        rm -rf "$path"
        echo -e "  ${GREEN}✅ Silindi:${NC} $label"
    else
        echo -e "  ${GRAY}⏭  Zaten yok:${NC} $label"
    fi
}

# Root temizlik
echo -e "${CYAN}Root dizin temizleniyor...${NC}"
remove_safe "$FREQTRADE_ROOT/docs" "docs/"
remove_safe "$FREQTRADE_ROOT/tests" "tests/"
remove_safe "$FREQTRADE_ROOT/.github" ".github/"
remove_safe "$FREQTRADE_ROOT/build_helpers" "build_helpers/"
remove_safe "$FREQTRADE_ROOT/docker" "docker/"
remove_safe "$FREQTRADE_ROOT/docker-compose.yml" "docker-compose.yml"
remove_safe "$FREQTRADE_ROOT/Dockerfile" "Dockerfile"
remove_safe "$FREQTRADE_ROOT/.dockerignore" ".dockerignore"
remove_safe "$FREQTRADE_ROOT/.gitattributes" ".gitattributes"
remove_safe "$FREQTRADE_ROOT/.pre-commit-config.yaml" ".pre-commit-config.yaml"
remove_safe "$FREQTRADE_ROOT/.readthedocs.yml" ".readthedocs.yml"
remove_safe "$FREQTRADE_ROOT/mkdocs.yml" "mkdocs.yml"
remove_safe "$FREQTRADE_ROOT/mypy.ini" "mypy.ini"
remove_safe "$FREQTRADE_ROOT/pyrightconfig.json" "pyrightconfig.json"
remove_safe "$FREQTRADE_ROOT/pytest.ini" "pytest.ini"
remove_safe "$FREQTRADE_ROOT/tox.ini" "tox.ini"
remove_safe "$FREQTRADE_ROOT/.coveragerc" ".coveragerc"
remove_safe "$FREQTRADE_ROOT/codecov.yml" "codecov.yml"
remove_safe "$FREQTRADE_ROOT/MANIFEST.in" "MANIFEST.in"
remove_safe "$FREQTRADE_ROOT/CONTRIBUTING.md" "CONTRIBUTING.md"
remove_safe "$FREQTRADE_ROOT/CODE_OF_CONDUCT.md" "CODE_OF_CONDUCT.md"
remove_safe "$FREQTRADE_ROOT/SECURITY.md" "SECURITY.md"
remove_safe "$FREQTRADE_ROOT/CHANGELOG.md" "CHANGELOG.md"
remove_safe "$FREQTRADE_ROOT/requirements-dev.txt" "requirements-dev.txt"
remove_safe "$FREQTRADE_ROOT/requirements-freqai.txt" "requirements-freqai.txt"
remove_safe "$FREQTRADE_ROOT/requirements-freqai-rl.txt" "requirements-freqai-rl.txt"

# Freqtrade ic temizlik
echo ""
echo -e "${CYAN}Freqtrade ic klasorler temizleniyor...${NC}"
remove_safe "$FT_ROOT/templates" "freqtrade/templates/"

if [ "$KEEP_BACKTEST" = false ]; then
    remove_safe "$FT_ROOT/optimize" "freqtrade/optimize/"
    remove_safe "$FREQTRADE_ROOT/requirements-hyperopt.txt" "requirements-hyperopt.txt"
fi

if [ "$KEEP_PLOT" = false ]; then
    remove_safe "$FT_ROOT/plot" "freqtrade/plot/"
    remove_safe "$FREQTRADE_ROOT/requirements-plot.txt" "requirements-plot.txt"
fi

if [ "$KEEP_FREQAI" = false ]; then
    remove_safe "$FT_ROOT/freqai" "freqtrade/freqai/"
fi

# user_data temizlik
echo ""
echo -e "${CYAN}user_data temizleniyor...${NC}"
remove_safe "$FREQTRADE_ROOT/user_data/notebooks" "user_data/notebooks/"
remove_safe "$FREQTRADE_ROOT/user_data/freqaimodels" "user_data/freqaimodels/"
remove_safe "$FREQTRADE_ROOT/user_data/hyperopts" "user_data/hyperopts/"
remove_safe "$FREQTRADE_ROOT/user_data/backtest_results" "user_data/backtest_results/"

# strategies dizini olustur
mkdir -p "$FREQTRADE_ROOT/user_data/strategies"
echo -e "  ${GREEN}✅ Hazir:${NC} user_data/strategies/"

# ─────────────────────────────────────────────────────────────
# ADIM 5: Dogrulama
# ─────────────────────────────────────────────────────────────

echo ""
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo -e "ADIM 5: Dogrulama"
echo -e "${GRAY}──────────────────────────────────────────────────────${NC}"
echo ""

if [ -f "$FREQTRADE_ROOT/pyproject.toml" ]; then
    echo -e "  ${GREEN}✅${NC} pyproject.toml mevcut"
else
    echo -e "  ${RED}❌ HATA: pyproject.toml silindi! Freqtrade calismaycak!${NC}"
fi

if [ -d "$FREQTRADE_ROOT/scripts" ]; then
    echo -e "  ${GREEN}✅${NC} scripts/ mevcut (api_wrapper referansi)"
fi

if [ -d "$FREQTRADE_ROOT/ft_client" ]; then
    echo -e "  ${GREEN}✅${NC} ft_client/ mevcut (REST API client)"
fi

for f in freqtradebot.py main.py worker.py wallets.py; do
    if [ ! -f "$FT_ROOT/$f" ]; then
        echo -e "  ${RED}❌ HATA: freqtrade/$f eksik!${NC}"
    fi
done

echo -e "  ${GREEN}✅${NC} Core freqtrade dosyalari tam"

echo ""
echo -e "${CYAN}======================================================"
echo -e "  ✅ Cleanup tamamlandi!"
echo -e "======================================================${NC}"
echo ""
echo -e "${NC}Siradaki adim:${NC}"
echo -e "${YELLOW}  1. user_data/strategies/DynamicGridStrategy.py olustur"
echo -e "  2. user_data/config.json olustur (config_examples/config_binance.example.json'dan)"
echo -e "  3. freqtrade trade --dry-run ile test et${NC}"
echo ""
