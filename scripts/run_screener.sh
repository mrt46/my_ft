#!/bin/bash
# run_screener.sh — Daily 00:00 UTC screener cron job
# Cron: 0 0 * * * /path/to/scripts/run_screener.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/analysis.log"

cd "$PROJECT_DIR"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] run_screener.sh started" >> "$LOG_FILE"

source venv/bin/activate 2>/dev/null || true

python -c "
import sys
sys.path.insert(0, '.')
from custom_modules.api_wrapper import ResilientExchangeWrapper
from custom_modules.screener import Screener

exchange = ResilientExchangeWrapper()
screener = Screener(exchange)

candidates = screener.daily_screener()
print(f'Screener found {len(candidates)} candidates')
for c in candidates:
    print(f'  {c[\"pair\"]}: score={c[\"score\"]} rsi4h={c[\"rsi_4h\"]} dist={c[\"distance_pct\"]:.1f}%')
" >> "$LOG_FILE" 2>&1

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] run_screener.sh done" >> "$LOG_FILE"
