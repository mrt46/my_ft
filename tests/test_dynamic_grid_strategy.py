"""Unit tests for DynamicGridStrategy.

Tests cover:
- %3 net profit target (minimal_roi + custom_exit)
- Adaptive grid: cache TTL, refresh, pair-level notifications
- Entry/exit/DCA signal logic
- Telegram notifications via dp.send_msg()
- bot_loop_start startup summary

Note: Freqtrade imports are mocked since the test environment does not have
      a full Freqtrade installation. The strategy logic is tested in isolation.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Mock Freqtrade modules before importing strategy
# ---------------------------------------------------------------------------

# Create minimal mocks for freqtrade dependencies
_mock_trade = MagicMock()
_mock_trade.__class__.__name__ = "Trade"

_mock_freqtrade = MagicMock()
_mock_freqtrade.persistence.Trade = _mock_trade
_mock_freqtrade.strategy.IStrategy = object  # base class = plain object
_mock_freqtrade.enums.RPCMessageType = MagicMock()

sys.modules.setdefault("freqtrade", _mock_freqtrade)
sys.modules.setdefault("freqtrade.persistence", _mock_freqtrade.persistence)
sys.modules.setdefault("freqtrade.strategy", _mock_freqtrade.strategy)
sys.modules.setdefault("freqtrade.enums", _mock_freqtrade.enums)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 50, base_price: float = 1000.0) -> pd.DataFrame:
    """Generate a minimal OHLCV DataFrame for strategy testing."""
    rng = np.random.default_rng(42)
    prices = base_price + rng.uniform(-10, 10, n).cumsum()
    prices = np.abs(prices)
    data = {
        "open": prices,
        "high": prices * 1.005,
        "low": prices * 0.995,
        "close": prices,
        "volume": rng.uniform(100, 1000, n),
    }
    idx = pd.date_range("2025-01-01", periods=n, freq="5min")
    return pd.DataFrame(data, index=idx)


def _make_grid(
    pair: str = "BTC/USDC",
    levels: Optional[list] = None,
    position_size: float = 20.0,
    sentiment_score: float = 0.0,
) -> dict:
    """Return a minimal grid config dict."""
    if levels is None:
        levels = [990.0, 995.0, 1000.0, 1005.0, 1010.0]
    return {
        "pair": pair,
        "levels": levels,
        "upper_bound": max(levels),
        "lower_bound": min(levels),
        "position_size": position_size,
        "spacing": "tier_5levels",
        "sentiment_score": sentiment_score,
        "sentiment_applied": sentiment_score != 0.0,
        "timestamp": time.time(),
    }


def _make_trade(
    pair: str = "BTC/USDC",
    open_rate: float = 1000.0,
    stake_amount: float = 20.0,
    nr_of_successful_entries: int = 1,
) -> MagicMock:
    """Return a mock Trade object."""
    trade = MagicMock()
    trade.pair = pair
    trade.open_rate = open_rate
    trade.stake_amount = stake_amount
    trade.nr_of_successful_entries = nr_of_successful_entries
    trade.open_date_utc = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return trade


def _load_strategy_class():
    """Dynamically load DynamicGridStrategy with mocked Freqtrade."""
    strategy_path = Path(__file__).parents[1] / "freqtrade" / "user_data" / "strategies"
    if str(strategy_path) not in sys.path:
        sys.path.insert(0, str(strategy_path))

    # Remove cached module if present (allow re-import with mocks)
    sys.modules.pop("DynamicGridStrategy", None)

    import DynamicGridStrategy as strat_module  # type: ignore
    return strat_module


def _make_strategy(tmp_path: Path, grid_data: Optional[dict] = None):
    """Instantiate DynamicGridStrategy with a temp final_grid.json."""
    strat_module = _load_strategy_class()
    DynamicGridStrategy = strat_module.DynamicGridStrategy

    # Write temp grid file
    grid_file = tmp_path / "final_grid.json"
    if grid_data is None:
        grid_data = {
            "BTC/USDC": _make_grid("BTC/USDC"),
            "ETH/USDC": _make_grid("ETH/USDC", levels=[190.0, 195.0, 200.0, 205.0, 210.0], position_size=12.0),
        }
    grid_file.write_text(json.dumps(grid_data))

    # Patch FINAL_GRID_FILE at module level
    strat_module.FINAL_GRID_FILE = grid_file

    # Create strategy instance (bypass __init__ to avoid Freqtrade config)
    strategy = DynamicGridStrategy.__new__(DynamicGridStrategy)
    strategy._cache = {}
    strategy._cache_ts = 0.0
    strategy._CACHE_TTL = 300.0
    strategy._grid_notified = set()
    strategy._startup_notified = False
    strategy.max_entry_position_adjustment = 8

    # Mock DataProvider
    mock_dp = MagicMock()
    mock_dp.send_msg = MagicMock()
    strategy.dp = mock_dp

    return strategy, strat_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def strategy(tmp_path):
    s, _ = _make_strategy(tmp_path)
    return s


@pytest.fixture
def strategy_with_module(tmp_path):
    return _make_strategy(tmp_path)


@pytest.fixture
def strategy_no_grid(tmp_path):
    s, _ = _make_strategy(tmp_path, grid_data={})
    return s


# ---------------------------------------------------------------------------
# 1. Profit Target Tests (%3 net)
# ---------------------------------------------------------------------------

class TestProfitTarget:
    """Verify %3 net profit target is correctly configured."""

    def test_minimal_roi_is_3_percent(self, strategy):
        """minimal_roi at 0 minutes must be 0.03 (3%)."""
        assert strategy.minimal_roi["0"] == 0.03

    def test_minimal_roi_fallback_after_1_day(self, strategy):
        """After 1440 minutes (1 day), ROI target drops to 2%."""
        assert strategy.minimal_roi["1440"] == 0.02

    def test_minimal_roi_fallback_after_2_days(self, strategy):
        """After 2880 minutes (2 days), ROI target drops to 1%."""
        assert strategy.minimal_roi["2880"] == 0.01

    def test_stoploss_is_12_percent(self, strategy):
        """Stoploss must be -12% (below lowest grid level)."""
        assert strategy.stoploss == -0.12

    def test_custom_exit_ignores_low_profit(self, strategy):
        """custom_exit should return None when profit < MIN_PROFIT_FOR_EXIT (1%)."""
        trade = _make_trade(open_rate=1000.0)
        result = strategy.custom_exit(
            pair="BTC/USDC",
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=1005.0,
            current_profit=0.005,  # 0.5% — below 1% threshold
        )
        assert result is None

    def test_custom_exit_triggers_at_grid_target(self, strategy):
        """custom_exit should return 'grid_tp' when price reaches next grid level."""
        # Grid levels: [990, 995, 1000, 1005, 1010]
        # Entry at 995, current price at 1000 (next level above)
        trade = _make_trade(open_rate=995.0)
        result = strategy.custom_exit(
            pair="BTC/USDC",
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            current_profit=0.05,  # 5% — above 1% threshold
        )
        assert result == "grid_tp"

    def test_custom_exit_does_not_trigger_below_target(self, strategy):
        """custom_exit should return None when price is below target level."""
        trade = _make_trade(open_rate=995.0)
        result = strategy.custom_exit(
            pair="BTC/USDC",
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=997.0,  # Between 995 and 1000 — not at target
            current_profit=0.02,
        )
        assert result is None

    def test_custom_exit_sends_telegram_on_tp(self, strategy):
        """custom_exit should call dp.send_msg when TP is triggered."""
        trade = _make_trade(open_rate=995.0)
        strategy.custom_exit(
            pair="BTC/USDC",
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            current_profit=0.05,
        )
        strategy.dp.send_msg.assert_called_once()
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "GRID TP" in msg
        assert "BTC/USDC" in msg

    def test_custom_exit_no_grid_returns_none(self, strategy_no_grid):
        """custom_exit should return None when no grid data available."""
        trade = _make_trade(open_rate=1000.0)
        result = strategy_no_grid.custom_exit(
            pair="BTC/USDC",
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=1010.0,
            current_profit=0.05,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 2. Adaptive Grid Cache Tests
# ---------------------------------------------------------------------------

class TestAdaptiveGridCache:
    """Verify grid cache TTL and refresh behavior."""

    def test_cache_loads_from_file(self, strategy):
        """Cache should be populated from final_grid.json on first access."""
        levels = strategy._levels("BTC/USDC")
        assert len(levels) > 0
        assert levels == sorted(levels)

    def test_cache_ttl_prevents_reload(self, strategy):
        """Cache should NOT reload within TTL window."""
        strategy._levels("BTC/USDC")  # First load
        ts_after_load = strategy._cache_ts

        # Simulate time passing but within TTL (100s < 300s TTL)
        strategy._cache_ts = time.time() - 100
        prev_ts = strategy._cache_ts
        strategy._levels("BTC/USDC")  # Should not reload

        # Cache timestamp should NOT have changed (no reload within TTL)
        assert strategy._cache_ts == prev_ts

    def test_cache_reloads_after_ttl(self, strategy_with_module):
        """Cache should reload when TTL expires."""
        strategy, strat_module = strategy_with_module
        strategy._levels("BTC/USDC")  # First load

        # Expire the cache
        strategy._cache_ts = time.time() - 400  # 400s ago > TTL=300s

        # Update the grid file with new levels
        new_grid = {"BTC/USDC": _make_grid("BTC/USDC", levels=[900.0, 950.0, 1000.0])}
        strat_module.FINAL_GRID_FILE.write_text(json.dumps(new_grid))

        levels = strategy._levels("BTC/USDC")
        assert 900.0 in levels  # New levels loaded

    def test_cache_returns_empty_for_unknown_pair(self, strategy):
        """Should return empty list for pairs not in grid."""
        levels = strategy._levels("UNKNOWN/USDC")
        assert levels == []

    def test_grid_stake_returns_position_size(self, strategy):
        """_grid_stake should return position_size from grid config."""
        stake = strategy._grid_stake("BTC/USDC", fallback=10.0)
        assert stake == 20.0  # From _make_grid default

    def test_grid_stake_uses_fallback_for_unknown(self, strategy):
        """_grid_stake should use fallback for unknown pairs."""
        stake = strategy._grid_stake("UNKNOWN/USDC", fallback=15.0)
        assert stake == 15.0

    def test_new_pairs_reset_notification_flag(self, strategy_with_module):
        """When new pairs appear in grid, their notification flag should reset."""
        strategy, strat_module = strategy_with_module
        strategy._levels("BTC/USDC")  # Load initial cache
        strategy._grid_notified.add("BTC/USDC")

        # Expire cache and add new pair
        strategy._cache_ts = time.time() - 400
        new_grid = {
            "BTC/USDC": _make_grid("BTC/USDC"),
            "SOL/USDC": _make_grid("SOL/USDC", levels=[50.0, 55.0, 60.0]),
        }
        strat_module.FINAL_GRID_FILE.write_text(json.dumps(new_grid))

        strategy._refresh_cache()
        # SOL/USDC is new — should NOT be in notified set
        assert "SOL/USDC" not in strategy._grid_notified


# ---------------------------------------------------------------------------
# 3. Entry Signal Tests
# ---------------------------------------------------------------------------

class TestEntrySignals:
    """Verify populate_indicators and populate_entry_trend."""

    def test_near_support_column_exists(self, strategy):
        """populate_indicators should add 'near_support' column."""
        df = _make_df(50, 1000.0)
        result = strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        assert "near_support" in result.columns

    def test_grid_support_column_exists(self, strategy):
        """populate_indicators should add 'grid_support' column."""
        df = _make_df(50, 1000.0)
        result = strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        assert "grid_support" in result.columns

    def test_near_support_at_grid_level(self, strategy):
        """near_support should be 1 when price is within 0.5% of a grid level."""
        # Grid levels include 1000.0 — set close price to 1001.0 (0.1% above)
        df = _make_df(10, 1000.0)
        df["close"] = 1001.0  # Within 0.5% of 1000.0
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.001
        df["low"] = df["close"] * 0.999
        result = strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        assert result["near_support"].sum() > 0

    def test_near_support_zero_far_from_grid(self, strategy):
        """near_support should be 0 when price is far from all grid levels."""
        df = _make_df(10, 1000.0)
        df["close"] = 1050.0  # Far above all grid levels (max=1010)
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.001
        df["low"] = df["close"] * 0.999
        result = strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        assert result["near_support"].sum() == 0

    def test_no_grid_returns_zero_support(self, strategy_no_grid):
        """populate_indicators should return near_support=0 when no grid data."""
        df = _make_df(10, 1000.0)
        result = strategy_no_grid.populate_indicators(df, {"pair": "BTC/USDC"})
        assert result["near_support"].sum() == 0

    def test_entry_trend_set_on_support(self, strategy):
        """populate_entry_trend should set enter_long=1 at support levels."""
        df = _make_df(10, 1000.0)
        df["close"] = 1001.0
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.001
        df["low"] = df["close"] * 0.999
        df = strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        df["enter_long"] = 0
        df["enter_tag"] = ""
        result = strategy.populate_entry_trend(df, {"pair": "BTC/USDC"})
        assert result["enter_long"].sum() > 0

    def test_grid_notification_sent_on_first_load(self, strategy):
        """populate_indicators should send Telegram notification on first pair load."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        strategy.dp.send_msg.assert_called_once()
        assert "BTC/USDC" in strategy._grid_notified

    def test_grid_notification_not_sent_twice(self, strategy):
        """populate_indicators should NOT send duplicate notifications."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})  # Second call
        assert strategy.dp.send_msg.call_count == 1  # Only once


# ---------------------------------------------------------------------------
# 4. DCA Tests
# ---------------------------------------------------------------------------

class TestDCA:
    """Verify adjust_trade_position DCA logic."""

    def test_dca_triggers_at_next_level(self, strategy):
        """DCA should trigger when price reaches the next lower grid level."""
        # Grid: [990, 995, 1000, 1005, 1010]
        # Entry at 1000, DCA level = 995 (first below entry)
        trade = _make_trade(open_rate=1000.0, nr_of_successful_entries=1)
        result = strategy.adjust_trade_position(
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=995.5,  # Within 0.5% of 995.0
            current_profit=-0.005,
            min_stake=5.0,
            max_stake=100.0,
            current_entry_rate=1000.0,
            current_exit_rate=1000.0,
            current_entry_profit=-0.005,
            current_exit_profit=-0.005,
        )
        assert result is not None
        assert result > 0

    def test_dca_does_not_trigger_above_level(self, strategy):
        """DCA should NOT trigger when price is above the next DCA level + proximity."""
        # Grid: [990, 995, 1000, 1005, 1010], entry at 1000
        # Next DCA level = 995. Price at 997 is > 995 * 1.005 = 999.975 → NO trigger
        trade = _make_trade(open_rate=1000.0, nr_of_successful_entries=1)
        result = strategy.adjust_trade_position(
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,  # Well above 995 * 1.005 = 999.975 → no trigger
            current_profit=-0.000,
            min_stake=5.0,
            max_stake=100.0,
            current_entry_rate=1000.0,
            current_exit_rate=1000.0,
            current_entry_profit=-0.000,
            current_exit_profit=-0.000,
        )
        assert result is None

    def test_dca_respects_max_adjustments(self, strategy):
        """DCA should stop after max_entry_position_adjustment entries."""
        # Simulate 8 DCA entries already done (max=8)
        trade = _make_trade(open_rate=1000.0, nr_of_successful_entries=9)
        result = strategy.adjust_trade_position(
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=990.0,
            current_profit=-0.01,
            min_stake=5.0,
            max_stake=100.0,
            current_entry_rate=1000.0,
            current_exit_rate=1000.0,
            current_entry_profit=-0.01,
            current_exit_profit=-0.01,
        )
        assert result is None

    def test_dca_sends_telegram_notification(self, strategy):
        """DCA should send Telegram notification when triggered."""
        trade = _make_trade(open_rate=1000.0, nr_of_successful_entries=1)
        strategy.adjust_trade_position(
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=995.5,
            current_profit=-0.005,
            min_stake=5.0,
            max_stake=100.0,
            current_entry_rate=1000.0,
            current_exit_rate=1000.0,
            current_entry_profit=-0.005,
            current_exit_profit=-0.005,
        )
        strategy.dp.send_msg.assert_called_once()
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "GRID DCA" in msg
        assert "BTC/USDC" in msg

    def test_dca_no_grid_returns_none(self, strategy_no_grid):
        """DCA should return None when no grid data available."""
        trade = _make_trade(open_rate=1000.0)
        result = strategy_no_grid.adjust_trade_position(
            trade=trade,
            current_time=datetime.now(timezone.utc),
            current_rate=990.0,
            current_profit=-0.01,
            min_stake=5.0,
            max_stake=100.0,
            current_entry_rate=1000.0,
            current_exit_rate=1000.0,
            current_entry_profit=-0.01,
            current_exit_profit=-0.01,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 5. Startup Summary Tests
# ---------------------------------------------------------------------------

class TestStartupSummary:
    """Verify bot_loop_start sends startup summary."""

    def test_startup_summary_sent_on_first_loop(self, strategy):
        """bot_loop_start should send startup summary on first call."""
        strategy.bot_loop_start(current_time=datetime.now(timezone.utc))
        strategy.dp.send_msg.assert_called_once()
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "Bot baslatildi" in msg
        assert "BTC/USDC" in msg

    def test_startup_summary_not_sent_twice(self, strategy):
        """bot_loop_start should NOT send startup summary on subsequent calls."""
        strategy.bot_loop_start(current_time=datetime.now(timezone.utc))
        strategy.bot_loop_start(current_time=datetime.now(timezone.utc))
        assert strategy.dp.send_msg.call_count == 1

    def test_startup_summary_includes_profit_target(self, strategy):
        """Startup summary should mention %3 profit target."""
        strategy.bot_loop_start(current_time=datetime.now(timezone.utc))
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "%3" in msg or "0.03" in msg or "3" in msg

    def test_startup_summary_not_sent_when_no_grid(self, strategy_no_grid):
        """Startup summary should NOT be sent when grid is empty."""
        strategy_no_grid.bot_loop_start(current_time=datetime.now(timezone.utc))
        strategy_no_grid.dp.send_msg.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Sentiment Integration Tests
# ---------------------------------------------------------------------------

class TestSentimentIntegration:
    """Verify sentiment score is included in grid notifications."""

    def test_bullish_sentiment_shown_in_notification(self, tmp_path):
        """Grid notification should show positive sentiment score."""
        grid_data = {
            "BTC/USDC": _make_grid("BTC/USDC", sentiment_score=0.75),
        }
        strategy, _ = _make_strategy(tmp_path, grid_data)
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})

        msg = strategy.dp.send_msg.call_args[0][0]
        assert "0.75" in msg or "+0.75" in msg

    def test_bearish_sentiment_shown_in_notification(self, tmp_path):
        """Grid notification should show negative sentiment score."""
        grid_data = {
            "BTC/USDC": _make_grid("BTC/USDC", sentiment_score=-0.60),
        }
        strategy, _ = _make_strategy(tmp_path, grid_data)
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})

        msg = strategy.dp.send_msg.call_args[0][0]
        assert "-0.60" in msg or "0.60" in msg

    def test_neutral_sentiment_shown_in_notification(self, tmp_path):
        """Grid notification should show neutral sentiment indicator."""
        grid_data = {
            "BTC/USDC": _make_grid("BTC/USDC", sentiment_score=0.05),
        }
        strategy, _ = _make_strategy(tmp_path, grid_data)
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})

        msg = strategy.dp.send_msg.call_args[0][0]
        # Neutral sentiment should show "~" indicator
        assert "~" in msg or "0.05" in msg


# ---------------------------------------------------------------------------
# 7. Custom Stake Amount Tests
# ---------------------------------------------------------------------------

class TestCustomStakeAmount:
    """Verify custom_stake_amount uses grid position_size."""

    def test_uses_grid_position_size(self, strategy):
        """custom_stake_amount should return position_size from grid config."""
        result = strategy.custom_stake_amount(
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            proposed_stake=50.0,
            min_stake=5.0,
            max_stake=100.0,
            leverage=1.0,
            entry_tag="grid_support",
            side="long",
            pair="BTC/USDC",
        )
        assert result == 20.0  # position_size from _make_grid

    def test_respects_min_stake(self, strategy):
        """custom_stake_amount should not go below min_stake."""
        # Force cache load first, then set position_size very low
        strategy._levels("BTC/USDC")  # Populate cache
        strategy._cache["BTC/USDC"]["position_size"] = 1.0
        result = strategy.custom_stake_amount(
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            proposed_stake=50.0,
            min_stake=10.0,
            max_stake=100.0,
            leverage=1.0,
            entry_tag="grid_support",
            side="long",
            pair="BTC/USDC",
        )
        assert result >= 10.0

    def test_respects_max_stake(self, strategy):
        """custom_stake_amount should not exceed max_stake."""
        # Force cache load first, then set position_size very high
        strategy._levels("BTC/USDC")  # Populate cache
        strategy._cache["BTC/USDC"]["position_size"] = 500.0
        result = strategy.custom_stake_amount(
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            proposed_stake=50.0,
            min_stake=5.0,
            max_stake=100.0,
            leverage=1.0,
            entry_tag="grid_support",
            side="long",
            pair="BTC/USDC",
        )
        assert result <= 100.0

    def test_uses_proposed_stake_for_unknown_pair(self, strategy):
        """custom_stake_amount should use proposed_stake for unknown pairs."""
        result = strategy.custom_stake_amount(
            current_time=datetime.now(timezone.utc),
            current_rate=1000.0,
            proposed_stake=30.0,
            min_stake=5.0,
            max_stake=100.0,
            leverage=1.0,
            entry_tag="grid_support",
            side="long",
            pair="UNKNOWN/USDC",
        )
        assert result == 30.0


# ---------------------------------------------------------------------------
# 8. Grid Notification Content Tests
# ---------------------------------------------------------------------------

class TestGridNotificationContent:
    """Verify grid notification message content."""

    def test_notification_includes_pair_name(self, strategy):
        """Grid notification should include pair name."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "BTC/USDC" in msg

    def test_notification_includes_position_size(self, strategy):
        """Grid notification should include position size."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "20" in msg  # position_size = 20.0

    def test_notification_includes_level_count(self, strategy):
        """Grid notification should include number of levels."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "5" in msg  # 5 levels in _make_grid default

    def test_notification_includes_timestamp(self, strategy):
        """Grid notification should include UTC timestamp."""
        df = _make_df(10, 1000.0)
        strategy.populate_indicators(df, {"pair": "BTC/USDC"})
        msg = strategy.dp.send_msg.call_args[0][0]
        assert "UTC" in msg

    def test_dp_send_gracefully_handles_missing_dp(self, strategy):
        """_dp_send should not raise when dp is None."""
        strategy.dp = None
        # Should not raise
        strategy._dp_send("test message")

    def test_dp_send_gracefully_handles_exception(self, strategy):
        """_dp_send should not raise when dp.send_msg raises."""
        strategy.dp.send_msg.side_effect = Exception("Telegram error")
        # Should not raise
        strategy._dp_send("test message")
