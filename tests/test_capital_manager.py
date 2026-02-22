"""Unit tests for custom_modules.capital_manager."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_modules.capital_manager import CapitalManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cm(tmp_path):
    """Return a CapitalManager with mocked exchange and temp config."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        "capital:\n"
        "  total_usdc: 1000\n"
        "  grid_min_reserve: 600\n"
        "  screener_max_per_position: 100\n"
        "  screener_min_per_position: 20\n"
        "  low_balance_alert_threshold: 100\n"
        "  deposit_detection_threshold: 50\n"
    )

    mock_exchange = MagicMock()
    mock_exchange.fetch_balance.return_value = {
        "USDC": {"free": 1000.0, "used": 0.0, "total": 1000.0}
    }

    with patch("custom_modules.capital_manager.Path") as mock_path:
        mock_path.return_value.__truediv__.return_value = config_dir / "settings.yaml"
        # Let POSITIONS_FILE and QUEUE_FILE point to tmp_path
        manager = CapitalManager.__new__(CapitalManager)
        manager._exchange = mock_exchange
        manager._positions = {}
        manager._pending_queue = []
        manager._total_usdc = 1000.0
        manager._grid_min_reserve = 600.0
        manager._screener_max = 100.0
        manager._screener_min = 20.0
        manager._alert_threshold = 100.0
        manager._deposit_threshold = 50.0
        manager._last_known_balance = 0.0
        manager.POSITIONS_FILE = tmp_path / "positions.json"
        manager.QUEUE_FILE = tmp_path / "screener_queue.json"
        return manager


# ---------------------------------------------------------------------------
# Balance snapshot
# ---------------------------------------------------------------------------

class TestBalanceSnapshot:
    def test_no_locked_positions(self, cm):
        snap = cm.get_balance_snapshot()
        assert snap["total"] == 1000.0
        assert snap["grid_locked"] == 0.0
        assert snap["screener_locked"] == 0.0
        assert snap["available"] == 1000.0

    def test_with_grid_position(self, cm):
        cm._positions["grid_btc"] = {
            "pair": "BTC/USDC", "type": "grid",
            "locked_usdc": 200.0, "entry_price": 50000,
            "amount": 0.004, "timestamp": time.time()
        }
        snap = cm.get_balance_snapshot()
        assert snap["grid_locked"] == 200.0
        assert snap["available"] == 800.0

    def test_with_screener_position(self, cm):
        cm._positions["MATIC/USDC"] = {
            "pair": "MATIC/USDC", "type": "screener",
            "locked_usdc": 100.0, "entry_price": 0.85,
            "amount": 117.6, "timestamp": time.time()
        }
        snap = cm.get_balance_snapshot()
        assert snap["screener_locked"] == 100.0
        assert snap["available"] == 900.0


# ---------------------------------------------------------------------------
# Trade gate
# ---------------------------------------------------------------------------

class TestTradeGate:
    def test_screener_allowed_when_sufficient(self, cm):
        assert cm.can_open_screener_trade(50) is True

    def test_screener_blocked_when_would_breach_reserve(self, cm):
        # Lock 650 USDC in grid (already at reserve)
        cm._positions["grid_1"] = {
            "pair": "BTC/USDC", "type": "grid",
            "locked_usdc": 650.0, "entry_price": 50000,
            "amount": 0.013, "timestamp": time.time()
        }
        assert cm.can_open_screener_trade(100) is False

    def test_grid_allowed_when_reserve_not_full(self, cm):
        assert cm.can_open_grid_trade(90) is True

    def test_grid_blocked_when_reserve_exceeded(self, cm):
        cm._positions["g"] = {
            "pair": "ETH/USDC", "type": "grid",
            "locked_usdc": 590.0, "entry_price": 3000,
            "amount": 0.2, "timestamp": time.time()
        }
        # Reserve 600, locked 590, requesting 20 → 600-590=10 < 20
        assert cm.can_open_grid_trade(20) is False


# ---------------------------------------------------------------------------
# Lock / Release
# ---------------------------------------------------------------------------

class TestLockRelease:
    def test_lock_screener_adds_position(self, cm):
        cm.lock_screener("MATIC/USDC", 80.0, 0.85, 94.1)
        assert "MATIC/USDC" in cm._positions
        assert cm._positions["MATIC/USDC"]["type"] == "screener"

    def test_release_screener_removes_position(self, cm):
        cm.lock_screener("SOL/USDC", 60.0, 20.0, 3.0)
        released = cm.release("SOL/USDC", "screener")
        assert released == 60.0
        assert "SOL/USDC" not in cm._positions

    def test_release_returns_zero_for_unknown(self, cm):
        assert cm.release("UNKNOWN/USDC", "screener") == 0.0


# ---------------------------------------------------------------------------
# Pending queue
# ---------------------------------------------------------------------------

class TestPendingQueue:
    def test_add_to_queue(self, cm):
        cm.add_to_pending_queue("XRP/USDC", 50.0, 75)
        assert len(cm._pending_queue) == 1
        assert cm._pending_queue[0]["pair"] == "XRP/USDC"

    def test_queue_sorted_by_score(self, cm):
        cm.add_to_pending_queue("ADA/USDC", 30.0, 55)
        cm.add_to_pending_queue("DOT/USDC", 30.0, 85)
        assert cm._pending_queue[0]["pair"] == "DOT/USDC"  # higher score first


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_positions_saved_and_loaded(self, cm, tmp_path):
        cm.lock_screener("AVAX/USDC", 70.0, 35.0, 2.0)
        # Create a new instance pointing at same files
        cm2 = CapitalManager.__new__(CapitalManager)
        cm2._exchange = cm._exchange
        cm2._positions = {}
        cm2._pending_queue = []
        cm2.POSITIONS_FILE = cm.POSITIONS_FILE
        cm2.QUEUE_FILE = cm.QUEUE_FILE
        cm2._total_usdc = 1000
        cm2._grid_min_reserve = 600
        cm2._screener_max = 100
        cm2._screener_min = 20
        cm2._alert_threshold = 100
        cm2._deposit_threshold = 50
        cm2._last_known_balance = 0
        cm2._load_positions()
        assert "AVAX/USDC" in cm2._positions
