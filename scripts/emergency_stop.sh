#!/bin/bash
# emergency_stop.sh — Manual kill switch
# Usage: ./scripts/emergency_stop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "⚠️  EMERGENCY STOP INITIATED"
echo "   Timestamp: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

source venv/bin/activate 2>/dev/null || true

# Kill main.py
pkill -f "python main.py" && echo "✅ main.py stopped" || echo "   main.py not running"

# Kill Freqtrade
pkill -f "freqtrade" && echo "✅ freqtrade stopped" || echo "   freqtrade not running"

# Cancel all open orders (via Python)
python -c "
import sys
sys.path.insert(0, '.')
try:
    from custom_modules.api_wrapper import ResilientExchangeWrapper
    from custom_modules.telegram_bot import send_alert_sync

    exchange = ResilientExchangeWrapper()
    open_orders = exchange.fetch_open_orders()
    print(f'Open orders found: {len(open_orders)}')

    for order in open_orders:
        result = exchange.cancel_order(order['id'], order['symbol'])
        print(f'  Cancelled: {order[\"id\"]} ({order[\"symbol\"]}) — {result}')

    send_alert_sync('🚨 EMERGENCY STOP executed! All open orders cancelled.')
    print('Emergency stop complete.')
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
"

echo "Done."
