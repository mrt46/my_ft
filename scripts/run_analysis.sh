#!/bin/bash
# run_analysis.sh — 2-hour grid analysis cron job
# Cron: 0 */2 * * * /path/to/scripts/run_analysis.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/analysis.log"

cd "$PROJECT_DIR"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] run_analysis.sh started" >> "$LOG_FILE"

source venv/bin/activate 2>/dev/null || true

python -c "
import sys
sys.path.insert(0, '.')
from custom_modules.api_wrapper import ResilientExchangeWrapper
from custom_modules.grid_analyzer import GridAnalyzer
from custom_modules.sentiment_analyzer import SentimentAnalyzer
from custom_modules.grid_fusion import GridFusion

exchange = ResilientExchangeWrapper()
analyzer = GridAnalyzer(exchange)
sentiment = SentimentAnalyzer()
fusion = GridFusion()

grids = analyzer.analyze_all()
print(f'Analyzed {len(grids)} pairs')

fusion.run()
print('Grid fusion complete')
" >> "$LOG_FILE" 2>&1

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] run_analysis.sh done" >> "$LOG_FILE"
