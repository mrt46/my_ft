#!/bin/bash
# health_check.sh — 30-second health check
# Cron:
#   * * * * * /path/to/scripts/health_check.sh
#   * * * * * sleep 30; /path/to/scripts/health_check.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/logs/api_errors.log"

cd "$PROJECT_DIR"

source venv/bin/activate 2>/dev/null || true

python -c "
import sys
sys.path.insert(0, '.')
from custom_modules.api_wrapper import ResilientExchangeWrapper

exchange = ResilientExchangeWrapper()
result = exchange.health_check()
status = result.get('status', 'unknown')
print(f'Health: {status}')
if status != 'healthy':
    import sys
    print(f'ERROR: {result}', file=sys.stderr)
    sys.exit(1)
" >> "$LOG_FILE" 2>&1
