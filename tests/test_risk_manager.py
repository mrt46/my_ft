"""Unit tests for custom_modules.risk_manager."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_modules.risk_manager import RiskManager, RiskState, HealthReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def risk_manager(tmp_path):
    """Return a RiskManager with mocked capital manager."""
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "risk:\n"
        "  max_daily_loss_pct: -5.0\n"
        "  max_consecutive_losses: 5\n"
        "  max_open_positions: 15\n"
        "  circuit_breaker_cooldown_hours: 4\n"
    )

    mock_capital = MagicMock()
    mock_capital.get_balance_snapshot.return_value = {
        "total": 1000.0,
        "grid_locked": 600.0,
        "screener_locked": 100.0,
        "available": 300.0,
        "timestamp": time.time(),
    }
    mock_capital._positions = {}

    state_file = tmp_path / "risk_state.json"

    with patch("custom_modules.risk_manager.Path") as mock_path:
        mock_path.return_value.__truediv__.return_value = settings
        mock_path.return_value.parent.parent.__truediv__.return_value = settings

        mgr = RiskManager.__new__(RiskManager)
        mgr._capital = mock_capital
        mgr._max_daily_loss_pct = -5.0
        mgr._max_consecutive_losses = 5
        mgr._max_open_positions = 15
        mgr._cooldown_hours = 4.0
        mgr.STATE_FILE = state_file
        mgr._state: RiskState = {
            "consecutive_losses": 0,
            "daily_pnl_usdc": 0.0,
            "daily_start_balance": 1000.0,
            "circuit_breaker_active": False,
            "circuit_breaker_until": 0.0,
            "last_reset_date": time.strftime("%Y-%m-%d", time.gmtime()),
            "paused_reason": "",
        }

        return mgr


# ---------------------------------------------------------------------------
# Trading permission
# ---------------------------------------------------------------------------

class TestIsTradingAllowed:
    def test_allows_trading_when_healthy(self, risk_manager):
        """Should allow trading when no circuit breaker and under limits."""
        assert risk_manager.is_trading_allowed() is True

    def test_blocks_when_circuit_breaker_active(self, risk_manager):
        """Should block trading when circuit breaker is active."""
        risk_manager._state["circuit_breaker_active"] = True
        risk_manager._state["paused_reason"] = "Test circuit breaker"

        assert risk_manager.is_trading_allowed() is False

    def test_blocks_at_position_limit(self, risk_manager):
        """Should block when max open positions reached."""
        risk_manager._capital._positions = {
            f"COIN{i}/USDC": {"type": "screener", "locked_usdc": 10.0}
            for i in range(15)
        }

        assert risk_manager.is_trading_allowed() is False

    def test_allows_below_position_limit(self, risk_manager):
        """Should allow when under position limit."""
        risk_manager._capital._positions = {
            f"COIN{i}/USDC": {"type": "screener", "locked_usdc": 10.0}
            for i in range(14)
        }

        assert risk_manager.is_trading_allowed() is True


# ---------------------------------------------------------------------------
# Trade result recording
# ---------------------------------------------------------------------------

class TestRecordTradeResult:
    def test_records_win(self, risk_manager):
        """Win should reset consecutive losses."""
        risk_manager._state["consecutive_losses"] = 3

        risk_manager.record_trade_result(pnl_usdc=50.0, pnl_pct=5.0)

        assert risk_manager._state["consecutive_losses"] == 0
        assert risk_manager._state["daily_pnl_usdc"] == 50.0

    def test_records_loss(self, risk_manager):
        """Loss should increment consecutive losses."""
        risk_manager._state["consecutive_losses"] = 2

        risk_manager.record_trade_result(pnl_usdc=-30.0, pnl_pct=-3.0)

        assert risk_manager._state["consecutive_losses"] == 3
        assert risk_manager._state["daily_pnl_usdc"] == -30.0

    def test_triggers_circuit_breaker_on_daily_loss(self, risk_manager):
        """Should activate circuit breaker when daily loss exceeds limit."""
        risk_manager._state["daily_start_balance"] = 1000.0
        risk_manager._state["daily_pnl_usdc"] = -50.0  # -5%

        risk_manager.record_trade_result(pnl_usdc=-1.0, pnl_pct=-0.1)

        assert risk_manager._state["circuit_breaker_active"] is True
        assert "Günlük kayıp limiti" in risk_manager._state["paused_reason"]

    def test_triggers_circuit_breaker_on_consecutive_losses(self, risk_manager):
        """Should activate circuit breaker when consecutive losses exceed limit."""
        risk_manager._state["consecutive_losses"] = 4

        risk_manager.record_trade_result(pnl_usdc=-10.0, pnl_pct=-1.0)

        assert risk_manager._state["circuit_breaker_active"] is True
        assert "Ardışık kayıp limiti" in risk_manager._state["paused_reason"]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_manual_reset(self, risk_manager):
        """Manual reset should clear circuit breaker."""
        risk_manager._state["circuit_breaker_active"] = True
        risk_manager._state["paused_reason"] = "Test"
        risk_manager._state["circuit_breaker_until"] = time.time() + 3600

        risk_manager.manually_reset_circuit_breaker()

        assert risk_manager._state["circuit_breaker_active"] is False
        assert risk_manager._state["paused_reason"] == ""
        assert risk_manager._state["circuit_breaker_until"] == 0.0

    def test_auto_expires_after_cooldown(self, risk_manager):
        """Circuit breaker should auto-expire after cooldown."""
        risk_manager._state["circuit_breaker_active"] = True
        risk_manager._state["circuit_breaker_until"] = time.time() - 1  # Expired

        risk_manager._check_cooldown_expiry()

        assert risk_manager._state["circuit_breaker_active"] is False

    def test_does_not_expire_before_cooldown(self, risk_manager):
        """Circuit breaker should not expire before cooldown."""
        risk_manager._state["circuit_breaker_active"] = True
        risk_manager._state["circuit_breaker_until"] = time.time() + 3600  # Future

        risk_manager._check_cooldown_expiry()

        assert risk_manager._state["circuit_breaker_active"] is True


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_healthy_status(self, risk_manager):
        """Should report healthy when all metrics normal."""
        report = risk_manager.health_check()

        assert report["status"] == "healthy"
        assert report["circuit_breaker"] is False
        assert report["consecutive_losses"] == 0

    def test_degraded_status_near_limits(self, risk_manager):
        """Should report degraded when approaching limits."""
        risk_manager._state["consecutive_losses"] = 3  # 60% of limit

        report = risk_manager.health_check()

        assert report["status"] == "degraded"

    def test_critical_status_with_circuit_breaker(self, risk_manager):
        """Should report critical when circuit breaker active."""
        risk_manager._state["circuit_breaker_active"] = True

        report = risk_manager.health_check()

        assert report["status"] == "critical"
        assert report["circuit_breaker"] is True

    def test_daily_pnl_calculation(self, risk_manager):
        """Should calculate daily P&L percentage correctly."""
        risk_manager._state["daily_start_balance"] = 1000.0
        risk_manager._state["daily_pnl_usdc"] = -30.0

        report = risk_manager.health_check()

        assert report["daily_pnl_pct"] == -3.0  # -30/1000 * 100


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------

class TestDailyReset:
    def test_resets_counters_on_new_day(self, risk_manager):
        """Should reset counters at midnight UTC."""
        risk_manager._state["last_reset_date"] = "2024-01-01"  # Old date
        risk_manager._state["consecutive_losses"] = 5
        risk_manager._state["daily_pnl_usdc"] = -100.0

        with patch("time.strftime", return_value="2024-01-02"):
            risk_manager._reset_if_new_day()

        assert risk_manager._state["consecutive_losses"] == 0
        assert risk_manager._state["daily_pnl_usdc"] == 0.0
        assert risk_manager._state["last_reset_date"] == "2024-01-02"

    def test_no_reset_same_day(self, risk_manager):
        """Should not reset counters on same day."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        risk_manager._state["last_reset_date"] = today
        risk_manager._state["consecutive_losses"] = 3

        risk_manager._reset_if_new_day()

        assert risk_manager._state["consecutive_losses"] == 3


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_saves_state(self, risk_manager):
        """Should save state to file."""
        risk_manager._save_state()

        assert risk_manager.STATE_FILE.exists()
        saved = json.loads(risk_manager.STATE_FILE.read_text())
        assert saved["consecutive_losses"] == 0

    def test_loads_state(self, risk_manager):
        """Should load state from file."""
        # Pre-populate state file
        saved_state = {
            "consecutive_losses": 3,
            "daily_pnl_usdc": -50.0,
            "daily_start_balance": 1000.0,
            "circuit_breaker_active": True,
            "circuit_breaker_until": time.time() + 3600,
            "last_reset_date": time.strftime("%Y-%m-%d", time.gmtime()),
            "paused_reason": "Test",
        }
        risk_manager.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        risk_manager.STATE_FILE.write_text(json.dumps(saved_state))

        loaded = risk_manager._load_state()

        assert loaded["consecutive_losses"] == 3
        assert loaded["circuit_breaker_active"] is True
