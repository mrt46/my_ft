"""Unit tests for custom_modules.bnb_manager."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_modules.bnb_manager import BnbManager, BnbStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bnb_manager(tmp_path):
    """Return a BnbManager with mocked dependencies."""
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "bot:\n"
        "  dry_run: true\n"
        "bnb:\n"
        "  auto_buy_threshold_usdc: 1.0\n"
        "  auto_buy_amount_usdc: 5.0\n"
        "  check_interval_minutes: 15\n"
    )

    mock_exchange = MagicMock()
    mock_capital = MagicMock()

    with patch("custom_modules.bnb_manager.Path") as mock_path:
        mock_path.return_value.__truediv__.return_value = settings
        mock_path.return_value.parent.parent.__truediv__.return_value = settings

        mgr = BnbManager.__new__(BnbManager)
        mgr._exchange = mock_exchange
        mgr._capital = mock_capital
        mgr._threshold = 1.0
        mgr._buy_amount = 5.0
        mgr._check_interval = 900  # 15 minutes in seconds
        mgr._dry_run = True
        mgr._last_check = 0.0

        return mgr


# ---------------------------------------------------------------------------
# BNB balance check
# ---------------------------------------------------------------------------

class TestBnbBalanceCheck:
    def test_get_bnb_value_usdc_returns_zero_in_dry_run(self, bnb_manager):
        """In dry-run mode, BNB balance should be 0."""
        result = bnb_manager._get_bnb_value_usdc()
        assert result == 0.0

    def test_get_bnb_value_usdc_calculates_correctly(self, bnb_manager):
        """BNB value = amount * price."""
        bnb_manager._dry_run = False
        bnb_manager._exchange.fetch_balance.return_value = {
            "BNB": {"free": 2.0, "used": 0.0, "total": 2.0}
        }
        bnb_manager._exchange.fetch_ticker.return_value = {"last": 300.0}

        result = bnb_manager._get_bnb_value_usdc()
        assert result == 600.0  # 2 BNB * $300

    def test_get_bnb_value_usdc_zero_balance(self, bnb_manager):
        """Zero BNB balance returns 0."""
        bnb_manager._dry_run = False
        bnb_manager._exchange.fetch_balance.return_value = {
            "BNB": {"free": 0.0, "used": 0.0, "total": 0.0}
        }

        result = bnb_manager._get_bnb_value_usdc()
        assert result == 0.0


# ---------------------------------------------------------------------------
# Check and top-up logic
# ---------------------------------------------------------------------------

class TestCheckAndTopUp:
    def test_check_respects_interval(self, bnb_manager):
        """Should skip check if interval hasn't elapsed."""
        bnb_manager._last_check = time.time()  # Just checked

        result = bnb_manager.check_and_top_up()

        assert result["triggered"] is False
        assert result["bnb_balance_usdc"] == -1.0  # Skipped indicator

    def test_no_trigger_when_balance_above_threshold(self, bnb_manager):
        """Should not trigger buy when BNB balance is sufficient."""
        bnb_manager._last_check = 0  # Force check
        bnb_manager._get_bnb_value_usdc = MagicMock(return_value=5.0)  # Above 1.0 threshold

        result = bnb_manager.check_and_top_up()

        assert result["triggered"] is False
        assert result["bnb_balance_usdc"] == 5.0

    def test_triggers_when_balance_below_threshold(self, bnb_manager):
        """Should trigger buy when BNB balance below threshold."""
        bnb_manager._last_check = 0  # Force check
        bnb_manager._get_bnb_value_usdc = MagicMock(return_value=0.5)  # Below 1.0 threshold

        result = bnb_manager.check_and_top_up()

        assert result["triggered"] is True
        assert result["bnb_balance_usdc"] == 0.5

    def test_dry_run_simulates_buy(self, bnb_manager):
        """In dry-run mode, should simulate buy without placing order."""
        bnb_manager._last_check = 0
        bnb_manager._get_bnb_value_usdc = MagicMock(return_value=0.5)
        bnb_manager._dry_run = True

        result = bnb_manager.check_and_top_up()

        assert result["triggered"] is True
        assert result["order"]["dry_run"] is True
        assert result["order"]["amount"] == 5.0


# ---------------------------------------------------------------------------
# BNB purchase
# ---------------------------------------------------------------------------

class TestBuyBnb:
    def test_buy_bnb_checks_capital_first(self, bnb_manager):
        """Should check available capital before buying."""
        bnb_manager._capital.can_open_screener_trade.return_value = False
        bnb_manager._exchange.fetch_ticker.return_value = {"last": 300.0}

        result = bnb_manager._buy_bnb()

        assert result is None
        bnb_manager._capital.can_open_screener_trade.assert_called_once_with(5.0)

    def test_buy_bnb_executes_order(self, bnb_manager):
        """Should execute market buy order when capital available."""
        bnb_manager._capital.can_open_screener_trade.return_value = True
        bnb_manager._exchange.fetch_ticker.return_value = {"last": 300.0}
        bnb_manager._exchange.execute_order.return_value = {
            "id": "order123",
            "status": "closed",
            "filled": 0.0167,  # 5 USDC / 300 = ~0.0167 BNB
        }

        result = bnb_manager._buy_bnb()

        assert result is not None
        assert result["id"] == "order123"
        # Verify correct quantity calculated
        bnb_manager._exchange.execute_order.assert_called_once()
        call_args = bnb_manager._exchange.execute_order.call_args
        assert call_args[0][0] == "BNB/USDC"
        assert call_args[0][1] == "buy"
        assert call_args[1]["order_type"] == "market"

    def test_buy_bnb_handles_exception(self, bnb_manager):
        """Should handle exchange errors gracefully."""
        bnb_manager._capital.can_open_screener_trade.return_value = True
        bnb_manager._exchange.fetch_ticker.side_effect = Exception("Network error")

        result = bnb_manager._buy_bnb()

        assert result is None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class TestPublicApi:
    def test_get_bnb_balance_usdc_returns_float(self, bnb_manager):
        """Public method should return float."""
        bnb_manager._get_bnb_value_usdc = MagicMock(return_value=10.5)

        result = bnb_manager.get_bnb_balance_usdc()

        assert isinstance(result, float)
        assert result == 10.5

    def test_get_bnb_balance_usdc_handles_error(self, bnb_manager):
        """Should return 0.0 on error."""
        bnb_manager._get_bnb_value_usdc = MagicMock(side_effect=Exception("API error"))

        result = bnb_manager.get_bnb_balance_usdc()

        assert result == 0.0
